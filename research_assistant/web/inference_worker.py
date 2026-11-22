"""Private loopback primitives for the Go migration; no public product routes.

Model calls and PDF extraction are Python responsibilities. Retrieval primitives
temporarily reuse the existing index implementation while Go owns task control.
"""
import argparse
import asyncio
import os
from pathlib import Path
import secrets

from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route

from research_assistant.core.service_context import stored_service_context
from research_assistant.knowledge.store import KnowledgeStore
from research_assistant.knowledge.service import KnowledgeService, IndexBuilding
from research_assistant.knowledge.qa import ModelCaller


def create_worker(root, token):
    if not token or not token.strip():
        raise ValueError("worker_token_required")
    store = KnowledgeStore(root)
    service = KnowledgeService(store, "multimodal")

    async def dispatch(request, raw):
        import json
        try:
            data = json.loads(raw)
            operation, payload = request.path_params["operation"], data["payload"]
            with stored_service_context(root, data["connection"], allow_missing=operation == "search"):
                if operation == "search":
                    result = await asyncio.to_thread(service.search, payload)
                elif operation == "model":
                    value, usage = await ModelCaller()(data["role"], payload, min(data.get("timeout", 45), 45))
                    result = {"value": value.model_dump() if hasattr(value, "model_dump") else value, "usage": usage}
                elif operation == "comparison-model":
                    from research_assistant.agents.comparison_workflow import call_model
                    reply = await asyncio.wait_for(call_model(data["role"], payload), 35)
                    result = {"value": reply.value.model_dump() if hasattr(reply.value, "model_dump") else reply.value,
                              "usage": reply.usage, "invalid": reply.parsing_error is not None}
                elif operation == "extract":
                    import base64
                    import tempfile
                    from research_assistant.ingestion.pdf_extractor import PDFExtractor
                    content = base64.b64decode(payload["content"], validate=True)
                    if len(content) > 25 * 1024 * 1024 or not content.startswith(b"%PDF-"):
                        raise ValueError("invalid_pdf")
                    with tempfile.TemporaryDirectory(prefix="research-extract-") as tmp:
                        path = Path(tmp) / "input.pdf"
                        path.write_bytes(content)
                        docs, report = await asyncio.to_thread(PDFExtractor(enable_ocr=False).extract, path, source=payload["hash"] + ".pdf")
                    result = {"pages": [{"text": d.page_content, "page": d.metadata.get("page")} for d in docs], "report": report.model_dump()}
                elif operation == "parse":
                    from research_assistant.ingestion.mineru_extractor import MinerUExtractor
                    sha, config = payload["hash"], payload["config"]
                    if not store.object_exists(sha):
                        raise ValueError("missing_pdf")
                    parser = MinerUExtractor(cache_dir=store.home / "mineru" / payload["profile"],
                        python_executable=Path(config["python"]) if config.get("python") else None,
                        server_url=config.get("url") or None, timeout_seconds=config.get("timeout", 1800), mode=config["mode"])
                    documents, report = await asyncio.to_thread(parser.parse, store.objects / (sha + ".pdf"), source=sha + ".pdf")
                    if not documents:
                        raise ValueError("empty_parse")
                    result = {"report": report, "blocks": [{"text": d.page_content, "metadata": d.metadata} for d in documents], "profile": payload["profile"]}
                else:
                    return JSONResponse({"error": "unknown_operation"}, status_code=404)
            return JSONResponse(result)
        except IndexBuilding:
            return JSONResponse({"status": "indexing", "results": [], "retry_after": 2}, status_code=202)
        except Exception as exc:
            # Exceptions may contain provider URLs/credentials or private text.
            return JSONResponse({"error": type(exc).__name__}, status_code=503)

    async def endpoint(request):
        if not secrets.compare_digest(request.headers.get("authorization", ""), "Bearer " + token):
            return JSONResponse({"error": "unauthorized"}, status_code=401)
        # Read the body before installing the disconnect watcher: two consumers
        # must never race on ASGI receive. HTTP cancellation stops async model
        # calls; already-running native OCR threads remain bounded by its timeout.
        raw = bytearray()
        async for part in request.stream():
            raw.extend(part)
            if len(raw) > 35 * 1024 * 1024:
                return JSONResponse({"error": "too_large"}, status_code=413)

        async def disconnected():
            while True:
                message = await request.receive()
                if message["type"] == "http.disconnect":
                    return

        task = asyncio.create_task(dispatch(request, raw))
        watcher = asyncio.create_task(disconnected())
        try:
            done, _ = await asyncio.wait({task, watcher}, return_when=asyncio.FIRST_COMPLETED)
            if task in done:
                return await task
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            return JSONResponse({"error": "cancelled"}, status_code=499)
        finally:
            watcher.cancel()
            if not task.done():
                task.cancel()
            await asyncio.gather(watcher, task, return_exceptions=True)

    return Starlette(routes=[Route("/internal/{operation}", endpoint, methods=["POST"])])


if __name__ == "__main__":
    import uvicorn
    p = argparse.ArgumentParser()
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--token-file", type=Path, required=True)
    p.add_argument("--port", type=int, default=8778)
    a = p.parse_args()
    os.environ.setdefault("RESEARCH_RERANKER_URL", "http://127.0.0.1:30002/rerank")
    uvicorn.run(create_worker(a.root.resolve(), a.token_file.read_text().strip()), host="127.0.0.1", port=a.port, log_level="warning")
