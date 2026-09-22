"""Reproducible lexical-memory evaluation, entirely local and temporary."""

import json
from pathlib import Path
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from research_assistant.memory.store import ReadingStore
from tests.test_reading_memory import pdf


def evaluate():
    cases = json.loads(
        (ROOT / "benchmarks/reading/notes_zh.json").read_text(encoding="utf-8")
    )
    with tempfile.TemporaryDirectory() as folder:
        root = Path(folder)
        p = root / "fixture.pdf"
        pdf(p)
        store = ReadingStore(root)
        paper = store.register(p, "fixture.pdf")
        artifact = store.artifact(paper["original_id"])
        anchor = {
            "artifact_id": artifact["id"],
            "artifact_hash": artifact["hash"],
            "page": 1,
            "coordinate_system": "pdf_user_space",
            "view_box": artifact["metadata"]["pages"][0]["view_box"],
            "rects": [],
            "excerpt": "Scientific evidence from original paper.",
        }
        keys = {}
        for item in cases["notes"]:
            note = store.save_note(
                {"paper_id": paper["id"], "body": item["body"], "anchors": [anchor]}
            )
            keys[note["id"]] = item["key"]
        results = []
        for q in cases["queries"]:
            start = time.perf_counter()
            hits = store.search(q["query"], paper["id"], k=3)
            elapsed = (time.perf_counter() - start) * 1000
            retrieved = [keys[n["id"]] for n in hits]
            expected = q["expected"]
            rank = retrieved.index(expected) + 1 if expected in retrieved else None
            results.append(
                {
                    **q,
                    "retrieved": retrieved,
                    "hit_at_3": rank is not None if expected else None,
                    "reciprocal_rank": 1 / rank if rank else 0,
                    "correct_abstention": not hits if expected is None else None,
                    "latency_ms": round(elapsed, 3),
                }
            )
        groups = {}
        for group in ("keyword", "fuzzy", "no_answer"):
            rows = [r for r in results if r["category"] == group]
            groups[group] = {
                "count": len(rows),
                "successes": sum(
                    r["correct_abstention"] if group == "no_answer" else r["hit_at_3"]
                    for r in rows
                ),
            }
        report = {
            "dataset": "benchmarks/reading/notes_zh.json",
            "retrieval": "Chinese character 2/3-gram + English words; overlap threshold .08",
            "k": 3,
            "groups": groups,
            "results": results,
            "limitations": "小样本有明确词面局限；无答案指标衡量候选检索拒答，不是模型回答忠实度。未用本组失败案例调参。",
        }
        output = ROOT / "output/reading-evaluation"
        output.mkdir(parents=True, exist_ok=True)
        (output / "notes-report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    evaluate()
