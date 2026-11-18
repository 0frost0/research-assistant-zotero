"""Run the Python reference API on an explicit root for migration comparison."""
import argparse
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--port", type=int, default=8776)
    a = p.parse_args()
    os.environ.setdefault("RESEARCH_RERANKER_URL", "http://127.0.0.1:30002/rerank")
    import uvicorn
    from starlette.applications import Starlette
    from starlette.routing import Mount
    from starlette.staticfiles import StaticFiles
    from research_assistant.web.knowledge_api import KnowledgeAPI
    from research_assistant.core.paths import STATIC_DIR
    api = KnowledgeAPI(a.root.resolve(), backend="multimodal")
    app = Starlette(routes=api.routes() + [Mount("/static", StaticFiles(directory=STATIC_DIR))])
    uvicorn.run(app, host="127.0.0.1", port=a.port, log_level="warning")
