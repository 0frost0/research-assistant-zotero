"""Live checks on public synthetic fixtures only. Never evaluates private papers.

Produces separate real-engine and real-model results in output/reading-live.
"""

import asyncio
import json
import os
from pathlib import Path
import sys
import time
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from research_assistant.memory.store import ReadingStore
from research_assistant.memory.qa import answer, draft
from research_assistant.translation.service import TranslationService


async def main():
    for path in (ROOT / ".env", ROOT.parent / ".env", ROOT.parent / "langchain/.env"):
        if path.exists():
            load_dotenv(path)
            break
    os.environ["RESEARCH_TRANSLATION_PYTHON"] = str(
        ROOT / ".translation_env/Scripts/python.exe"
    )
    os.environ["RESEARCH_TRANSLATION_ENABLED"] = "true"
    os.environ["LANGSMITH_TRACING"] = "false"
    output = ROOT / "output/reading-live"
    store = ReadingStore(output)
    paper = store.register(
        ROOT / "output/pdf/reading-sample/scientific-en.pdf", "synthetic-scientific.pdf"
    )
    service = TranslationService(store)
    await service.start()
    report = {
        "sample": "synthetic CC0 double-column PDF, not a private paper",
        "human_confirmation": "simulated by this acceptance script",
    }
    try:
        job, created = service.submit(paper["id"])
        if job["state"] == "failed":
            job = service.retry(job["id"])
        print(
            json.dumps({"job": job["id"], "created": created, "state": job["state"]}),
            flush=True,
        )
        start = time.monotonic()
        peak = 0
        previous = None
        while job["state"] in {"queued", "running"}:
            await asyncio.sleep(0.5)
            job = store.job(job["id"])
            if job["stage"] != previous:
                previous = job["stage"]
                print(
                    json.dumps(
                        {"stage": previous, "progress": job["progress"]},
                        ensure_ascii=False,
                    ),
                    flush=True,
                )
            if service.process:
                try:
                    import psutil

                    p = psutil.Process(service.process.pid)
                    rss = sum(
                        x.memory_info().rss
                        for x in [p] + p.children(recursive=True)
                        if x.is_running()
                    )
                    peak = max(peak, rss)
                except (ImportError, OSError):
                    pass
        report["translation"] = {
            "job_id": job["id"],
            "state": job["state"],
            "validation": job.get("validation"),
            "error": job.get("error"),
            "wall_seconds": round(time.monotonic() - start, 2),
            "peak_process_tree_rss_mib": round(
                max(peak, job.get("peak_process_tree_rss_bytes", 0)) / 1024**2, 1
            ),
        }
        if "--translation-only" in sys.argv:
            previous = (
                json.loads((output / "report.json").read_text(encoding="utf-8"))
                if (output / "report.json").exists()
                else {}
            )
            report["memory"] = previous.get("memory", {"status": "not_run"})
            (output / "report.json").write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
            return
        original = store.artifact(paper["original_id"])
        anchor = {
            "artifact_id": original["id"],
            "artifact_hash": original["hash"],
            "page": 1,
            "coordinate_system": "pdf_user_space",
            "view_box": original["metadata"]["pages"][0]["view_box"],
            "rects": [],
            "excerpt": "Personal notes are not equivalent to experimental results. Manual review is required for formulas, units and translated tables.",
        }
        try:
            n = await asyncio.to_thread(
                draft,
                store,
                {
                    "paper_id": paper["id"],
                    "body": "请用中文写一条理解草稿",
                    "anchors": [anchor],
                    "allow_external": True,
                },
            )
            before = store.search("原文公式核对", paper["id"])
            n = store.save_note(
                {
                    "expected_revision": n["revision"],
                    "confirm": True,
                    "body": "我的理解：个人笔记不等于论文结论，公式和单位必须核对原文。",
                },
                n["id"],
            )
            response = await asyncio.to_thread(
                answer,
                store,
                {
                    "paper_id": paper["id"],
                    "question": "我对公式和单位核对有什么理解",
                    "scope": "notes",
                    "allow_external": True,
                },
            )
            report["memory"] = {
                "draft_created": True,
                "draft_confirmed_initially": False,
                "draft_excluded": not before,
                "confirmed_author_source": n["author_source"],
                "answer_status": response["status"],
                "note_citation_revisions": [
                    x["revision"] for x in response["note_citations"]
                ],
                "original_citations": len(response["original_citations"]),
                "warnings": response["warnings"],
            }
        except Exception as exc:
            report["memory"] = {"status": "failed", "error_type": type(exc).__name__}
        (output / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    finally:
        await service.stop()


if __name__ == "__main__":
    asyncio.run(main())
