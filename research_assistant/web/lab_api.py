"""Local evaluation/review and structured table endpoints, separate from answer generation."""

import asyncio
import json
import threading
from pathlib import Path

from starlette.concurrency import run_in_threadpool
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route

from research_assistant.evaluation.retrieval_evaluation import EvaluationStore, run_evaluation, validate_dataset
from research_assistant.storage.table_store import TableStore


def make_lab_routes(root: Path, get_index, safe_library_path):
    tables = TableStore(root / ".data/tables.sqlite3")
    evaluations = EvaluationStore(root / ".data/evaluations.sqlite3")
    table_lock = threading.Lock()
    job = {"status": "idle"}
    tasks = set()
    dataset_path = root / "benchmarks/multimodal_eval_v1.json"

    def sync_tables():
        with table_lock:
            return tables.sync(root / "library")

    async def body(request):
        origin = request.headers.get("origin")
        if origin and origin.rstrip("/") != str(request.base_url).rstrip("/"):
            raise ValueError("Cross-origin changes are not allowed")
        if "application/json" not in request.headers.get("content-type", ""):
            raise ValueError("JSON request required")
        raw = await request.body()
        if len(raw) > 20000:
            raise ValueError("Request exceeds 20 KB")
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("JSON object required")
        return data

    async def home(_):
        return FileResponse(root / "research_assistant/web/static/lab.html")

    async def table_list(request):
        await run_in_threadpool(sync_tables)
        try:
            offset = max(0, int(request.query_params.get("offset", "0")))
        except ValueError:
            return JSONResponse({"error": "Invalid pagination offset"}, status_code=400)
        rows = await run_in_threadpool(tables.search, request.query_params.get("q", "")[:500],
                                       request.query_params.get("source"), 51, offset)
        return JSONResponse({"tables": rows[:50], "stats": tables.stats(), "offset": offset, "has_more": len(rows) > 50})

    async def table_detail(request):
        await run_in_threadpool(sync_tables)
        item = tables.get(request.path_params["table_id"])
        return JSONResponse(item or {"error": "Table not found or source changed"}, status_code=200 if item else 404)

    async def table_sql(request):
        try:
            data = await body(request)
            await run_in_threadpool(sync_tables)
            result = await run_in_threadpool(tables.query, str(data.get("sql", "")))
            return JSONResponse(result)
        except (ValueError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    async def pdf(request):
        try:
            path = safe_library_path(request.query_params.get("source", ""))
            if path.suffix.lower() not in {".pdf", ".md", ".txt"} or not path.is_file():
                raise ValueError("Document not found")
            media = "application/pdf" if path.suffix.lower() == ".pdf" else "text/plain; charset=utf-8"
            return FileResponse(path, media_type=media, filename=path.name, content_disposition_type="inline")
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=404)

    async def table_image(request):
        await run_in_threadpool(sync_tables)
        item = tables.get(request.path_params["table_id"])
        path = Path(item["image_path"]).resolve() if item and item.get("image_path") else None
        if not path or not path.is_relative_to((root / ".cache/mineru").resolve()) or not path.is_file():
            return JSONResponse({"error": "Image not available"}, status_code=404)
        return FileResponse(path)

    async def evaluation_state(_):
        dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
        return JSONResponse({"job": dict(job), "runs": evaluations.list_runs(),
                             "dataset_id": dataset["id"], "case_count": len(dataset["cases"])})

    def evaluate_worker(dataset):
        validate_dataset(dataset, root)
        index = get_index("multimodal")
        def progress(done, total):
            job.update(done=done, total=total)
        run = run_evaluation(root, index, dataset, progress)
        evaluations.save(run)
        return run["run_id"]

    async def evaluate_background(dataset):
        try:
            run_id = await run_in_threadpool(evaluate_worker, dataset)
            job.update(status="complete", run_id=run_id)
        except Exception as exc:
            job.update(status="failed", error=str(exc) if isinstance(exc, ValueError) else
                       f"{type(exc).__name__}: evaluation failed; check Qwen tunnel and Qdrant")

    async def evaluation_start(request):
        try:
            await body(request)
            if job["status"] == "running":
                return JSONResponse({"error": "An evaluation is already running", "job": job}, status_code=409)
            dataset = json.loads(dataset_path.read_text(encoding="utf-8"))
            job.clear()
            job.update(status="running", done=0, total=len(dataset["cases"]))
            task = asyncio.create_task(evaluate_background(dataset))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
            return JSONResponse({"job": job}, status_code=202)
        except (ValueError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    async def evaluation_run(request):
        try:
            run_id = request.path_params["run_id"]
            if request.method == "POST":
                result = evaluations.judge(run_id, await body(request))
            else:
                result = evaluations.get(run_id)
            return JSONResponse(result)
        except (ValueError, TypeError) as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    return [Route("/lab", home), Route("/api/tables", table_list),
            Route("/api/tables/sql", table_sql, methods=["POST"]),
            Route("/api/tables/{table_id}/image", table_image), Route("/api/tables/{table_id}", table_detail),
            Route("/api/document", pdf), Route("/api/evaluations", evaluation_state),
            Route("/api/evaluations", evaluation_start, methods=["POST"]),
            Route("/api/evaluations/{run_id}", evaluation_run, methods=["GET", "POST"])]
