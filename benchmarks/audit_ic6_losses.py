"""Evaluator-only loss accounting. Never produces queries or evidence selections.

Reads frozen gold only for diagnostics; writes counts, not facts or corpus text.
No output from this program may be consumed by training or actual selection.
"""
import argparse
import math
from pathlib import Path
from statistics import mean
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmarks.run_ic6_retrieval import load, sha, validate_bundle, write_new
from benchmarks.setr_protocol import PROTOCOL, evidence_sets, normalize


def account(facts, pool_matches, shortlist_positions, selected_positions, *, upstream_ok):
    """Exact additive losses on the unchanged fact-union metric, budget <= 6."""
    if (len(set(shortlist_positions)) != len(shortlist_positions) or
            any(type(i) is not int or not 0 <= i < len(pool_matches) for i in shortlist_positions) or
            len(selected_positions) > 6 or len(set(selected_positions)) != len(selected_positions) or
            not set(selected_positions) <= set(shortlist_positions)):
        raise ValueError("Invalid nested candidate/selection indices")
    if not upstream_ok and (shortlist_positions or selected_positions):
        raise ValueError("Failed upstream must have empty outputs")
    n = len(facts)
    pool = set().union(*pool_matches) if pool_matches else set()
    short = set().union(*(pool_matches[i] for i in shortlist_positions)) if shortlist_positions else set()
    selected = set().union(*(pool_matches[i] for i in selected_positions)) if selected_positions else set()
    if not selected <= short <= pool <= set(range(n)):
        raise ValueError("Invalid fact mapping")
    result = {"gold_count": n, "pool_gold_count": len(pool), "shortlist_gold_count": len(short),
              "selected_gold_count": len(selected)}
    values = [n - len(pool), len(pool) if not upstream_ok else 0,
              len(pool) - len(short) if upstream_ok else 0, len(short) - len(selected), len(selected)]
    names = ["retrieval_loss", "upstream_failure_loss", "shortlist_loss", "selection_loss", "achieved_ic6"]
    result.update({key: value / n if n else None for key, value in zip(names, values)})
    if n and not math.isclose(sum(result[key] for key in names), 1, abs_tol=1e-12):
        raise ValueError("Loss accounting does not sum to one")
    return result


def summarize(rows):
    answerable = [r for r in rows if r["gold_count"]]
    keys = ("retrieval_loss", "upstream_failure_loss", "shortlist_loss", "selection_loss", "achieved_ic6")
    return {"questions": len(rows), "answerable": len(answerable),
            "null_questions": len(rows) - len(answerable),
            **{key: mean(r[key] for r in answerable) if answerable else None for key in keys},
            "pool_ic_diagnostic_only": mean(r["pool_gold_count"] / r["gold_count"] for r in answerable) if answerable else None,
            "shortlist_ic_diagnostic_only": mean(r["shortlist_gold_count"] / r["gold_count"] for r in answerable) if answerable else None,
            "pool_complete_questions": sum(r["pool_gold_count"] == r["gold_count"] for r in answerable),
            "shortlist_complete_questions": sum(r["shortlist_gold_count"] == r["gold_count"] for r in answerable),
            "selected_complete_questions": sum(r["selected_gold_count"] == r["gold_count"] for r in answerable)}


def audit(a):
    bundle, retrieval, inputs, gold, baseline = map(load, (a.bundle, a.retrieval, a.inputs, a.gold, a.baseline_report))
    validate_bundle(bundle)
    if (gold["protocol"] != PROTOCOL or gold["role"] != "external_test_only" or
            retrieval["bundle_sha256"] != sha(a.bundle) or
            inputs["fingerprints"]["bundle"] != sha(a.bundle) or
            inputs["fingerprints"]["retrieval"] != sha(a.retrieval) or
            baseline["fingerprints"]["bundle"] != sha(a.bundle) or
            baseline["fingerprints"]["gold"] != sha(a.gold)):
        raise ValueError("Input identity/protocol mismatch")
    queries = {q["id"]: q for q in bundle["queries"]}
    if len(queries) != len(bundle["queries"]):
        raise ValueError("Duplicate question IDs")

    def keyed(rows):
        result = {r["id"]: r for r in rows}
        if len(result) != len(rows) or set(result) != set(queries):
            raise ValueError("Partial or duplicate report; no audit")
        return result

    groups, labels, anchors = map(keyed, (inputs["groups"], gold["groups"], baseline["rows"]))
    chunks = {c["id"]: c for c in bundle["chunks"]}
    if inputs["chunks"] != bundle["chunks"]:
        raise ValueError("Shortlist corpus changed")
    texts = {ident: normalize(c["text"]) for ident, c in chunks.items()}
    rows = []
    for qid, q in queries.items():
        g, label, old = groups[qid], labels[qid], anchors[qid]
        pool = retrieval["pools"][qid]["hybrid100"]
        short, chosen = g["candidate_ids"], g["baseline_selected_ids"]
        if (len(pool) != 100 or len(set(pool)) != 100 or not set(pool) <= set(chunks) or
                g["question"] != q["question"] or label["question"] != q["question"] or
                old["question_type"] != label["question_type"] or old["status"] != g["status"] or
                old["selected_ids"] != chosen or len(set(short)) != len(short) or not set(short) <= set(pool) or
                (g["status"] == "ok" and (len(short) != 32 or len(chosen) != 6))):
            raise ValueError("Candidate, question or baseline changed")
        matched = evidence_sets([chunks[i] for i in pool], label["gold"], normalized_texts=[texts[i] for i in pool])
        if (label["question_type"] == "null_query") != (not matched[0]):
            raise ValueError("Null label mismatch")
        counts = account(*matched, [pool.index(i) for i in short], [pool.index(i) for i in chosen],
                         upstream_ok=g["status"] == "ok")
        if (old["gold_count"] != counts["gold_count"] or old["pool_gold_count"] != counts["pool_gold_count"] or
                old["covered_gold_count"] != counts["selected_gold_count"]):
            raise ValueError("Old metrics not exactly reproducible")
        rows.append({"id": qid, "question_type": label["question_type"], "status": g["status"], **counts})
    summary = summarize(rows)
    if not math.isclose(summary["achieved_ic6"], baseline["summary"]["ic_at_6"], abs_tol=1e-12):
        raise ValueError("Anchor aggregate changed")
    report = {"version": "ic6-loss-audit-v1", "protocol": PROTOCOL,
              "role": "diagnostic_only_never_selection_or_training", "exploratory_not_independent_test": True,
              "summary": summary, "slices": {t: summarize([r for r in rows if r["question_type"] == t])
                                               for t in sorted({r["question_type"] for r in rows})},
              "fingerprints": {"bundle": sha(a.bundle), "retrieval": sha(a.retrieval), "inputs": sha(a.inputs),
                               "gold": sha(a.gold), "baseline_report": sha(a.baseline_report), "script": sha(__file__)},
              "notes": ["All five loss/achievement terms use the same answerable denominator and sum to one.",
                        "Upstream failure is separated from valid shortlist compression losses.",
                        "Shortlist IC is a diagnostic pool ceiling, NOT an achieved selector score.",
                        "No queries, selections, gold facts or passage texts are emitted; null rows have no IC."],
              "rows": rows}
    write_new(a.output, report)
    import json
    print(json.dumps({k: v for k, v in report.items() if k != "rows"}, indent=2), flush=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    for flag in ("bundle", "retrieval", "inputs", "gold", "baseline-report", "output"):
        p.add_argument("--" + flag, required=True, type=Path)
    audit(p.parse_args())
