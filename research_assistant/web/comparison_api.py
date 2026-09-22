"""可轮询、可取消的文献对照任务；SQLite 保存证据快照和已完成的分析。"""

import asyncio
import hashlib
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone

from starlette.responses import JSONResponse
from starlette.routing import Route

from research_assistant.agents.comparison_workflow import ComparisonWorkflow, call_model, collect_evidence
from research_assistant.core.agent_telemetry import ExecutionTrace, error_category, interrupted_trace
from research_assistant.projects.project_workflow import source_versions


TERMINAL = {"completed", "failed", "cancelled", "interrupted", "stale"}


class Conflict(ValueError):
    pass


class ComparisonStore:
    def __init__(self, path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(sqlite3.connect(path)) as con, con:
            con.execute("CREATE TABLE IF NOT EXISTS comparisons (id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, payload TEXT NOT NULL)")
            # 本地单进程服务重启后不会自动再次外发资料或重复付费。
            for item_id, payload in con.execute("SELECT id, payload FROM comparisons").fetchall():
                run = json.loads(payload)
                if run["phase"] not in TERMINAL:
                    run.update(phase="interrupted", error="服务已重启；已完成的分析仍保留。重新分析需要再次提交。")
                    if run.get("telemetry"):
                        run["telemetry"] = interrupted_trace(run["telemetry"])
                    con.execute("UPDATE comparisons SET payload=? WHERE id=?", (json.dumps(run, ensure_ascii=False), item_id))

    def get(self, item_id):
        with closing(sqlite3.connect(self.path)) as con:
            row = con.execute("SELECT payload FROM comparisons WHERE id=?", (item_id,)).fetchone()
        if not row:
            raise KeyError(item_id)
        return json.loads(row[0])

    def list(self):
        with closing(sqlite3.connect(self.path)) as con:
            rows = con.execute("SELECT payload FROM comparisons ORDER BY rowid DESC LIMIT 20").fetchall()
        fields = ("id", "question", "sources", "phase", "created_at", "calls", "call_budget")
        return [{k: r[k] for k in fields} for r in (json.loads(row[0]) for row in rows)]

    def reserve(self, item_id, question, options, versions):
        fingerprint = hashlib.sha256(json.dumps([question, options, versions], sort_keys=True).encode()).hexdigest()
        with closing(sqlite3.connect(self.path)) as con, con:
            con.execute("BEGIN IMMEDIATE")
            existing = con.execute("SELECT fingerprint, payload FROM comparisons WHERE id=?", (item_id,)).fetchone()
            if existing:
                if existing[0] != fingerprint:
                    raise Conflict("请求编号已用于其他问题或资料版本。请重新提交。")
                return json.loads(existing[1]), False
            active = [(fp, json.loads(p)) for fp, p in con.execute("SELECT fingerprint, payload FROM comparisons") if json.loads(p)["phase"] not in TERMINAL]
            if active:
                if active[0][0] == fingerprint:
                    return active[0][1], False
                raise Conflict("已有文献对照正在执行，请等待或先停止该任务。")
            run = {"id": item_id, "question": question, "sources": options["sources"],
                   "options": options, "source_versions": versions, "phase": "queued",
                   "created_at": datetime.now(timezone.utc).isoformat(), "external_consent": True,
                   "calls": 0, "call_budget": len(options["sources"]) + 2,
                   "telemetry": None,
                   "analyses": [], "documents": [], "result": None, "error": None}
            con.execute("INSERT INTO comparisons VALUES(?,?,?)", (item_id, fingerprint, json.dumps(run, ensure_ascii=False)))
            return run, True

    def update(self, item_id, **values):
        with closing(sqlite3.connect(self.path)) as con, con:
            con.execute("BEGIN IMMEDIATE")
            row = con.execute("SELECT payload FROM comparisons WHERE id=?", (item_id,)).fetchone()
            if not row:
                raise KeyError(item_id)
            run = json.loads(row[0])
            # 取消先改变业务终态；异步调用退出后仍须补齐计时，但不能改回运行中。
            if run["phase"] not in TERMINAL or set(values) == {"telemetry"}:
                run.update(values)
                con.execute("UPDATE comparisons SET payload=? WHERE id=?", (json.dumps(run, ensure_ascii=False), item_id))
        return run


def make_comparison_routes(root, get_index, parse_options, *, caller=call_model,
                           collector=None, total_timeout=180, retrieval_timeout=45,
                           call_timeout=35):
    store = ComparisonStore(root / ".data/comparisons.sqlite3")
    workflow = ComparisonWorkflow(caller, call_timeout=call_timeout)
    workers, retrievals = {}, set()
    collector = collector or (lambda question, options: collect_evidence(get_index, question, options))

    def public(run):
        return {k: v for k, v in run.items() if k not in {"source_versions"}}

    async def body(request):
        origin = request.headers.get("origin")
        if (origin and origin.rstrip("/") != str(request.base_url).rstrip("/")) or request.headers.get("sec-fetch-site") == "cross-site":
            raise PermissionError("不允许跨站发起文献分析。")
        if request.headers.get("content-type", "").split(";")[0] != "application/json":
            raise ValueError("请使用 JSON 请求。")
        raw = bytearray()
        async for chunk in request.stream():
            raw.extend(chunk)
            if len(raw) > 65536:
                raise ValueError("请求超过 64 KB。")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("请求必须是 JSON 对象。")
        return data

    def endpoint(fn):
        async def wrapped(request):
            try:
                return await fn(request)
            except PermissionError as exc:
                return JSONResponse({"error": str(exc), "error_code": "external_permission_required"}, status_code=403)
            except Conflict as exc:
                return JSONResponse({"error": str(exc)}, status_code=409)
            except KeyError:
                return JSONResponse({"error": "对照任务不存在。"}, status_code=404)
            except (ValueError, TypeError) as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
        return wrapped

    async def execute(run, trace):
        item_id = run["id"]
        store.update(item_id, phase="retrieving")
        # 同步索引读取不能强行杀线程。超时/取消后禁止再调用模型，并阻止累积新的取证线程。
        future = asyncio.create_task(asyncio.to_thread(collector, run["question"], run["options"]))
        retrievals.add(future)
        def retrieved(task):
            retrievals.discard(task)
            if not task.cancelled():
                task.exception()
        future.add_done_callback(retrieved)
        retrieval_span = trace.start("retrieving", "phase")
        try:
            documents = await asyncio.wait_for(asyncio.shield(future), retrieval_timeout)
            trace.end(retrieval_span)
        except asyncio.CancelledError:
            trace.end(retrieval_span, "cancelled")
            raise
        except Exception as exc:
            category = error_category(exc)
            trace.end(retrieval_span, "timeout" if category == "timeout" else "error", category=category)
            raise
        store.update(item_id, documents=documents)
        versions = await asyncio.to_thread(source_versions, root, run["sources"])
        if versions != run["source_versions"]:
            store.update(item_id, phase="stale", error="取证期间资料或解析缓存发生变化，请重新提交。")
            return
        result = await workflow.run(run["question"], documents, lambda **kw: store.update(item_id, **kw), trace=trace)
        result.pop("telemetry", None)  # 顶层轨迹统一包含取证、图执行和取消收尾。
        versions = await asyncio.to_thread(source_versions, root, run["sources"])
        if versions != run["source_versions"]:
            store.update(item_id, phase="stale", error="分析期间资料或解析缓存发生变化，本次仅保留旧版证据和分析草稿。")
            return
        store.update(item_id, phase="completed", result=result)

    async def worker(run):
        trace = ExecutionTrace(lambda snapshot: store.update(run["id"], telemetry=snapshot))
        try:
            await asyncio.wait_for(execute(run, trace), total_timeout)
        except asyncio.CancelledError:
            store.update(run["id"], phase="cancelled", error="已停止后续模型调用；已经发送的请求可能仍由模型服务计费。")
        except TimeoutError:
            store.update(run["id"], phase="failed", error="达到本轮执行时限，已停止后续调用；保留已取得证据与分析。")
        except Exception as exc:
            store.update(run["id"], phase="failed", error=f"对照未完成（{type(exc).__name__}），保留已取得证据与分析。")
        finally:
            try:
                trace.finish(store.get(run["id"])["phase"])
            finally:
                workers.pop(run["id"], None)

    @endpoint
    async def runs(request):
        if request.method == "GET":
            return JSONResponse({"runs": store.list()})
        data = await body(request)
        if data.get("allow_external") is not True:
            raise PermissionError("请先允许将问题和所选文献片段发送给 DeepSeek 进行分析与复核。")
        question = data.get("question")
        if not isinstance(question, str) or not 1 <= len(question.strip()) <= 3000:
            raise ValueError("对照问题应为 1 到 3000 个字符。")
        sources = data.get("sources")
        if not isinstance(sources, list) or not all(isinstance(s, str) for s in sources) or not 2 <= len(sources) <= 4 or len(set(sources)) != len(sources):
            raise ValueError("多文献对照需要选择 2 到 4 篇不同文献。")
        if type(data.get("k", 5)) is not int or not 1 <= data.get("k", 5) <= 8:
            raise ValueError("每篇文献的证据数量应为 1 到 8。")
        item_id = data.get("request_id", "")
        if not isinstance(item_id, str) or not 16 <= len(item_id) <= 64 or not all(c in "0123456789abcdef-" for c in item_id):
            raise ValueError("缺少有效请求编号。")
        options = parse_options(data)
        versions = await asyncio.to_thread(source_versions, root, options["sources"])
        if retrievals and not workers:
            raise Conflict("上一轮已停止，底层索引读取尚在收尾，请稍后再提交。")
        run, created = store.reserve(item_id, question.strip(), options, versions)
        if created:
            task = asyncio.create_task(worker(run))
            workers[run["id"]] = task
            # 即使任务尚未开始就被取消，仍清理占用记录。
            task.add_done_callback(lambda done: workers.pop(run["id"], None))
        return JSONResponse(public(run), status_code=202 if run["phase"] not in TERMINAL else 200)

    @endpoint
    async def detail(request):
        item_id = request.path_params["run_id"]
        run = store.get(item_id)
        if request.method == "POST":
            data = await body(request)
            if data.get("action") != "cancel":
                raise ValueError("只支持停止操作。重新分析请发起新任务。")
            if item_id in workers:
                run = store.update(item_id, phase="cancelled", error="已停止后续调用；已发送请求可能仍计费。")
                workers[item_id].cancel()
        return JSONResponse(public(run))

    return [Route("/api/comparisons", runs, methods=["GET", "POST"]),
            Route("/api/comparisons/{run_id}", detail, methods=["GET", "POST"])]
