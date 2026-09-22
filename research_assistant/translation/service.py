"""Persistent single-worker translation queue with conservative output checks."""

import asyncio
import json
import math
import os
import re
import uuid
from difflib import SequenceMatcher
from pathlib import Path
from pypdf import PdfReader
from research_assistant.memory.store import digest, inspect_pdf, now, Conflict
from research_assistant.translation.policy import runtime_policy, ERROR_MESSAGES
from research_assistant.translation.ledger import TranslationLedger, DEFAULT_TOKENS, DEFAULT_REQUESTS
from research_assistant.translation.references import reference_pages, preserve_reference_pages

VERSIONS = {"pdf2zh-next": "2.9.0", "BabelDOC": "0.6.2", "PyMuPDF": "1.25.2"}


def protocol_event(line):
    """Upstream shutdown logs can include a bare JSON number, not an event."""
    try:
        event = json.loads(line)
    except (ValueError, UnicodeError):
        return None
    return (
        event
        if isinstance(event, dict)
        and event.get("type") in {"progress", "error", "finish"}
        else None
    )


def safe_worker_metrics(event):
    """Persist only numeric diagnostics, never upstream text or credentials."""
    counts = event.get("diagnostics")
    counts = counts if isinstance(counts, dict) else {}
    result = {"diagnostics": {k: counts[k] for k in (
        "paragraph_errors", "timeout_records", "rate_limit_records", "unchanged_fallback_records"
    ) if type(counts.get(k)) is int and counts[k] >= 0}}
    seconds = event.get("seconds")
    if type(seconds) in (int, float) and math.isfinite(seconds) and seconds >= 0:
        result["engine_seconds"] = seconds
    usage = event.get("token_usage")
    if isinstance(usage, dict):
        result["token_usage"] = {
            role: {k: values[k] for k in ("total", "prompt", "completion", "cache_hit_prompt")
                   if type(values.get(k)) is int and values[k] >= 0}
            for role, values in usage.items() if role in {"main", "term"} and isinstance(values, dict)
        }
    return result


def configuration(root):
    python = Path(
        os.getenv(
            "RESEARCH_TRANSLATION_PYTHON",
            str(Path(root) / (".translation_env/Scripts/python.exe" if os.name == "nt" else ".translation_env/bin/python")),
        )
    )
    enabled = os.getenv("RESEARCH_TRANSLATION_ENABLED", "true").lower() in {
        "true",
        "1",
        "yes",
    }
    key = os.getenv("TRANSLATION_API_KEY") or os.getenv("OPENAI_API_KEY", "")
    model = os.getenv("TRANSLATION_MODEL") or os.getenv("OPENAI_MODEL", "")
    base = os.getenv("TRANSLATION_BASE_URL") or os.getenv("OPENAI_BASE_URL", "")
    return {
        "configured": bool(enabled and python.is_file() and key and model and base),
        "enabled": enabled,
        "installed": python.is_file(),
        "python": str(python),
        "model": model,
        "base_url": base,
        "key": key,
    }


def profile(config, force=False):
    # Never persist credentials, full endpoint URLs or server paths.
    result = {
        "versions": VERSIONS,
        "engine": "pdf2zh-next",
        "model": config["model"],
        "endpoint_hash": digest(config["base_url"].encode()),
        "target": "zh",
        "source": "en",
        "table_text": True,
        "auto_glossary": False,
        **runtime_policy(config["model"], config["base_url"]),
        "watermark": "no_watermark",
    }
    if force:
        result["generation"] = str(uuid.uuid4())
    return result


def retained_english_prose(before, after):
    """Conservatively flag long unchanged prose, not names or short labels.

    CJK tokens remain boundaries: do not join scattered English abbreviations
    across translated sentences. This heuristic cannot prove full translation.
    Long intentional English quotations can also trigger manual review.
    """
    def tokens(text):
        text = re.sub(r"(?<=[A-Za-z])-\s*\n\s*(?=[A-Za-z])", "", text)
        return re.findall(r"[a-z]+|[\u4e00-\u9fff]", text.lower())

    source, target = tokens(before), tokens(after)
    prose_words = {"the", "and", "of", "to", "in", "is", "are", "we", "with", "that", "for", "this"}
    for block in SequenceMatcher(None, source, target, autojunk=False).get_matching_blocks():
        words = target[block.b : block.b + block.size]
        if (
            block.size >= 80
            and all(word.isascii() for word in words)
            and len(set(words) & prose_words) >= 5
        ):
            return True
    return False


def validate_output(original_path, output_path):
    source, output = inspect_pdf(original_path), inspect_pdf(output_path)
    issues = []
    if source["page_count"] != output["page_count"]:
        issues.append("页数与原件不同")
    src_pages, dst_pages = PdfReader(original_path).pages, PdfReader(output_path).pages
    bibliography = reference_pages([p.extract_text() or "" for p in src_pages])
    numeric_warnings = []
    for i, (a, b) in enumerate(zip(source["pages"], output["pages"])):
        if (
            a["view_box"] != b["view_box"]
            or a["rotation"] != b["rotation"]
            or a["user_unit"] != b["user_unit"]
        ):
            issues.append(f"第 {i + 1} 页尺寸、裁剪或旋转变化")
        is_reference = i + 1 in bibliography
        if b["text_chars"] < 40 or (not is_reference and a["latin"] > 100 and b["cjk"] < 15):
            issues.append(f"第 {i + 1} 页缺少可选中文，可能漏译")
        before, after = (
            src_pages[i].extract_text() or "",
            dst_pages[i].extract_text() or "",
        )
        if "\ufffd" in after:
            issues.append(f"第 {i + 1} 页存在乱码替代字符")
        if is_reference:
            if re.sub(r"\s+", "", before) != re.sub(r"\s+", "", after):
                issues.append(f"第 {i + 1} 页参考文献未原样保留，请先恢复原件页面")
        elif before.strip() == after.strip():
            issues.append(f"第 {i + 1} 页未检测到翻译")
        elif retained_english_prose(before, after):
            issues.append(f"第 {i + 1} 页存在长段未翻译英文；产物不完整，请核对或重试")
        numbers = set(re.findall(r"\d+(?:\.\d+)?", before))
        missing = numbers - set(re.findall(r"\d+(?:\.\d+)?", after))
        if missing:
            numeric_warnings.append(
                {"page": i + 1, "missing_number_count": len(missing)}
            )
    if issues:
        raise ValueError("产物未通过完整性检查：" + "；".join(issues[:8]))
    return {
        "automated_checks": "passed",
        "human_review": "pending",
        "page_count": output["page_count"],
        "numeric_warnings": numeric_warnings,
        "reference_pages_preserved": bibliography,
        "reference_policy": "pure_numbered_bibliography_original_pages; mixed_pages_checked_as_body",
        "mapping": "unaligned",
        "limitations": "文字层和页数检查不能证明全文翻译完整、公式正确或无遮挡；须逐页核对，未做自动原译文对齐。",
    }


class TranslationService:
    def __init__(self, store):
        self.store = store
        self.task = None
        self.wakeup = asyncio.Event()
        self.process = None
        self.ledger = TranslationLedger(store.path)
        self.cancel_requested = set()

    def status(self):
        cfg = configuration(self.store.root)
        return {k: cfg[k] for k in ("configured", "enabled", "installed", "model")} | {
            "versions": VERSIONS,
            "auto_translate": self.store.settings().get("auto_translate", False),
            "configuration_url": "/reader#settings",
            "usage_policy": "meter_only_with_loop_protection",
        }

    async def start(self):
        interrupted = [j for j in self.store.jobs() if j["state"] in {"queued", "running"}]
        self.store.interrupt_jobs()
        for job in interrupted:
            account = self.ledger.summary(job["id"])
            blocked = account and account["blocked"]
            if blocked in ERROR_MESSAGES:
                self.store.update_job(
                    job["id"], expected={"failed"}, stage="失败",
                    error_code=blocked, error=ERROR_MESSAGES[blocked],
                )
            self.ledger.recover(job["id"], "failed")
        self.ledger.recover()
        self.task = asyncio.create_task(self.run())

    async def stop(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass

    def submit(self, paper_id, *, force=False, manual=False, token_budget=DEFAULT_TOKENS, request_limit=DEFAULT_REQUESTS):
        cfg = configuration(self.store.root)
        if not cfg["configured"]:
            raise ValueError(
                "翻译服务未配置。请查看阅读器中的配置说明；原文和手动笔记仍可使用。"
            )
        original = self.store.artifact(self.store.paper(paper_id)["original_id"])
        meta = original["metadata"]
        if meta["language"] == "zh":
            raise ValueError("检测为中文文献，无需重复翻译。")
        if not meta["text_layer"]:
            raise ValueError(meta["warning"])
        if meta["language"] != "en" and not manual:
            raise ValueError("语言不确定或混合，请手动确认按英文翻译。")
        if type(token_budget) is not int or type(request_limit) is not int or token_budget < 0 or request_limit < 0:
            raise ValueError("验证预算必须为非负整数。")
        job, created = self.store.reserve_job(paper_id, profile(cfg, force))
        self.ledger.account(job["id"], token_budget, request_limit)
        self.wakeup.set()
        return job, created

    def retry(self, job_id):
        old = self.store.job(job_id)
        if old["state"] != "failed":
            raise ValueError("仅失败任务可以重试。")
        recovered = self.recover_validated_output(job_id)
        if recovered:
            return recovered
        if not configuration(self.store.root)["configured"]:
            raise ValueError("请先配置翻译服务。")
        # A changed model/config must create a new cache key instead of lying about provenance.
        current = profile(configuration(self.store.root))
        if {k: v for k, v in old["profile"].items() if k != "generation"} != current:
            # An explicit retry after an adapter/model upgrade gets a new cache
            # identity. Preserve the old attempt and all existing annotations.
            limits = self.ledger.summary(old["id"])
            job, _ = self.submit(old["paper_id"], manual=True,
                                 token_budget=(limits["token_limit"] or 0) if limits else DEFAULT_TOKENS,
                                 request_limit=(limits["request_limit"] or 0) if limits else DEFAULT_REQUESTS)
            if job["state"] == "failed":
                return self.retry(job["id"])
            return job
        job = self.store.update_job(
            job_id,
            expected={"failed"},
            state="queued",
            stage="等待重试",
            progress=None,
            error=None,
            error_code=None,
            # Request ledger and previous aggregate usage are retained on retry.
            engine_seconds=None,
            attempt=old["attempt"] + 1,
        )
        self.wakeup.set()
        return job

    def jobs(self, paper_id=None):
        return [{**j, "budget": self.ledger.summary(j["id"])} for j in self.store.jobs(paper_id)]

    def recover_validated_output(self, job_id):
        """Recheck completed output locally, without a new model call."""
        job = self.store.job(job_id)
        if (job["state"] != "failed" or not str(job.get("error", "")).startswith("产物未通过完整性检查：")
                or (job.get("diagnostics") or {}).get("paragraph_errors", 0)):
            return None
        account = self.ledger.summary(job_id)
        if account and account["blocked"]:
            return None
        output = (self.store.root / ".data/translation_work" / job_id / str(job["attempt"])).resolve()
        candidates = list(output.glob("*.no_watermark.zh.mono.pdf"))
        if len(candidates) != 1 or not candidates[0].resolve().is_relative_to(output):
            return None
        original = self.store.artifact_path(job["original_id"])
        candidate, pages = preserve_reference_pages(original, candidates[0])
        validation = validate_output(original, candidate)
        artifact = self.store.add_translation(job["paper_id"], job["original_id"], candidate,
            {**job["profile"], "job_id": job_id, "generated_at": now(),
             "postprocessing": "preserve_original_reference_pages", "reference_pages": pages,
             "engine_output_hash": digest(candidates[0].read_bytes())}, validation)
        return self.store.update_job(job_id, expected={"failed"}, state="succeeded", progress=100,
            stage="已恢复现有译文；参考文献保留原文，未重新调用模型", error=None, error_code=None,
            previous_validation_error=job["error"], artifact_id=artifact["id"], validation=validation)

    async def cancel(self, job_id):
        job = self.store.job(job_id)
        if job["state"] not in {"queued", "running"}:
            return job
        self.cancel_requested.add(job_id)
        if job["state"] == "running":
            await self.kill_process()
        else:
            self.cancel_requested.discard(job_id)
        return self.store.update_job(job_id, state="failed", stage="已停止", progress=None,
                                     error_code="cancelled", error=ERROR_MESSAGES["cancelled"])

    async def kill_process(self):
        process = self.process
        if process and process.returncode is None:
            if os.name == "nt":
                killer = await asyncio.create_subprocess_exec(
                    "taskkill",
                    "/PID",
                    str(process.pid),
                    "/T",
                    "/F",
                    stdout=asyncio.subprocess.DEVNULL,
                    stderr=asyncio.subprocess.DEVNULL,
                )
                await killer.wait()
            else:
                import signal

                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    # The child can exit before asyncio observes its return code.
                    pass
            await process.wait()

    async def worker_lines(self, job_id):
        read = asyncio.create_task(self.process.stdout.readline())
        try:
            while True:
                done, _ = await asyncio.wait({read}, timeout=1)
                blocked, pending = self.ledger.activity(job_id)
                # Let already-sent requests settle their usage before killing layout.
                if blocked and not pending:
                    raise ValueError(ERROR_MESSAGES.get(blocked, "翻译请求已停止。"))
                if not done:
                    continue
                line = read.result()
                if not line:
                    return
                yield line
                read = asyncio.create_task(self.process.stdout.readline())
        finally:
            read.cancel()
            try:
                await read
            except asyncio.CancelledError:
                pass

    async def execute(self, job):
        cfg = configuration(self.store.root)
        if not cfg["configured"] or {
            k: v for k, v in job["profile"].items() if k != "generation"
        } != profile(cfg):
            raise ValueError("服务配置已变化或不可用，请重新提交。")
        output = (
            self.store.root / ".data/translation_work" / job["id"] / str(job["attempt"])
        )
        output.mkdir(parents=True, exist_ok=True)
        request = {
            "input": str(self.store.artifact_path(job["original_id"])),
            "output": str(output),
            "versions": VERSIONS,
            "key": cfg["key"],
            "model": cfg["model"],
            "base_url": cfg["base_url"],
        }
        self.ledger.account(job["id"])
        self.ledger.start(job["id"], job["attempt"])
        request["ledger"] = {"database": str(self.store.path), "job_id": job["id"],
                             "attempt": job["attempt"], "endpoint_hash": job["profile"]["endpoint_hash"]}
        env = dict(os.environ, PYTHONIOENCODING="utf-8",
                   RESEARCH_FULL_TRANSLATION_CONTEXT=json.dumps(request["ledger"]))
        worker = (
            Path(__file__).resolve().parents[2] / "deployment/translation/worker.py"
        )
        self.process = await asyncio.create_subprocess_exec(
            cfg["python"],
            str(worker),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
            env=env,
            start_new_session=os.name != "nt",
            limit=1024 * 1024,
        )
        self.process.stdin.write((json.dumps(request) + "\n").encode())
        await self.process.stdin.drain()
        self.process.stdin.close()
        candidate = None
        async for line in self.worker_lines(job["id"]):
            if job["id"] in self.cancel_requested:
                raise ValueError(ERROR_MESSAGES["cancelled"])
            event = protocol_event(line)
            if event is None:
                continue
            if event.get("type") == "progress":
                percent = event.get("progress")
                if type(percent) not in (int, float) or not math.isfinite(percent):
                    percent = None
                rss = event.get("rss_bytes")
                peak = max(
                    self.store.job(job["id"]).get("peak_process_tree_rss_bytes", 0),
                    rss if type(rss) is int and rss > 0 else 0,
                )
                self.store.update_job(
                    job["id"],
                    stage=event.get("stage") or "翻译排版",
                    progress=min(99, max(0, percent)) if percent is not None else None,
                    peak_process_tree_rss_bytes=peak,
                )
            elif event.get("type") == "error":
                code = event.get("code", "engine_failed")
                self.store.update_job(job["id"], error_code=code if code in ERROR_MESSAGES else "engine_failed",
                                      **safe_worker_metrics(event))
                raise ValueError(
                    ERROR_MESSAGES.get(code) or (
                    "翻译引擎失败（"
                    + re.sub(r"[^a-zA-Z0-9_]", "", str(event.get("code", "unknown")))[
                        :60
                    ]
                    + "）。请检查网络、模型凭据、额度及引擎资源下载，然后重试。")
                )
            elif event.get("type") == "finish" and event.get("path"):
                candidate = Path(event["path"]).resolve()
                self.store.update_job(job["id"], **safe_worker_metrics(event))
        returncode = await self.process.wait()
        budget = self.ledger.summary(job["id"])
        if budget and budget["blocked"]:
            self.store.update_job(job["id"], error_code=budget["blocked"])
            raise ValueError(ERROR_MESSAGES.get(budget["blocked"], "翻译请求已停止，请检查账户或网络。"))
        if job["id"] in self.cancel_requested:
            raise ValueError(ERROR_MESSAGES["cancelled"])
        if (
            returncode != 0
            or not candidate
            or not candidate.is_relative_to(output.resolve())
            or not candidate.is_file()
        ):
            raise ValueError("翻译引擎未生成有效产物，请检查配置及网络后重试。")
        self.store.update_job(
            job["id"], stage="核对页数、页面几何和文字层", progress=None
        )
        engine_hash = digest(candidate.read_bytes())
        candidate, reference_page_numbers = await asyncio.to_thread(preserve_reference_pages, request["input"], candidate)
        validation = await asyncio.to_thread(
            validate_output, request["input"], candidate
        )
        artifact = self.store.add_translation(
            job["paper_id"],
            job["original_id"],
            candidate,
            {**job["profile"], "generated_at": now(), "job_id": job["id"],
             "postprocessing": "preserve_original_reference_pages", "reference_pages": reference_page_numbers,
             "engine_output_hash": engine_hash},
            validation,
        )
        self.store.update_job(
            job["id"],
            state="succeeded",
            stage="已生成，自动检查通过；翻译质量待人工核对",
            progress=100,
            artifact_id=artifact["id"],
            validation=validation,
        )

    async def run(self):
        while True:
            queued = [j for j in reversed(self.store.jobs()) if j["state"] == "queued"]
            if not queued:
                self.wakeup.clear()
                await self.wakeup.wait()
                continue
            job = queued[0]
            try:
                self.store.update_job(
                    job["id"],
                    expected={"queued"},
                    state="running",
                    stage="启动引擎 / 准备字体和布局模型",
                    progress=None,
                )
                await asyncio.wait_for(
                    self.execute(job),
                    timeout=float(os.getenv("RESEARCH_TRANSLATION_TIMEOUT", "1800")),
                )
            except asyncio.CancelledError:
                try:
                    await self.kill_process()
                finally:
                    self.store.update_job(
                        job["id"],
                        state="failed",
                        stage="应用停止",
                        progress=None,
                        error="任务被中断，请手动重试。",
                    )
                raise
            except Exception as exc:
                try:
                    await self.kill_process()
                finally:
                    # Even a cleanup failure must not leave a completed attempt running.
                    account = self.ledger.summary(job["id"])
                    blocked = account and account["blocked"]
                    message = (
                        "翻译超过时间上限，请缩小文档或调整任务超时后重试。"
                        if isinstance(exc, asyncio.TimeoutError)
                        else str(exc)
                        if isinstance(exc, (ValueError, Conflict))
                        else "翻译失败，请检查引擎安装、网络和 PDF 是否受支持。"
                    )
                    self.store.update_job(
                        job["id"],
                        state="failed",
                        stage="失败",
                        progress=None,
                        error=ERROR_MESSAGES.get(blocked, message),
                        **({"error_code": blocked} if blocked else {}),
                    )
            finally:
                self.ledger.recover(job["id"], self.store.job(job["id"])["state"])
                self.cancel_requested.discard(job["id"])
                self.process = None
