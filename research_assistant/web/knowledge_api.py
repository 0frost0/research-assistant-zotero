"""Authenticated Zotero bridge; generation only after an explicit ask request."""
import asyncio
import json
import os
from pathlib import Path
import re
import secrets
import tempfile
from urllib.parse import urlsplit

from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

from research_assistant.knowledge.store import KnowledgeStore
from research_assistant.knowledge.service import KnowledgeService, IndexBuilding, search_ready
from research_assistant.knowledge.qa import CombinedQA, EvidenceQA, MemoryQA, model_configured
from research_assistant.knowledge.jobs import QAJobStore
from research_assistant.knowledge.backup import export_backup
from research_assistant.memory.store import Conflict
from research_assistant.core.paths import STATIC_DIR
from research_assistant.core.service_config import ServiceConfigStore
from research_assistant.core.service_context import service_context
from research_assistant.agents.comparison_workflow import ComparisonWorkflow
from research_assistant.knowledge.parsing import ParsingJobs


class KnowledgeAPI:
    def __init__(self, root, token=None, backend=None, comparison_caller=None):
        self.root = Path(root)
        self.explicit_token = token
        self.backend = backend or os.getenv("RESEARCH_KNOWLEDGE_BACKEND", "multimodal")
        self._store = self._service = None
        self._qa = None
        self._memory_qa = None
        self._combined_qa = None
        self._comparison_caller = comparison_caller
        self._comparison_busy = False
        self._qa_jobs = QAJobStore(self.root / ".data/zotero/qa_jobs.sqlite3")
        self._qa_tasks = set()
        self._qa_task_by_job = {}
        self._service_configs = ServiceConfigStore(self.root)
        self._parsing = ParsingJobs(self.store)

    def _track_qa_task(self, job_id, task):
        self._qa_tasks.add(task)
        self._qa_task_by_job[job_id] = task
        def finished(done):
            self._qa_tasks.discard(done)
            if self._qa_task_by_job.get(job_id) is done:
                self._qa_task_by_job.pop(job_id, None)
        task.add_done_callback(finished)

    @property
    def store(self):
        if self._store is None:
            self._store = KnowledgeStore(self.root)
        return self._store

    @property
    def service(self):
        if self._service is None:
            self._service = KnowledgeService(self.store, self.backend)
        return self._service

    def token(self):
        if self.explicit_token is not None:
            return self.explicit_token
        configured = os.getenv("RESEARCH_ZOTERO_TOKEN")
        if configured:
            return configured
        path = self.root / ".data/zotero/connection-token"
        return path.read_text().strip() if path.is_file() else None
    def _connection_id(self, request):
        """
        读取 Zotero 发送的连接 ID。

        连接 ID 不是密钥，只用于区分不同客户端的服务配置。
        """
        value = request.headers.get("x-research-connection", "").strip()

        if not value:
            raise ValueError("缺少 Zotero connection 标识。")

        if len(value) > 200:
            raise ValueError("connection 标识过长。")

        return value


    @staticmethod
    def _public_service_config(value):
        """
        返回给 Zotero 的配置。

        API Key 永远不返回。
        """
        result = {}

        for section in ("llm", "embedding", "mineru"):
            current = value.get(section, {})

            if section == "mineru":
                result[section] = {
                    "mode": current.get("mode", "remote"),
                    "url": current.get("url", ""),
                    "timeout": current.get("timeout", 1800),
                    "python": current.get("python", ""),
                    "auto_parse": current.get("auto_parse", False),
                    "configured": bool(current.get("mode") == "local" or (current.get("mode") == "remote" and current.get("url"))),
                    "runtime_connected": None,
                    "runtime_wired": True,
                    "has_api_key": False,
                }
            else:
                result[section] = {
                    "base_url": current.get("base_url", ""),
                    "model": current.get("model", ""),
                    "configured": bool(
                        current.get("base_url")
                        and current.get("model")
                        and current.get("api_key")
                    ),
                    "has_api_key": bool(current.get("api_key")),
                    "runtime_connected": None,
                    "runtime_wired": True,
                }

        result["embedding"].update({
            "dimension": value.get("embedding", {}).get("dimension", 2048),
            "revision": value.get("embedding", {}).get("revision", "1"),
            "protocol": "vllm-qwen3-vl-messages-v1",
            "runtime_wired": True,
        })
        return result

    async def endpoint(self, request):
        configured = self.token()
        if not configured:
            return JSONResponse({"error": "Zotero 连接尚未配置。"}, status_code=503)
        supplied = request.headers.get("authorization", "")
        if not secrets.compare_digest(supplied, "Bearer " + configured):
            return JSONResponse({"error": "连接令牌无效。"}, status_code=401)
        origin = request.headers.get("origin")
        if origin and origin != f"{request.url.scheme}://{request.url.netloc}":
            return JSONResponse({"error": "拒绝跨站请求。"}, status_code=403)
        connection = request.headers.get("x-research-connection", "").strip()
        if len(connection) > 200:
            return JSONResponse({"error": "connection 标识过长。"}, status_code=400)
        try:
            config = self._service_configs.get(connection) if connection else {}
        except ValueError:
            return JSONResponse({"error": "无法读取服务配置，未使用旧服务替代。"}, status_code=503)
        token = service_context.set({"connection": connection, "config": config} if connection else None)
        try:
            return await self._endpoint(request)
        finally:
            service_context.reset(token)

    async def _endpoint(self, request):
        action, ident = request.path_params.get("action"), request.path_params.get("ident")
        try:
            if request.method == "GET":
                if action == "status":
                    result = {"protocol": 1, "backend": self.backend, "generation": model_configured(),
                              "capabilities": ["search", "ask", "memory_ask", "compare"], "qa_max_calls": EvidenceQA.MAX_CALLS,
                              "comparison_max_sources": 4,
                              "knowledge": self.store.stats(),
                              "qa_jobs": self._qa_jobs.stats()}
                elif action == "services":
                    connection = self._connection_id(request)
                    result = self._public_service_config(
                        self._service_configs.get(connection)
                    )
                elif action == "parsing":
                    result = {"jobs": self._parsing.list()}
                elif action == "bases":
                    result = self.store.snapshot([ident]) if ident else {"bases": self.store.bases()}
                elif action == "history" and ident:
                    result = self.store.note_history(ident)
                elif action == "jobs":
                    result = (self._qa_jobs.public(ident) if ident
                              else {"jobs": self._qa_jobs.list()})
                elif action == "objects" and ident:
                    result = {"hash": ident, "exists": await run_in_threadpool(self.store.object_exists, ident)}
                elif action == "export":
                    fd, name = tempfile.mkstemp(suffix=".zip")
                    os.close(fd)
                    try:
                        await run_in_threadpool(export_backup, self.store, name)
                    except BaseException:
                        Path(name).unlink(missing_ok=True)
                        raise
                    return FileResponse(name, media_type="application/zip",
                                        filename="research-knowledge-backup.zip",
                                        background=BackgroundTask(Path(name).unlink, missing_ok=True))
                else:
                    raise KeyError(action)
            else:
                limit = 25 * 1024 * 1024 if action == "objects" else 8 * 1024 * 1024
                raw = bytearray()
                async for part in request.stream():
                    raw.extend(part)
                    if len(raw) > limit:
                        raise ValueError("请求超过允许大小。")
                if action == "objects" and ident and request.method == "PUT":
                    result = await run_in_threadpool(self.store.put_object, ident, bytes(raw))
                else:
                    data = json.loads(raw or b"{}")
                    if not isinstance(data, dict):
                        raise ValueError("请求须为 JSON 对象。")
                    if action == "bases" and not ident:
                        result = self.store.create(data)
                    elif action == "services":
                        connection = self._connection_id(request)
                        if not all(isinstance(data.get(section), dict) for section in ("llm", "embedding", "mineru")):
                            raise ValueError("问答模型和嵌入模型配置格式无效。")
                        llm = data["llm"]
                        embedding = data["embedding"]
                        mineru = data.get("mineru", {})
                        cleaned = {
                            "llm": {
                                "base_url": str(llm.get("base_url", "")).strip(),
                                "model": str(llm.get("model", "")).strip(),
                                "api_key": str(llm.get("api_key", "")).strip(),
                            },
                            "embedding": {
                                "base_url": str(embedding.get("base_url", "")).strip(),
                                "model": str(embedding.get("model", "")).strip(),
                                "api_key": str(embedding.get("api_key", "")).strip(),
                            },
                            "mineru": {
                                "mode": mineru.get("mode", "remote"),
                                "url": str(mineru.get("url", "")).strip(),
                                "timeout": mineru.get("timeout", 1800),
                                "python": mineru.get("python", ""),
                                "auto_parse": mineru.get("auto_parse", False),
                            },
                        }
                        cleaned["embedding"]["dimension"] = embedding.get("dimension", 2048)
                        cleaned["embedding"]["revision"] = embedding.get("revision", "1")
                        if (type(cleaned["embedding"]["dimension"]) is not int or
                                not 1 <= cleaned["embedding"]["dimension"] <= 8192):
                            raise ValueError("嵌入维度必须是 1–8192 的整数，并与服务输出一致。")
                        if (not isinstance(cleaned["embedding"]["revision"], str) or
                                not cleaned["embedding"]["revision"].strip() or
                                len(cleaned["embedding"]["revision"]) > 100):
                            raise ValueError("请填写有效的嵌入服务版本标识。")
                        if cleaned["mineru"]["mode"] not in {"local", "remote"}:
                            raise ValueError("MinerU 模式无效。")
                        python_path = cleaned["mineru"]["python"]
                        if not isinstance(python_path, str) or (python_path and not Path(python_path).is_absolute()):
                            raise ValueError("MinerU Python 须为后端机器上的绝对路径。")
                        if type(cleaned["mineru"]["auto_parse"]) is not bool:
                            raise ValueError("自动精析配置必须为布尔值。")
                        for section in ("llm", "embedding"):
                            for key in ("base_url", "model", "api_key"):
                                if not isinstance(data[section].get(key, ""), str):
                                    raise ValueError("配置字段必须是字符串。")
                            if not cleaned[section]["model"] or len(cleaned[section]["model"]) > 200:
                                raise ValueError("模型名称无效。")
                            cleaned[section]["base_url"] = self._validate_service_url(cleaned[section]["base_url"])
                        if cleaned["mineru"]["mode"] == "remote":
                            cleaned["mineru"]["url"] = self._validate_service_url(cleaned["mineru"]["url"])
                        if type(cleaned["mineru"]["timeout"]) is not int or not 60 <= cleaned["mineru"]["timeout"] <= 7200:
                            raise ValueError("MinerU 超时必须是 60–7200 秒的整数。")
                        cleaned = self._service_configs.save(connection, cleaned)
                        result = self._public_service_config(cleaned)
                    elif action == "jobs" and ident:
                        operation = data.get("action")
                        if operation == "cancel":
                            result = self._qa_jobs.cancel(ident)
                            task = self._qa_task_by_job.get(ident)
                            if task and not task.done():
                                task.cancel()
                            return JSONResponse(result, status_code=200,
                                                headers={"Cache-Control": "no-store"})
                        if operation == "retry":
                            result = self._qa_jobs.retry(ident)
                            retry_payload = self._qa_jobs.payload(result["job_id"])
                            task = asyncio.create_task(self._run_qa_job(result["job_id"], retry_payload))
                            self._track_qa_task(result["job_id"], task)
                            return JSONResponse(result, status_code=202,
                                                headers={"Cache-Control": "no-store"})
                        if operation == "feedback":
                            result = self._qa_jobs.record_feedback(ident, data)
                            return JSONResponse(result, status_code=201,
                                                headers={"Cache-Control": "no-store"})
                        raise ValueError("问答任务只支持显式 retry 或 feedback 操作。")
                    elif action == "sync" and ident:
                        if data.get("allow_upload") is not True:
                            raise ValueError("同步前需要明确允许将所选内容发送到后端。")
                        result = await run_in_threadpool(self.store.sync, ident, data)
                        context = service_context.get()
                        mineru = context["config"].get("mineru", {}) if context else {}
                        if mineru.get("auto_parse"):
                            snapshot = self.store.snapshot([ident])
                            result["parsing_jobs"] = [self._parsing.submit(sha, mineru) for sha in sorted({
                                e["data"]["hash"] for e in snapshot["entities"] if e["data"]["kind"] == "document"})]
                    elif action == "parse":
                        context = service_context.get()
                        mineru = context["config"].get("mineru", {}) if context else {}
                        ids = data.get("kb_ids")
                        if not isinstance(ids, list) or not ids or len(ids) > 20:
                            raise ValueError("请选择知识库")
                        snapshot = self.store.snapshot(ids)
                        hashes = sorted({e["data"]["hash"] for e in snapshot["entities"]
                                         if e["data"]["kind"] == "document"})
                        result = {"jobs": [self._parsing.submit(sha, mineru) for sha in hashes]}
                    elif action == "search":
                        result = await run_in_threadpool(self.service.search, data)
                    elif action in {"ask", "ask-async"}:
                        self._validate_ask_payload(data)
                        if action == "ask-async":
                            job = self._qa_jobs.create("ask", data)
                            task = asyncio.create_task(self._run_qa_job(job["job_id"], data))
                            self._track_qa_task(job["job_id"], task)
                            return JSONResponse(job, status_code=202,
                                                headers={"Cache-Control": "no-store"})
                        result = await self._ask(data)
                        if isinstance(result, JSONResponse):
                            return result
                    elif action == "compare":
                        result = await self._compare(data)
                    elif action == "notes":
                        result = self.store.save_note(data.get("kb_id"), data, ident)
                    elif action == "archive" and ident:
                        result = self.store.set_archive(ident, data)
                    else:
                        raise KeyError(action)
            return JSONResponse(result, headers={"Cache-Control": "no-store"})
        except IndexBuilding as exc:
            return JSONResponse({"status": "indexing", "warning": str(exc), "results": [],
                                 "retry_after": 2}, status_code=202)
        except Conflict as exc:
            return JSONResponse({"error": str(exc), "code": "revision_conflict"}, status_code=409)
        except KeyError:
            return JSONResponse({"error": "知识库或记录不存在。"}, status_code=404)
        except (ValueError, TypeError, UnicodeError) as exc:
            if action == "services":
                return JSONResponse({"error": str(exc)}, status_code=400)
            return JSONResponse({"error": "请求无效、文件损坏或同步资料不完整；已有数据未被清空。"}, status_code=400)
        except Exception:
            return JSONResponse({"error": "知识库服务暂不可用，已有资料保留。"}, status_code=503)

    @staticmethod
    def _validate_service_url(value):
        try:
            url = urlsplit(value)
            port = url.port
        except ValueError:
            raise ValueError("服务地址格式无效。") from None
        local = url.hostname in {"localhost", "127.0.0.1", "::1"}
        if (len(value) > 2000 or not url.hostname or url.username or url.password
                or url.query or url.fragment or any(c.isspace() for c in value)
                or not (url.scheme == "https" or (url.scheme == "http" and local))):
            raise ValueError("服务地址须使用 HTTPS 或回环 HTTP，不允许凭据、空白或查询参数。")
        return value.rstrip("/")

    @staticmethod
    def _validate_ask_payload(data):
        if not isinstance(data.get("kb_ids"), list) or not data["kb_ids"] or len(data["kb_ids"]) > 20:
            raise ValueError("请明确选择 1–20 个知识库。")
        if data.get("scope", "original") not in {"original", "notes", "both"}:
            raise ValueError("问答范围无效。")
        if data.get("strategy", "adaptive") not in {"fixed", "selected", "adaptive"}:
            raise ValueError("未知问答策略。")
        from research_assistant.knowledge.store import text
        text(data.get("query"), "研究问题", 2000)
        conversation_id = data.get("conversation_id")
        if conversation_id is not None and (
                not isinstance(conversation_id, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", conversation_id)
        ):
            raise ValueError("会话标识无效。")

    def _with_conversation_context(self, data):
        conversation_id = data.get("conversation_id")
        if not conversation_id:
            return data
        return {**data, "_conversation_history": self._qa_jobs.conversation_history(
            conversation_id, data.get("kb_ids", []), limit=3
        )}

    async def _ask(self, data):
        data = self._with_conversation_context(data)
        if data.get("scope") == "notes":
            if self._memory_qa is None:
                self._memory_qa = MemoryQA(self.service)
            if self._memory_qa.lock.locked():
                return JSONResponse({"error": "已有个人记录问答正在执行，请等待完成，避免重复消耗。"}, status_code=429)
            return await self._memory_qa.ask(data)
        if data.get("scope") == "both":
            if self._combined_qa is None:
                self._combined_qa = CombinedQA(self.service)
            if self._combined_qa.lock.locked():
                return JSONResponse({"error": "已有原文与个人记录问答正在执行，请等待完成，避免重复消耗。"}, status_code=429)
            return await self._combined_qa.ask(data)
        if self._qa is None:
            self._qa = EvidenceQA(self.service)
        if self._qa.lock.locked():
            return JSONResponse({"error": "已有原文问答正在执行，请等待完成，避免重复消耗。"}, status_code=429)
        return await self._qa.ask(data)

    async def _run_qa_job(self, job_id, data):
        try:
            self._qa_jobs.mark_running(job_id)
            result = await self._ask(data)
            # _ask normally returns a dict.  A JSONResponse means a shared QA
            # lock was occupied; persist a safe failure rather than serializing
            # a framework response object.
            if isinstance(result, JSONResponse):
                self._qa_jobs.finish(job_id, "failed", error="已有问答任务正在执行，请稍后重新提交。")
            else:
                self._qa_jobs.finish(job_id, "succeeded", result=result)
        except asyncio.CancelledError:
            self._qa_jobs.finish(job_id, "interrupted", error="服务停止中断，未自动重试。")
            raise
        except IndexBuilding as exc:
            self._qa_jobs.finish(job_id, "failed", error=str(exc) + "索引仍在后台准备，请稍后重试。")
        except Exception as exc:
            self._qa_jobs.finish(job_id, "failed", error=f"问答未完成（{type(exc).__name__}）。请检查服务后重新提交。")

    async def page(self, request):
        return FileResponse(STATIC_DIR / "knowledge.html",
                            headers={"Referrer-Policy": "no-referrer", "Cache-Control": "no-store"})

    async def _compare(self, data):
        """在 Zotero 知识库入口复用既有 ComparisonWorkflow。

        每个 source 是一个已同步 PDF 的 sha256.pdf 标识；先按篇检索文字证据，
        再交给并行单篇分析、综合和一次复核，避免插件另起一套 Agent。
        """
        if data.get("allow_external") is not True:
            raise PermissionError("请先允许将问题和所选文献片段发送给已配置模型进行对照分析。")
        question = data.get("question")
        if not isinstance(question, str) or not 1 <= len(question.strip()) <= 3000:
            raise ValueError("对照问题应为 1 到 3000 个字符。")
        kb_ids = data.get("kb_ids")
        if not isinstance(kb_ids, list) or not kb_ids or len(kb_ids) > 20:
            raise ValueError("请选择至少一个知识库。")
        sources = data.get("sources")
        if (not isinstance(sources, list) or not 2 <= len(sources) <= 4
                or not all(isinstance(value, str) for value in sources)
                or len(set(sources)) != len(sources)):
            raise ValueError("多文献对照需要选择 2 到 4 篇不同论文。")
        k = data.get("k", 5)
        if type(k) is not int or not 1 <= k <= 8:
            raise ValueError("每篇论文的证据数量应为 1 到 8。")
        if self._comparison_busy:
            raise ValueError("已有多文献对照正在执行，请等待完成，避免重复消耗。")
        self._comparison_busy = True
        try:
            snapshot = await run_in_threadpool(self.store.snapshot, kb_ids)
            allowed = {
                entity["data"].get("hash") + ".pdf": entity["data"]
                for entity in snapshot["entities"]
                if entity["data"].get("kind") == "document" and entity["data"].get("hash")
            }
            if any(source not in allowed for source in sources):
                raise ValueError("所选论文不属于当前知识库，资料可能已同步更新。")
            documents = []
            for number, source in enumerate(sources, 1):
                result = await search_ready(self.service, {
                    "query": question.strip(), "scope": "original", "k": k,
                    "kb_ids": kb_ids, "source_ids": [source],
                })
                evidence = []
                for position, hit in enumerate(result.get("results", []), 1):
                    if hit.get("modality") == "image" or hit.get("quote") == "[图像；无文字图注]":
                        continue
                    evidence.append({
                        "ref": f"D{number}E{position}", "source": source,
                        "page": hit.get("page"), "quote": hit.get("quote", ""),
                        "evidence_id": hit.get("retrieval_evidence_id"),
                        "context_evidence_ids": hit.get("context_evidence_ids", []),
                        "modality": "text", "backend": hit.get("backend", self.backend),
                        "quality_warnings": [], "context_span": None,
                        "truncated": False, "title": allowed[source].get("title", source),
                        "url": hit.get("url"),
                    })
                documents.append({"source": source, "title": allowed[source].get("title", source),
                                  "evidence": evidence,
                                  "error": None if evidence else "当前问题未检索到这篇论文的文字证据。"})
            workflow = ComparisonWorkflow(self._comparison_caller) if self._comparison_caller else ComparisonWorkflow()
            result = await workflow.run(question.strip(), documents)
            await run_in_threadpool(self.store.assert_current, snapshot)
            result["source_titles"] = {source: allowed[source].get("title", source) for source in sources}
            result["source_urls"] = {
                e["ref"]: e.get("url")
                for document in documents for e in document["evidence"] if e.get("url")
            }
            return result
        finally:
            self._comparison_busy = False

    def routes(self):
        return [Route("/knowledge", self.page)] + [
            Route("/api/knowledge/{action}" + suffix, self.endpoint, methods=["GET", "POST", "PUT"])
            for suffix in ("", "/{ident}")
        ]
