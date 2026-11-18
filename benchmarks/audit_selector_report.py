"""Recompute set metrics, alternative evidence F1 and paired paper bootstrap.

Read-only post-hoc audit. No model calls, labels changed, checkpoint selection,
or raw paper excerpts in output. Bootstrap is diagnostic on this filtered set.
"""
import argparse
from collections import Counter, defaultdict
import hashlib
import json
from pathlib import Path
import random
from statistics import mean
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmarks.evaluate_evidence_selector import set_metrics
from research_assistant.retrieval.selector_dataset import load_dataset


def interval(pairs, *, samples=5000, seed=20261110):
    clustered = defaultdict(list)
    for paper, delta in pairs:
        clustered[paper].append(delta)
    keys = sorted(clustered)
    if not keys:
        return None
    rng = random.Random(seed)
    # Resample papers, preserving all questions within each sampled paper.
    draws = []
    for _ in range(samples):
        values = [v for key in rng.choices(keys, k=len(keys)) for v in clustered[key]]
        draws.append(mean(values))
    draws.sort()
    return {"mean_delta": mean(delta for _, delta in pairs), "papers": len(keys),
            "questions": len(pairs), "low_95": draws[int(samples * .025)],
            "high_95": draws[int(samples * .975)], "resampling_unit": "paper",
            "samples": samples, "seed": seed}


def audit(dataset, report):
    if not report.get("rows") or not isinstance(report.get("summary"), dict):
        raise ValueError("empty or invalid evaluation report")
    groups = {g["id"]: g for g in dataset["groups"] if g["split"] == report["split"]}
    rows_by_arm = defaultdict(dict)
    errors, extras = [], []
    for row in report["rows"]:
        gid, arm = row["group_id"], row["arm"]
        if gid not in groups or gid in rows_by_arm[arm]:
            raise ValueError("unknown or repeated report group")
        group = groups[gid]
        candidates = {c["id"]: c for c in group["candidates"]}
        ids = row["selected_ids"]
        if len(ids) > 6 or len(set(ids)) != len(ids) or not set(ids) <= set(candidates):
            raise ValueError("invalid selected IDs")
        selected = [candidates[cid] for cid in ids]
        if sum(len(c["quote"]) for c in selected) > 12000:
            raise ValueError("selection exceeded character budget")
        expected_hash = hashlib.sha256(json.dumps(sorted(candidates)).encode()).hexdigest()
        if row["candidate_set_sha256"] != expected_hash:
            raise ValueError("candidate hash mismatch")
        recomputed = set_metrics(group, selected)
        for key, value in recomputed.items():
            if row.get(key) != value:
                errors.append(f"{gid}/{arm}: {key} mismatch")
        alternatives = group.get("alternative_evidence_sets", [])
        annotated = {cid for alt in alternatives for cid in alt}
        alternative_scores = []
        for alt in alternatives:
            common = len(set(ids) & set(alt))
            alternative_scores.append({"recall": common / len(set(alt)),
                                       "f1": 2 * common / (len(ids) + len(set(alt))),
                                       "all": int(set(alt) <= set(ids))})
        extra = {"group_id": gid, "paper": group["paper"], "arm": arm,
                 "support_paragraph_count": len(group["dimensions"]),
                 "pool_contains_all_support": not recomputed["missing_candidate_dimensions"],
                 "support_coverage": recomputed["dimension_coverage_at_6"],
                 "all_support": recomputed["all_required_dimensions_at_6"],
                 "annotated_selected": len(set(ids) & annotated), "selected": len(ids),
                 "best_annotation_recall": max((v["recall"] for v in alternative_scores), default=None),
                 "best_annotation_f1": max((v["f1"] for v in alternative_scores), default=None),
                 "any_annotation_complete": max((v["all"] for v in alternative_scores), default=None),
                 "candidate_support_recall": recomputed["candidate_dimension_recall"]}
        rows_by_arm[arm][gid] = extra
        extras.append(extra)
    for arm, rows in rows_by_arm.items():
        if set(rows) != set(groups):
            errors.append(f"{arm}: missing groups")
        original = report["summary"][arm]
        for key in original:
            values = [r[key] for r in report["rows"] if r["arm"] == arm and r[key] is not None]
            if values and abs(mean(values) - original[key]) > 1e-12:
                errors.append(f"{arm}: summary mismatch")
    if set(rows_by_arm) != set(report["summary"]):
        errors.append("report summary and row arms differ")
    comparisons = {}
    trained = rows_by_arm.get("lora_selector", {})
    for arm, rows in rows_by_arm.items():
        if arm == "lora_selector":
            continue
        keys = sorted(set(rows) & set(trained))
        changes = [trained[k]["support_coverage"] - rows[k]["support_coverage"] for k in keys]
        comparisons[arm] = {"improved": sum(d > 1e-12 for d in changes),
                            "regressed": sum(d < -1e-12 for d in changes),
                            "tied": sum(abs(d) <= 1e-12 for d in changes),
                            "paired_coverage_interval": interval([(trained[k]["paper"], d)
                                                                    for k, d in zip(keys, changes)]),
                            "regression_group_ids": [k for k, d in zip(keys, changes) if d < -1e-12]}
    alternative_summary = {}
    for arm, rows in rows_by_arm.items():
        alternative_summary[arm] = {key: mean(r[key] for r in rows.values() if r[key] is not None)
                                   for key in ("best_annotation_recall", "best_annotation_f1", "any_annotation_complete")}
    return {"valid": not errors, "errors": errors, "split": report["split"],
            "audit_scope": "selected IDs, candidate membership/hash, budgets, recomputed set metrics and paired statistics; not original score-cache provenance",
            "questions": len(groups), "papers": len({g["paper"] for g in groups.values()}),
            "support_count_distribution": dict(Counter(len(g["dimensions"]) for g in groups.values())),
            "all_support_in_pool_questions": sum(all(any(d["id"] in c["covers"] for c in g["candidates"])
                                                       for d in g["dimensions"]) for g in groups.values()),
            "comparisons": comparisons, "alternative_annotation_summary": alternative_summary,
            "alternative_metric_note": "Best over human annotation sets; custom Top-6 protocol, not official QASPER score",
            "semantic_facets_evaluated": False, "rows": extras}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    dataset = load_dataset(args.dataset)
    report = json.loads(args.report.read_bytes())
    if hashlib.sha256(args.dataset.read_bytes()).hexdigest() != report["dataset_sha256"]:
        raise ValueError("dataset/report hash mismatch")
    result = audit(dataset, report)
    result["report_sha256"] = hashlib.sha256(args.report.read_bytes()).hexdigest()
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
    print(json.dumps({k: v for k, v in result.items() if k != "rows"}, indent=2))
    raise SystemExit(0 if result["valid"] else 1)
