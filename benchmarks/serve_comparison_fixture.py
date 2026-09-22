"""Isolated browser QA: synthetic papers and async agent fixtures, temporary storage."""

import argparse
import asyncio
import sys
import tempfile
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from research_assistant.web.comparison_api import make_comparison_routes
from tests.test_comparison import documents, fixture_caller


async def simulated_agent(role, payload):
    await asyncio.sleep(2 if role == "analyst" else 1)
    return await fixture_caller(role, payload)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="research-comparison-ui-") as temporary:
        root = Path(temporary)
        (root / "library").mkdir()
        for doc in documents():
            (root / "library" / doc["source"]).write_text("Synthetic browser test document", encoding="utf-8")
        async def home(_):
            return FileResponse(ROOT / "research_assistant/web/static/index.html")
        async def library(_):
            return JSONResponse({"files": [{"name": d["source"], "size": 32, "type": "TXT", "mineru_cached": False} for d in documents()]})
        async def status(_):
            return JSONResponse({"model": "模拟 Agent（隔离测试）", "files": 2, "model_configured": True})
        def options(data):
            return {"sources": data["sources"], "backend": data.get("backend", "tfidf"), "k": data.get("k", 5), "file_types": None, "min_score": .1}
        def collect(q, o):
            return documents()
        routes = [Route("/", home), Route("/api/library", library), Route("/api/status", status)]
        routes.extend(make_comparison_routes(root, None, options, caller=simulated_agent, collector=collect))
        routes.append(Mount("/static", StaticFiles(directory=ROOT / "research_assistant/web/static")))
        print("ISOLATED COMPARISON QA: synthetic documents, no model fees, no live indexes.", flush=True)
        uvicorn.run(Starlette(routes=routes), host="127.0.0.1", port=args.port)
