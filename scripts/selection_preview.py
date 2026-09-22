"""Isolated browser fixture: synthetic PDF, mock provider, no external requests."""
import argparse
from pathlib import Path
import re
import sys
import shutil
import uvicorn
from starlette.applications import Starlette
from starlette.responses import FileResponse
from starlette.routing import Route, Mount
from starlette.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from research_assistant.web.reading_api import ReadingAPI
from research_assistant.translation.selection import SelectionTranslator
from research_assistant.translation import selection as module

parser = argparse.ArgumentParser()
parser.add_argument("--run", required=True)
args = parser.parse_args()
if not re.fullmatch(r"[A-Za-z0-9_-]{1,60}", args.run):
    parser.error("invalid isolated run name")
QA = ROOT / "output/selection-browser" / args.run
(QA / "library").mkdir(parents=True, exist_ok=True)
shutil.copy2(ROOT / "output/pdf/reading-sample/scientific-en.pdf", QA / "library/scientific-en.pdf")
module.configuration = lambda root: {"enabled": True, "key": "fixture-only", "model": "fixture-no-network", "base_url": "https://example.invalid/v1"}


def translate(config, text):
    return {"text": "这是浏览器验收使用的模拟译文：科研证据需要核对原文。它不是一次真实模型翻译。", "finish_reason": "stop",
            "usage": {"prompt_tokens": 80, "completion_tokens": 20, "total_tokens": 100}}


api = ReadingAPI(QA)
api._selection_translator = SelectionTranslator(api.store, translate)
api.register("scientific-en.pdf")


async def reader(request):
    return FileResponse(ROOT / "research_assistant/web/static/reader/index.html")


routes = [Route("/reader", reader),
          Mount("/static", StaticFiles(directory=ROOT / "research_assistant/web/static")),
          Mount("/reader-assets", StaticFiles(directory=ROOT / ".reader_assets/node_modules/pdfjs-dist"))]
routes.extend(api.routes()[1:])
app = Starlette(routes=routes, lifespan=api.lifespan)
if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8767)
