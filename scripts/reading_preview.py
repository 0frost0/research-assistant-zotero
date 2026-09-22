"""Isolated browser acceptance server. All notes live under output, not real data."""

from pathlib import Path
import asyncio
import argparse
import re
import shutil
import sys
import uvicorn
from starlette.applications import Starlette
from starlette.responses import FileResponse
from starlette.routing import Route, Mount
from starlette.staticfiles import StaticFiles

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from research_assistant.web.reading_api import ReadingAPI
from research_assistant.translation.service import validate_output, VERSIONS
from tests.test_reading_memory import pdf

parser = argparse.ArgumentParser()
parser.add_argument("--run", default="", help="Separate repeatable QA dataset name; use the same name after restart")
parser.add_argument("--budget-fixture", action="store_true", help="Mock full translation requests; never use the model")
options = parser.parse_args()
if options.run and not re.fullmatch(r"[A-Za-z0-9_-]{1,60}", options.run):
    parser.error("run must contain only letters, digits, underscore or hyphen")
QA = ROOT / "output/reading-browser"
if options.run:
    QA = QA / options.run
(QA / "library").mkdir(parents=True, exist_ok=True)
sample = ROOT / "output/pdf/reading-sample"
shutil.copy2(sample / "scientific-en.pdf", QA / "library/scientific-en.pdf")
pdf(QA / "library/rotated-cropped.pdf", rotate=90, crop=True)
api = ReadingAPI(QA)
paper = api.register("scientific-en.pdf")
if not any(a["kind"] == "translation" for a in paper["artifacts"]):
    real = sample / "real/scientific-en.no_watermark.zh.mono.pdf"
    validation = validate_output(sample / "scientific-en.pdf", real)
    api.store.add_translation(
        paper["id"],
        paper["original_id"],
        real,
        {"versions": VERSIONS, "sample": "real engine output"},
        validation,
    )

if options.budget_fixture:
    from research_assistant.translation import service as service_module
    from research_assistant.translation.ledger import metered_create
    from types import SimpleNamespace
    service_module.configuration = lambda root: {"configured": True, "enabled": True, "installed": True,
        "python": sys.executable, "key": "mock-only", "model": "fixture-no-network", "base_url": "https://example.invalid/v1"}

    async def simulate(job):
        service = api.translator
        service.ledger.start(job["id"], job["attempt"])
        result = SimpleNamespace(usage=SimpleNamespace(model_dump=lambda: {"prompt_tokens": 70, "completion_tokens": 30, "total_tokens": 100}),
            choices=[SimpleNamespace(finish_reason="stop", message=SimpleNamespace(content="模拟译文"))])
        metered_create(lambda **kw: result, service.ledger, job["id"], job["attempt"])(messages=[{"role": "user", "content": "synthetic fixture"}])
        for index in range(12):
            await asyncio.sleep(.3)
            if job["id"] in service.cancel_requested:
                raise ValueError("模拟任务已停止，未发真实模型请求。")
            api.store.update_job(job["id"], stage="模拟翻译与排版", progress=index * 8)
        real = sample / "real/scientific-en.no_watermark.zh.mono.pdf"
        a = api.store.add_translation(job["paper_id"], job["original_id"], real,
            {"fixture": "previous synthetic artifact reused; no live call"}, validate_output(sample / "scientific-en.pdf", real))
        api.store.update_job(job["id"], state="succeeded", stage="模拟完成，未调用模型", progress=100, artifact_id=a["id"])
    api.translator.execute = simulate


async def reader(request):
    return FileResponse(ROOT / "research_assistant/web/static/reader/index.html")


routes = [
    Route("/reader", reader),
    Mount("/static", StaticFiles(directory=ROOT / "research_assistant/web/static")),
    Mount(
        "/reader-assets",
        StaticFiles(directory=ROOT / ".reader_assets/node_modules/pdfjs-dist"),
    ),
]
routes.extend(api.routes()[1:])
app = Starlette(routes=routes, lifespan=api.lifespan)
if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8766)
