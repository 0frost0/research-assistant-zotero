"""Synthetic, isolated browser acceptance server. Never loads the production app."""
import argparse
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import uvicorn
from starlette.applications import Starlette
from starlette.routing import Mount
from starlette.staticfiles import StaticFiles
from research_assistant.web.knowledge_api import KnowledgeAPI
from research_assistant.core.paths import STATIC_DIR
from research_assistant.knowledge.store import digest
from tests.test_reading_memory import pdf

parser = argparse.ArgumentParser()
parser.add_argument("--port", type=int, default=8771)
args = parser.parse_args()
temporary = tempfile.TemporaryDirectory(prefix="research-knowledge-fixture-")
root = Path(temporary.name)
api = KnowledgeAPI(root, token="synthetic-acceptance-token", backend="tfidf")
source = root / "synthetic.pdf"
pdf(source)
content = source.read_bytes()
sha = digest(content)
api.store.put_object(sha, content)
for key, name, entities in [
    ("A", "检索方法", [{"key": "PDF00001", "kind": "document", "title": "Scientific Evidence", "hash": sha},
                    {"key": "NOTE0001", "kind": "note", "title": "阅读记录", "body": "检索结果必须回到原文核验。Scientific evidence requires validation."}]),
    ("B", "长期研究计划与跨学科证据评估", [{"key": "NOTE0002", "kind": "note", "title": "独立笔记", "body": "独立库只讨论时间安排与实验记录。"}]),
]:
    base = api.store.create({"connection": "fixture", "library": "user:0", "collection": key, "name": name})
    api.store.sync(base["id"], {"name": name, "include_children": True, "complete": True,
                               "entities": entities, "expected_revision": 0})
app = Starlette(routes=api.routes() + [Mount("/static", StaticFiles(directory=STATIC_DIR))])
if __name__ == "__main__":
    try:
        uvicorn.run(app, host="127.0.0.1", port=args.port, log_level="warning")
    finally:
        temporary.cleanup()
