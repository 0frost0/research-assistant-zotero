"""Complete-only supplementary bridge/dense/hybrid ablation; no new tuning."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmarks.run_ic6_retrieval import (
    BRIDGE, load, sha, validate_bundle, read_cache, validate_scores,
    select, oracle6, write_new,
)
from benchmarks.setr_protocol import PROTOCOL, evidence_sets, metrics, summarize


def evaluate(run, scores_dir, gold_path, output):
    data = load(run / "bundle.json")
    validate_bundle(data)
    retrieval_path = run / "embedding/retrieval.json"
    retrieval = load(retrieval_path)
    manifest = load(scores_dir / "manifest.json")
    complete = load(scores_dir / "complete.json")
    if (manifest["bundle_sha256"] != sha(run / "bundle.json") or
            manifest["retrieval_sha256"] != sha(retrieval_path) or
            manifest["instruction"] != BRIDGE or
            complete["scores_sha256"] != sha(scores_dir / "scores.jsonl")):
        raise ValueError("Score contract mismatch")
    queries = {q["id"]: q for q in data["queries"]}
    cache = read_cache(scores_dir / "scores.jsonl", set(queries))
    if set(cache) != set(queries):
        raise ValueError("Incomplete scores")
    gold = load(gold_path)
    if gold["protocol"] != PROTOCOL or gold["role"] != "external_test_only":
        raise ValueError("Gold protocol mismatch")
    if len(gold["groups"]) != len(queries) or {g["id"] for g in gold["groups"]} != set(queries):
        raise ValueError("Gold IDs mismatch")
    lookup = {c["id"]: c for c in data["chunks"]}
    rows = []
    for group in gold["groups"]:
        qid = group["id"]
        if group["question"] != queries[qid]["question"]:
            raise ValueError("Question mismatch")
        pools = {"bm25100": data["bm25"][qid], **retrieval["pools"][qid]}
        union = list(dict.fromkeys(pools["bm25100"] + pools["dense100"] + pools["hybrid100"]))
        record = cache[qid]
        validate_scores(record, union)
        scores = dict(zip(union, record["scores"]))
        lengths = dict(zip(union, record["document_tokens"]))
        for pool, ids in pools.items():
            candidates = [lookup[i] for i in ids]
            matched = evidence_sets(candidates, group["gold"])
            if (group["question_type"] == "null_query") != (not matched[0]):
                raise ValueError("Null/gold mismatch")
            for mmr in (False, True):
                selected = select(ids, [scores[i] for i in ids], lookup, mmr=mmr)
                rows.append({"id": qid, "arm": "bridge_" + ("mmr_" if mmr else "") + pool,
                             "question_type": group["question_type"], "status": "ok",
                             "selected_ids": selected,
                             "selected_document_tokens": sum(lengths[i] for i in selected),
                             "oracle_ic_at_6_diagnostic_only": oracle6(matched[1], len(matched[0])),
                             **metrics(candidates, group["gold"], [ids.index(i) for i in selected],
                                       matched_evidence=matched)})
    arms = sorted({r["arm"] for r in rows})
    report = {"protocol": PROTOCOL, "exploratory_after_first_sweep": True, "final_budget": 6,
              "summary": {arm: summarize([r for r in rows if r["arm"] == arm]) for arm in arms},
              "question_type_slices": {kind: {arm: summarize([r for r in rows if
                  r["arm"] == arm and r["question_type"] == kind]) for arm in arms}
                  for kind in sorted({r["question_type"] for r in rows})},
              "fingerprints": {"scores": sha(scores_dir / "scores.jsonl"), "gold": sha(gold_path),
                               "bundle": sha(run / "bundle.json"), "evaluator": sha(__file__)}, "rows": rows}
    write_new(output, report)
    print(json.dumps(report["summary"], indent=2), flush=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    for name in ("run", "scores-dir", "gold-path", "output"):
        p.add_argument("--" + name, required=True, type=Path)
    a = p.parse_args()
    evaluate(a.run, a.scores_dir, a.gold_path, a.output)
