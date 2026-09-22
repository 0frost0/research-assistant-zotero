"""Project workspace endpoints; background planning never commits without approval."""

import asyncio
import json
import logging

from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

from research_assistant.projects.project_store import Conflict, ProjectStore, TERMINAL_PHASES
from research_assistant.projects.project_workflow import ProjectWorkflow, make_drafter, source_versions


def make_project_routes(root, get_index, *, drafter=None, static_dir=None):
    store = ProjectStore(root / ".data/projects.sqlite3")
    workflow = ProjectWorkflow(root, store, drafter or make_drafter(get_index))
    active, workers = set(), set()
    static_dir = static_dir or root / "research_assistant/web/static"

    async def body(request):
        origin = request.headers.get("origin")
        if (origin and origin.rstrip("/") != str(request.base_url).rstrip("/")) or request.headers.get("sec-fetch-site") == "cross-site":
            raise PermissionError("不允许跨站修改项目。")
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

    def revision(data):
        value = data.pop("revision", None)
        if type(value) is not int or value < 1:
            raise ValueError("缺少有效项目版本，请刷新。")
        return value

    def public_run(run):
        result = {k: v for k, v in run.items() if k not in {"project", "source_versions"}}
        result["active"] = run["id"] in active
        result["display_phase"] = "interrupted" if run["phase"] in {"queued", "drafting", "checking"} and not result["active"] else run["phase"]
        return result

    def detail(project_id):
        result = store.detail(project_id)
        result["proposals"] = [public_run(r) for r in result["proposals"]]
        return result

    async def worker(proposal_id, decision):
        try:
            await run_in_threadpool(workflow.execute, proposal_id, decision)
        except Exception as exc:
            # No request text, model response or credentials in diagnostics.
            logging.getLogger(__name__).warning("project_workflow_failed id=%s type=%s", proposal_id, type(exc).__name__)
            store.mark(proposal_id, phase="failed", error=f"工作流未完成（{type(exc).__name__}）。请检查模型连接和资料，重试或放弃；任务未自动应用。")
        finally:
            active.discard(proposal_id)

    def launch(proposal_id, decision=None):
        if proposal_id in active:
            raise Conflict("工作流正在执行，请稍候。")
        active.add(proposal_id)
        task = asyncio.create_task(worker(proposal_id, decision))
        workers.add(task)
        task.add_done_callback(workers.discard)

    def endpoint(fn):
        async def wrapped(request):
            try:
                return await fn(request)
            except PermissionError as exc:
                return JSONResponse({"error": str(exc)}, status_code=403)
            except KeyError:
                return JSONResponse({"error": "项目、任务或工作流不存在。"}, status_code=404)
            except Conflict as exc:
                return JSONResponse({"error": str(exc)}, status_code=409)
            except ValidationError:
                return JSONResponse({"error": "项目或任务字段不符合要求，请检查名称、工时和文本长度。"}, status_code=400)
            except (ValueError, TypeError) as exc:
                return JSONResponse({"error": str(exc)}, status_code=400)
        return wrapped

    async def home(_):
        return FileResponse(static_dir / "projects.html")

    @endpoint
    async def projects(request):
        if request.method == "POST":
            data = await body(request)
            await run_in_threadpool(source_versions, root, data.get("sources", []))
            return JSONResponse({"project": store.create(data)}, status_code=201)
        return JSONResponse({"projects": store.list()})

    @endpoint
    async def project(request):
        project_id = request.path_params["project_id"]
        if request.method == "POST":
            data = await body(request)
            rev = revision(data)
            await run_in_threadpool(source_versions, root, data.get("sources", []))
            store.update(project_id, rev, data)
        return JSONResponse(detail(project_id))

    @endpoint
    async def tasks(request):
        data = await body(request)
        rev, project_id = revision(data), request.path_params["project_id"]
        if "task_id" in request.path_params:
            if set(data) != {"status", "progress_note"}:
                raise ValueError("只能修改任务状态与进展备注。")
            store.update_task(project_id, rev, request.path_params["task_id"], data["status"], data["progress_note"])
        else:
            store.add_task(project_id, rev, data)
        return JSONResponse(detail(project_id))

    @endpoint
    async def propose(request):
        data = await body(request)
        if data.get("allow_external") is not True:
            raise PermissionError("请先允许将项目目标、记忆、任务进展和所选文献片段发送给 DeepSeek。")
        rev, project_id = revision(data), request.path_params["project_id"]
        p = store.get(project_id)
        versions = await run_in_threadpool(source_versions, root, p["sources"])
        run = store.reserve(project_id, rev, data.get("request"), versions)
        launch(run["id"])
        return JSONResponse(public_run(run), status_code=202)

    @endpoint
    async def proposal(request):
        proposal_id = request.path_params["proposal_id"]
        run = store.proposal(proposal_id)
        if request.method == "POST":
            data = await body(request)
            action = data.get("action")
            if action not in {"approve", "reject", "retry", "discard"}:
                raise ValueError("无效的工作流操作。")
            if proposal_id in active:
                raise Conflict("工作流正在执行，请稍候。")
            if run["phase"] in TERMINAL_PHASES:
                return JSONResponse(public_run(run))
            if action == "discard":
                return JSONResponse(public_run(store.finish(proposal_id, "reject", {})))
            if action == "retry":
                if run["phase"] == "awaiting_approval":
                    raise Conflict("草案正在等待审批，无需重复生成。")
                if data.get("allow_external") is not True:
                    raise PermissionError("重试可能调用模型，需要重新确认外发权限。")
                p = store.get(run["project_id"])
                if p["revision"] != run["base_revision"] or p["archived"]:
                    return JSONResponse(public_run(store.mark(proposal_id, phase="stale", error="项目版本已变化，请重新规划。")))
                launch(proposal_id)
            else:
                if run["phase"] != "awaiting_approval":
                    raise Conflict("此工作流不在待审批状态。")
                launch(proposal_id, action)
            return JSONResponse(public_run(store.proposal(proposal_id)), status_code=202)
        return JSONResponse(public_run(run))

    return [Route("/projects", home), Route("/api/projects", projects, methods=["GET", "POST"]),
            Route("/api/projects/{project_id}", project, methods=["GET", "POST"]),
            Route("/api/projects/{project_id}/tasks", tasks, methods=["POST"]),
            Route("/api/projects/{project_id}/tasks/{task_id}", tasks, methods=["POST"]),
            Route("/api/projects/{project_id}/proposals", propose, methods=["POST"]),
            Route("/api/project-proposals/{proposal_id}", proposal, methods=["GET", "POST"])]
