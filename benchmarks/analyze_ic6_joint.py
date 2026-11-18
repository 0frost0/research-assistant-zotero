"""Post-evaluation paired fact accounting; never used by the selector.

Requires a complete joint report, not smoke or partially generated predictions.
Reads evaluator fact indices only; emits no gold spans, passage text or selections.
"""
import argparse
import json
import math
from pathlib import Path
from statistics import mean
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmarks.run_ic6_retrieval import load, sha, write_new
from benchmarks.setr_protocol import PROTOCOL


def compare_rows(base, joint):
    if (base["id"] != joint["id"] or base["gold_count"] != joint["gold_count"] or
            base["question_type"] != joint["question_type"]):
        raise ValueError("Paired row identity mismatch")
    n = base["gold_count"]

    def covered(row):
        indices = row["covered_gold_indices"]
        if (any(type(i) is not int or not 0 <= i < n for i in indices) or
                len(set(indices)) != len(indices) or row["covered_gold_count"] != len(indices) or
                (row["status"] != "ok" and indices) or
                (n and not math.isclose(row["ic_at_6"], len(indices)/n, abs_tol=1e-12))):
            raise ValueError("Invalid fact indices/count/IC or failed output")
        return set(indices)

    old, new = covered(base), covered(joint)
    gained, lost = len(new - old), len(old - new)
    delta = (gained - lost)/n if n else None
    return {"id": base["id"], "question_type": base["question_type"], "gold_count": n,
            "baseline_status": base["status"], "joint_status": joint["status"],
            "retained_facts": len(old & new), "gained_facts": gained, "lost_facts": lost,
            "gained_ic": gained/n if n else None, "lost_ic": lost/n if n else None,
            "delta_ic6": delta, "joint_failure_lost_ic": lost/n if n and joint["status"] != "ok" else (0 if n else None),
            "outcome": "null" if not n else "win" if delta > 0 else "loss" if delta < 0 else "tie",
            "equal_score_fact_swap": bool(n and delta == 0 and gained and lost)}


def summary(rows):
    real = [r for r in rows if r["gold_count"]]
    return {"questions": len(rows), "answerable": len(real), "null_questions": len(rows)-len(real),
            **{outcome: sum(r["outcome"] == outcome for r in real) for outcome in ("win", "tie", "loss")},
            "equal_score_fact_swaps": sum(r["equal_score_fact_swap"] for r in real),
            **{key: mean(r[key] for r in real) if real else None for key in
               ("gained_ic", "lost_ic", "delta_ic6", "joint_failure_lost_ic")}}


def analyze(report):
    if report["protocol"] != PROTOCOL or report.get("exploratory_not_independent_test") is not True:
        raise ValueError("Unexpected report protocol/role")
    by_arm = {arm: {} for arm in ("coverage_anchors", "joint_8b")}
    for row in report["rows"]:
        if row["arm"] not in by_arm or row["id"] in by_arm[row["arm"]]:
            raise ValueError("Unexpected arm/duplicate row")
        by_arm[row["arm"]][row["id"]] = row
    base, joint = by_arm.values()
    if not base or set(base) != set(joint):
        raise ValueError("Missing paired rows")
    for arm, values in by_arm.items():
        expected = report["summary"][arm]
        answerable = [r for r in values.values() if r["gold_count"]]
        if (len(values) != expected["questions"] or len(answerable) != expected["answerable"] or
                len(values)-len(answerable) != expected["null_questions"] or not answerable or
                not math.isclose(mean(r["ic_at_6"] for r in answerable), expected["ic_at_6"], abs_tol=1e-12)):
            raise ValueError("Partial report or summary mismatch")
    rows = [compare_rows(base[qid], joint[qid]) for qid in base]
    result = summary(rows)
    if not math.isclose(result["delta_ic6"], report["delta_ic6"], abs_tol=1e-12):
        raise ValueError("Paired aggregate delta changed")
    return {"version": "ic6-joint-paired-audit-v1", "source_experiment": report["version"],
            "role": "diagnostic_only_never_selection_or_training", "exploratory_not_independent_test": True,
            "summary": result, "slices": {t: summary([r for r in rows if r["question_type"] == t])
                                           for t in sorted({r["question_type"] for r in rows})},
            "notes": ["Gained IC minus lost IC equals the complete paired IC@6 difference.",
                      "A tied score may conceal replacement of one gold fact by another.",
                      "Joint failure lost IC is part of lost IC, not an additional penalty.",
                      "No gold text, original questions, passage text or selection IDs are emitted."], "rows": rows}


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--report", required=True, type=Path)
    p.add_argument("--output", required=True, type=Path)
    a = p.parse_args()
    result = analyze(load(a.report))
    result["fingerprints"] = {"report": sha(a.report), "script": sha(__file__)}
    write_new(a.output, result)
    print(json.dumps(result["summary"], indent=2))
