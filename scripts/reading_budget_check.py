"""Explicit, bounded real translation of ONLY our two-page synthetic fixture.

Outputs stay in output/full-translation-check, never in the production library.
No new version or automatic retries; reruns reuse the existing job.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from dotenv import load_dotenv
from research_assistant.memory.store import ReadingStore
from research_assistant.translation.service import TranslationService


async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="Allow paid translation of the CC0 two-page fixture")
    args = parser.parse_args()
    if not args.live:
        print("No model request. Use --live only after reviewing the 10000-token / 10-request limit.")
        return
    for env_path in (ROOT / ".env", ROOT.parent / ".env", ROOT.parent / "langchain/.env"):
        if env_path.exists():
            load_dotenv(env_path)
            break
    os.environ["RESEARCH_TRANSLATION_PYTHON"] = str(ROOT / ".translation_env/Scripts/python.exe")
    os.environ["RESEARCH_TRANSLATION_TIMEOUT"] = "600"
    qa = ROOT / "output/full-translation-check"
    (qa / "library").mkdir(parents=True, exist_ok=True)
    shutil.copy2(ROOT / "output/pdf/reading-sample/scientific-en.pdf", qa / "library/scientific-en.pdf")
    store = ReadingStore(qa)
    paper = store.register(qa / "library/scientific-en.pdf", "scientific-en.pdf")
    service = TranslationService(store)
    await service.start()
    try:
        job, created = service.submit(paper["id"], token_budget=10000, request_limit=10)
        previous = None
        while True:
            job = store.job(job["id"])
            state = (job["state"], job["stage"])
            if state != previous:
                print(json.dumps({"state": state[0], "stage": state[1]}, ensure_ascii=False), flush=True)
                previous = state
            if job["state"] in {"failed", "succeeded"}:
                report = {"sample": "CC0 synthetic two-page PDF", "created": created,
                          "state": job["state"], "error_code": job.get("error_code"),
                          "error": job.get("error"), "budget": service.ledger.summary(job["id"]),
                          "artifact": str(store.artifact_path(job["artifact_id"])) if job.get("artifact_id") else None,
                          "validation": job.get("validation")}
                print(json.dumps(report, ensure_ascii=False), flush=True)
                break
            await asyncio.sleep(1)
    finally:
        await service.stop()


if __name__ == "__main__":
    asyncio.run(main())
