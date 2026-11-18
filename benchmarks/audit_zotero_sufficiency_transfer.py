"""Audit the boundary between Zotero anchor retrieval and sufficiency quality.

This is a read-only post-hoc audit of the frozen stage-1 report. It recomputes
anchor coverage and candidate integrity, but never treats assistant-authored
anchors or unverified absence probes as sufficiency gold.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import statistics


def file_sha256(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def mean(values):
    return statistics.mean(values) if values else None


def arm_metrics(rows, arm):
    labelled = [row for row in rows if not row["no_answer"]]
    ranks = [rank for row in labelled for rank in row[arm]["anchor_ranks"]]
    case_recall = [row[arm]["recall_at_6"] for row in labelled]
    return {
        "labelled_cases": len(labelled),
        "anchors": len(ranks),
        "candidate_anchor_recall": mean([rank is not None for rank in ranks]),
        "anchor_recall_at_1": mean([rank == 1 for rank in ranks]),
        "anchor_recall_at_6": mean([rank is not None and rank <= 6 for rank in ranks]),
        "case_all_anchors_at_6": mean(case_recall),
        "case_all_anchors_in_candidates": mean(
            [row[arm]["all_anchors_in_candidates"] for row in labelled]),
        "rerank_statuses": dict(Counter(row["rerank"]["status"] for row in rows)),
        "coverage_warning_cases": sum(bool(row.get("retrieval_warning")) for row in rows),
        "candidate_counts": {
            "min": min((row["candidate_count"] for row in rows), default=None),
            "median": statistics.median(row["candidate_count"] for row in rows)
            if rows else None,
            "max": max((row["candidate_count"] for row in rows), default=None),
        },
    }


def validate_case(row):
    required = {"id", "no_answer", "candidate_count", "same_pool", "candidates",
                "before", "after", "rerank"}
    if not required <= set(row):
        raise ValueError(f"missing report fields for {row.get('id')}")
    if not isinstance(row["no_answer"], bool) or not row["same_pool"]:
        raise ValueError(f"invalid scope or changed candidate pool: {row['id']}")
    if row["candidate_count"] != len(row["candidates"]):
        raise ValueError(f"candidate count mismatch: {row['id']}")
    ids = [candidate["id"] for candidate in row["candidates"]]
    if len(ids) != len(set(ids)):
        raise ValueError(f"duplicate candidate IDs: {row['id']}")
    for arm in ("before", "after"):
        if len(row[arm]["anchor_ranks"]) != row["anchor_count"]:
            raise ValueError(f"anchor count mismatch: {row['id']}/{arm}")
        if any(rank is not None and (type(rank) is not int or rank < 1)
               for rank in row[arm]["anchor_ranks"]):
            raise ValueError(f"invalid anchor rank: {row['id']}/{arm}")


def audit(dataset_path, report_path, output_path):
    dataset_raw = Path(dataset_path).read_bytes()
    report_raw = Path(report_path).read_bytes()
    dataset = json.loads(dataset_raw)
    report = json.loads(report_raw)
    expected_dataset_hash = hashlib.sha256(dataset_raw).hexdigest()
    if report.get("dataset_sha256") != expected_dataset_hash:
        raise ValueError("stage-1 report does not match frozen dataset")
    if dataset.get("split") != "development":
        raise ValueError("Zotero transfer audit requires the development dataset")
    cases = report.get("cases")
    if not isinstance(cases, list) or len(cases) != 104:
        raise ValueError("expected two retrieval arms for each of 52 cases")
    dataset_cases = {case["id"]: case for case in dataset.get("cases", [])}
    rows = []
    by_mode = {}
    for row in cases:
        validate_case(row)
        source = dataset_cases.get(row["id"])
        if source is None or source["no_answer"] != row["no_answer"]:
            raise ValueError(f"dataset/report case mismatch: {row['id']}")
        rows.append(row)
        mode = row.get("retrieval_mode")
        if not isinstance(mode, str) or not mode:
            raise ValueError(f"missing retrieval mode: {row['id']}")
        by_mode.setdefault(mode, []).append(row)
    if set(by_mode) != {"legacy", "hybrid"} or any(len(group) != 52 for group in by_mode.values()):
        raise ValueError("expected legacy and hybrid arms with 52 cases each")
    for mode, group in by_mode.items():
        if len({row["id"] for row in group}) != 52:
            raise ValueError(f"duplicate or missing case in {mode} arm")
    positive = [row for row in by_mode["legacy"] if not row["no_answer"]]
    probes = [row for row in by_mode["legacy"] if row["no_answer"]]
    if len(positive) != 48 or len(probes) != 4:
        raise ValueError("expected 48 labelled development cases and 4 probes per arm")
    def mode_result(group):
        labelled = [row for row in group if not row["no_answer"]]
        return {
            "case_counts": {"total": len(group), "anchor_cases": len(labelled),
                            "unscored_absence_probes": len(group) - len(labelled)},
            "before": arm_metrics(group, "before"),
            "after": arm_metrics(group, "after"),
            "pair_changes": dict(Counter(
                "improved" if row["after"]["anchor_ranks"] and row["before"]["anchor_ranks"] and
                (row["after"]["anchor_ranks"][0] or 10**9) < (row["before"]["anchor_ranks"][0] or 10**9)
                else "regressed" if row["after"]["anchor_ranks"] and row["before"]["anchor_ranks"] and
                (row["after"]["anchor_ranks"][0] or 10**9) > (row["before"]["anchor_ranks"][0] or 10**9)
                else "unchanged" for row in labelled)),
        }
    result = {
        "dataset_id": dataset["id"],
        "dataset_sha256": expected_dataset_hash,
        "report_sha256": hashlib.sha256(report_raw).hexdigest(),
        "split": dataset["split"],
        "case_counts": {"total": 52, "anchor_cases": len(positive),
                        "unscored_absence_probes": len(probes), "retrieval_arms": 2},
        "proxy_scope": {
            "anchor_labels": "assistant source-checked locators, not independent human gold",
            "absence_probes": "not exhaustively checked across the corpus; excluded from quality metrics",
            "sufficiency_accuracy": "not measured; this report contains no independent sufficient/insufficient gold",
        },
        "retrieval_modes": {mode: mode_result(group) for mode, group in sorted(by_mode.items())},
        "same_candidate_pool_cases": {
            mode: sum(row["same_pool"] for row in group)
            for mode, group in sorted(by_mode.items())
        },
        "not_a_gate_result": True,
        "production_change": False,
    }
    Path(output_path).open("x", encoding="utf-8").write(
        json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    audit(args.dataset, args.report, args.output)
