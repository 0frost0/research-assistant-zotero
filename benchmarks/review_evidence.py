"""Read-only review packet; compile explicitly authored AI decisions, never auto-grade."""

import argparse
import json
import sqlite3
import sys
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from research_assistant.evaluation.retrieval_evaluation import EvaluationStore


def review_packet(run):
    pool, cases = {}, []
    for case in run["cases"]:
        ordered = {}
        for mode in ("raw", "displayed"):
            for rank, hit in enumerate(case["rankings"][mode], 1):
                pid = hit["point_id"]
                if pid not in pool:
                    pool[pid] = {"alias": f"E{len(pool) + 1:03d}", **hit}
                ordered.setdefault(pid, {"alias": pool[pid]["alias"], "point_id": pid})[mode] = rank
        cases.append({**case, "review_items": list(ordered.values())})
    return list(pool.values()), cases


def compile_decisions(run, decisions):
    from research_assistant.evaluation.retrieval_evaluation import review_snapshot_hash
    if decisions["run_id"] != run["run_id"] or decisions["snapshot_hash"] != review_snapshot_hash(run):
        raise ValueError("Decision sheet belongs to a different immutable run")
    pool, cases = review_packet(run)
    known = {c["id"] for c in cases}
    if set(decisions["cases"]) != known:
        raise ValueError("Each case must have an explicit review")
    judgments, reviews = [], []
    for case in cases:
        decision = decisions["cases"][case["id"]]
        labels = decision["labels"]
        if len(labels) != len(case["review_items"]):
            raise ValueError(f"Wrong number of labels for {case['id']}")
        for item, label in zip(case["review_items"], labels, strict=True):
            alias, reason = label.split(":", 1)
            if alias != item["alias"] or reason not in decisions["rubric"]:
                raise ValueError(f"Unknown evidence or reason in {case['id']}: {label}")
            rubric = decisions["rubric"][reason]
            judgments.append({"case_id": case["id"], "point_id": item["point_id"],
                              "grade": rubric["grade"], "note": rubric["note"],
                              "confidence": decision.get("confidence", "medium")})
        reviews.append({"case_id": case["id"], "status": decision["status"],
                        "note": decision["note"], "confidence": decision.get("confidence", "medium"),
                        "needs_human_review": decision["needs_human_review"]})
    return {"schema": 1, "annotation_origin": "ai_assisted", "reviewer": "Codex assistant",
            "run_id": run["run_id"], "snapshot_hash": review_snapshot_hash(run),
            "dataset_hash": run["dataset_hash"], "method": decisions["method"],
            "limitations": decisions["limitations"], "source_checks": decisions["source_checks"],
            "rubric": decisions["rubric"], "judgments": judgments, "case_reviews": reviews}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--run", default="723ea83a-4ef8-4c1b-b4a9-77c6f7296663")
    p.add_argument("--start", type=int, default=1)
    p.add_argument("--count", type=int, default=30)
    p.add_argument("--cases", action="store_true")
    p.add_argument("--compile", type=Path)
    p.add_argument("--validate", type=Path, help="Validate decisions without writing to the database")
    args = p.parse_args()
    # Inspection does not create tables or modify the evaluation database.
    with closing(sqlite3.connect((ROOT / ".data/evaluations.sqlite3").as_uri() + "?mode=ro", uri=True)) as con:
        row = con.execute("SELECT payload FROM runs WHERE run_id=?", (args.run,)).fetchone()
    if row is None:
        raise ValueError("Unknown evaluation run")
    run = json.loads(row[0])
    pool, cases = review_packet(run)
    if args.compile and args.validate:
        p.error("Choose --compile or --validate, not both")
    if args.compile or args.validate:
        from research_assistant.evaluation.retrieval_evaluation import ai_summary, validate_ai_review
        decisions = json.loads((args.compile or args.validate).read_text(encoding="utf-8"))
        package = compile_decisions(run, decisions)
        validate_ai_review(run, package)
        if args.validate:
            print(json.dumps(ai_summary(run["cases"], package["judgments"]), ensure_ascii=False, indent=2))
            sys.exit(0)
        output = args.compile.with_name(args.compile.stem + "_resolved.json")
        encoded = json.dumps(package, ensure_ascii=False, indent=2)
        if output.exists() and json.loads(output.read_text(encoding="utf-8")) != package:
            raise ValueError("Refusing to overwrite a different resolved review")
        EvaluationStore(ROOT / ".data/evaluations.sqlite3").import_ai_review(package)
        output.write_text(encoded, encoding="utf-8")
        print(json.dumps({"path": str(output), "judgments": len(package["judgments"]),
                          "cases": len(package["case_reviews"])}))
    elif args.cases:
        for c in cases:
            print(c["id"], c["question"] or "[image input]", "input=", c["input_modality"])
            print("EXPECTED", json.dumps(c["expected"], ensure_ascii=False))
            print("ORDER", " ".join(item["alias"] for item in c["review_items"]))
    else:
        for h in pool[args.start - 1:args.start - 1 + args.count]:
            print(f"\n{h['alias']} {h['source']} p.{h['page']} {h['modality']} {h['content_type']} {h['point_id']}")
            print(h["text"])
