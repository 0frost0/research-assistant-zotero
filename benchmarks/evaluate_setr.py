"""Same frozen MultiHopRAG pool: BM25, Qwen Top-K, generated set selections.

Reports annotated-evidence coverage only, not semantic-negative classification.
Malformed/overflow generation counts as empty selection (failure), never null success.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmarks.setr_protocol import PROTOCOL, digest, evidence_sets, metrics, normalize, parse_selection, summarize
from benchmarks.run_setr_generation import input_fingerprint

INSTRUCTION = "Retrieve passages that directly answer the question, preserving its conditions, entities and numbers. For a definition question, prefer a passage explaining what the requested concept is, not a passage that merely mentions it."


def read_predictions(paths, groups):
    lookup = {g["id"]: g for g in groups}
    result = {}
    for path in paths:
        with path.open(encoding="utf-8") as f:
            for line in f:
                row = json.loads(line)
                if row["id"] not in lookup or row["id"] in result:
                    raise ValueError("unknown/duplicate prediction")
                g = lookup[row["id"]]
                if row["input_sha256"] != input_fingerprint(g, row["mode"]):
                    raise ValueError("prediction input mismatch")
                if row["status"] == "ok":
                    if parse_selection(row["output"], len(g["candidates"]), ranking=row["mode"] == "rank") != row["selected_indices"]:
                        raise ValueError("prediction selection mismatch")
                elif row["selected_indices"]:
                    raise ValueError("failed generation has nonempty selection")
                result[row["id"]] = row
    if set(result) != set(lookup):
        raise ValueError("incomplete predictions; no partial external-test report")
    return result


def evaluate(data, *, prediction_arms=None, scorer=None):
    if data["protocol"] != PROTOCOL or data["role"] != "external_test_only":
        raise ValueError("unexpected dataset protocol")
    normalized = {c["id"]: normalize(c["text"]) for g in data["groups"] for c in g["candidates"]}
    rows = []
    for pos, g in enumerate(data["groups"]):
        candidates = g["candidates"]
        matched = evidence_sets(candidates, g["gold"], normalized_texts=[normalized[c["id"]] for c in candidates])
        if (g["question_type"] == "null_query") != (len(matched[0]) == 0):
            raise ValueError("null query/gold inconsistency")
        arms = {"bm25_top6": (list(range(min(6, len(candidates)))), "ok")}
        if scorer:
            scores = scorer(g["question"], [c["text"] for c in candidates])
            import math
            if len(scores) != len(candidates) or any(type(s) not in (int, float) or not math.isfinite(s) or not 0 <= s <= 1 for s in scores):
                raise ValueError("invalid reranker scores")
            arms["qwen_top6"] = (sorted(range(len(scores)), key=lambda i: (-scores[i], i))[:6], "ok")
        for name, predictions in (prediction_arms or {}).items():
            prediction = predictions[g["id"]]
            arms[name] = (prediction["selected_indices"], prediction["status"])
        for arm, (indices, status) in arms.items():
            rows.append({"id": g["id"], "question_type": g["question_type"], "arm": arm, "status": status,
                         "candidate_pool_sha256": digest([c["id"] for c in candidates]),
                         "selected_ids": [candidates[i]["id"] for i in indices],
                         **metrics(candidates, g["gold"], indices, matched_evidence=matched)})
        if (pos + 1) % 100 == 0:
            print(json.dumps({"evaluated": pos + 1, "total": len(data["groups"])}), flush=True)
    summary = {arm: summarize([r for r in rows if r["arm"] == arm]) for arm in sorted({r["arm"] for r in rows})}
    slices = {kind: {arm: summarize([r for r in rows if r["arm"] == arm and r["question_type"] == kind]) for arm in summary}
              for kind in sorted({r["question_type"] for r in rows})}
    return {"protocol": PROTOCOL, "exact_paper_reproduction": False, "production_writes": False,
            "notes": ["External test not used for training or checkpoint selection.",
                      "BM25 whitespace-word chunks differ from paper BGE retrieval; no original-paper score comparison.",
                      "IC is macro distinct annotated gold-span recall; unannotated text is not a semantic negative.",
                      "Set output order is evaluated as emitted, no padding; IC@k plateaus when fewer than k selected.",
                      "Failed output is zero coverage, but never counted as successful null abstention.",
                      "No end-to-end QA quality or original SetR checkpoint claim."],
            "summary": summary, "question_type_slices": slices, "rows": rows}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dataset", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--base-model", type=Path)
    p.add_argument("--model-manifest", type=Path)
    p.add_argument("--prediction-arm", action="append", default=[], help="NAME=PATH; repeat NAME for sharded files")
    a = p.parse_args()
    if a.output.exists():
        raise FileExistsError(a.output)
    data = json.loads(a.dataset.read_bytes())
    paths = {}
    for arg in a.prediction_arm:
        name, path = arg.split("=", 1)
        if name in {"bm25_top6", "qwen_top6"}:
            raise ValueError("reserved arm name")
        paths.setdefault(name, []).append(Path(path))
    predictions = {name: read_predictions(items, data["groups"]) for name, items in paths.items()}
    scorer = None
    if a.base_model:
        from deployment.embedding.remote_reranker_service import Scorer, MODEL, REVISION
        manifest = json.loads(a.model_manifest.read_bytes())
        if manifest["repo"] != MODEL or manifest["revision"] != REVISION:
            raise ValueError("reranker manifest mismatch")
        # Existing manifests list weights under `files` or `sha256`.
        files = manifest.get("files", manifest.get("sha256", {}))
        if not isinstance(files, dict) or not files:
            raise ValueError("model manifest lacks file fingerprints")
        for name, expected in files.items():
            if hashlib.sha256((a.base_model / name).read_bytes()).hexdigest() != expected:
                raise ValueError("reranker weight fingerprint mismatch")
        model = Scorer(str(a.base_model))
        scorer = lambda q, docs: model.scores(q, docs, INSTRUCTION)
    report = evaluate(data, prediction_arms=predictions, scorer=scorer)
    report["dataset_sha256"] = hashlib.sha256(a.dataset.read_bytes()).hexdigest()
    report["evaluator_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    report["prediction_sha256"] = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for paths in paths.values() for p in paths}
    if a.model_manifest:
        report["model_manifest_sha256"] = hashlib.sha256(a.model_manifest.read_bytes()).hexdigest()
    a.output.parent.mkdir(parents=True, exist_ok=True)
    with a.output.open("x", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(json.dumps(report["summary"], indent=2))


if __name__ == "__main__":
    main()
