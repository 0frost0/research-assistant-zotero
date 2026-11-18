"""Structural source-label gate, not a semantic teacher-quality evaluation."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmarks.evaluate_setr import read_predictions


def audit(data, predictions, min_valid_rate=0.95):
    if data.get("schema") != "setr-style-source-v1" or data.get("role") == "external_test_only":
        raise ValueError("teacher audit requires frozen source, never external test")
    if not 0 <= min_valid_rate <= 1:
        raise ValueError("invalid threshold")
    splits = {}
    for split in ("train", "dev"):
        rows = [predictions[g["id"]] for g in data["groups"] if g["split"] == split]
        valid = [r for r in rows if r["status"] == "ok"]
        splits[split] = {"total": len(rows), "status": dict(Counter(r["status"] for r in rows)),
                         "valid_rate": len(valid) / len(rows) if rows else 0,
                         "selected_count_histogram": dict(Counter(len(r["selected_indices"]) for r in valid))}
    return {"kind": "structural source-label gate; not semantic quality or gold coverage",
            "min_valid_rate": min_valid_rate, "splits": splits,
            "passed": all(s["total"] > 0 and s["valid_rate"] >= min_valid_rate for s in splits.values()),
            "notes": ["All frozen source IDs and input fingerprints checked before audit.",
                      "Invalid labels remain failures and are excluded from student targets.",
                      "Passing syntax does not prove evidence support, completeness or lack of hallucination."]}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dataset", type=Path, required=True)
    p.add_argument("--teacher", type=Path, action="append", required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--min-valid-rate", type=float, default=0.95)
    a = p.parse_args()
    if a.output.exists():
        raise FileExistsError(a.output)
    data = json.loads(a.dataset.read_bytes())
    predictions = read_predictions(a.teacher, data["groups"])
    result = audit(data, predictions, a.min_valid_rate)
    result["dataset_sha256"] = hashlib.sha256(a.dataset.read_bytes()).hexdigest()
    result["teacher_sha256"] = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in a.teacher}
    with a.output.open("x", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    print(json.dumps(result, indent=2), flush=True)
    if not result["passed"]:
        raise SystemExit("Teacher syntax failure rate exceeds fixed gate; student training blocked")


if __name__ == "__main__":
    main()
