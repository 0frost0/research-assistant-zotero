"""Isolated, gold-free IC@6 exploration. Gold is read only by `evaluate`.

Reuse frozen chunking and local weights. No production calls, downloads or writes.
Every cache is bound to input, code, instruction and local model fingerprints.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmarks.setr_protocol import digest, PROTOCOL
from benchmarks.evaluate_setr import INSTRUCTION

VERSION = "ic6-retrieval-exploration-v1"
BRIDGE = ("Retrieve evidence needed to answer the multi-hop question, including direct facts and "
          "intermediate or bridging facts. A passage need not answer the entire question by itself. "
          "Preserve the requested entities, relations, sources, dates, numbers and comparison conditions. "
          "Prefer factual support for a necessary reasoning step over a mere mention of the topic.")
QUERY_INSTRUCTION = "Given a multi-hop question, retrieve passages that provide the facts needed to answer it."
CORPUS_SHA = "20b61b5ab84de84a927420c5d265b7ec8d859ae49980699958a787ade9e4d28f"


def sha(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for block in iter(lambda: f.read(8 * 1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def load(path):
    return json.loads(Path(path).read_bytes())


def write_new(path, data):
    with Path(path).open("x", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, allow_nan=False, indent=2)


def model_fingerprint(root):
    root = Path(root)
    files = sorted(p for p in root.rglob("*") if p.is_file() and
                   p.suffix in {".safetensors", ".json", ".jinja", ".py", ".txt"} and
                   ".cache" not in p.parts and "__pycache__" not in p.parts)
    if not any(p.suffix == ".safetensors" for p in files):
        raise ValueError("No existing local weights")
    return {str(p.relative_to(root)): sha(p) for p in files}


def validate_bundle(data):
    if set(data) != {"version", "chunks", "queries", "bm25", "provenance"} or data["version"] != VERSION:
        raise ValueError("Unexpected input bundle")
    if any(set(q) != {"id", "question"} for q in data["queries"]):
        raise ValueError("Only query text and ID allowed")
    chunks = data["chunks"]
    ids = {c["id"] for c in chunks}
    if len(ids) != len(chunks) or len({q["id"] for q in data["queries"]}) != len(data["queries"]):
        raise ValueError("Duplicate inputs")
    allowed = {"id", "source_id", "text", "word_start", "word_end"}
    if any(set(c) != allowed for c in chunks):
        raise ValueError("Unexpected candidate fields")
    if set(data["bm25"]) != {q["id"] for q in data["queries"]}:
        raise ValueError("Incomplete retrieval")
    for pool in data["bm25"].values():
        if len(pool) != 100 or len(set(pool)) != len(pool) or not set(pool) <= ids:
            raise ValueError("Expected 100 unique real corpus chunks")


def prepare(a):
    from benchmarks.prepare_setr_data import corpus_chunks
    from research_assistant.retrieval.hybrid_retrieval import BM25Index
    if sha(a.corpus) != CORPUS_SHA:
        raise ValueError("Corpus differs from original experiment")
    frozen = load(a.frozen_inputs)
    if frozen["role"] != "external_test_only" or any(
            set(g) != {"id", "question", "candidates"} for g in frozen["groups"]):
        raise ValueError("Frozen model inputs contain extra fields")
    chunks = corpus_chunks(load(a.corpus))
    lookup = {c["id"]: c for c in chunks}
    index = BM25Index([{"point_id": c["id"], "text": c["text"], "source": c["source_id"],
                        "file_type": ".txt"} for c in chunks])
    queries, pools = [], {}
    for pos, group in enumerate(frozen["groups"]):
        queries.append({"id": group["id"], "question": group["question"]})
        hits = [h["point_id"] for h in index.search(group["question"], k=100)]
        if hits[:20] != [c["id"] for c in group["candidates"]]:
            raise ValueError("Rebuilt BM25 Top-20 differs from frozen pool")
        if any(lookup[c["id"]]["text"] != c["text"] for c in group["candidates"]):
            raise ValueError("Rebuilt chunk text differs from frozen pool")
        pools[group["id"]] = hits
        if (pos + 1) % 100 == 0:
            print(json.dumps({"prepared": pos + 1}), flush=True)
    data = {"version": VERSION, "chunks": chunks, "queries": queries, "bm25": pools,
            "provenance": {"corpus_sha256": sha(a.corpus), "frozen_model_input_sha256": sha(a.frozen_inputs),
                           "script_sha256": sha(__file__), "all_frozen_top20_identical": True,
                           "chunking": "256 whitespace words / 64 overlap; original metadata unchanged"}}
    validate_bundle(data)
    write_new(a.output, data)


def contract(a, data, extra):
    return {"version": VERSION, "bundle_sha256": sha(a.bundle), "script_sha256": sha(__file__),
            "model_files": model_fingerprint(a.model), **extra}


def check_manifest(path, value):
    if path.exists():
        if load(path) != value:
            raise ValueError("Cache contract changed; choose a new directory")
    else:
        write_new(path, value)


def embed(a):
    import numpy as np
    import torch
    data = load(a.bundle)
    validate_bundle(data)
    a.output.mkdir(exist_ok=True)
    check_manifest(a.output / "manifest.json", contract(a, data, {
        "model_identity": "Qwen3-VL-Embedding-2B (not Qwen3 text embedding)",
        "query_instruction": QUERY_INSTRUCTION, "document_instruction": "Represent the user's input.",
        "batch": 8, "max_length": 8192, "dtype": "bfloat16", "normalization": "float32 L2"}))
    sys.path.insert(0, str(a.model))
    from scripts.qwen3_vl_embedding import Qwen3VLEmbedder
    model = Qwen3VLEmbedder(str(a.model), max_length=8192, torch_dtype=torch.bfloat16,
                            attn_implementation="sdpa", local_files_only=True)
    all_vectors = []
    for kind, items in [("corpus", data["chunks"]), ("queries", data["queries"])]:
        blocks = []
        for start in range(0, len(items), 8):
            path = a.output / f"{kind}-{start:06d}.npy"
            group = items[start:start + 8]
            if not path.exists():
                inputs = [{"text": x["text"]} if kind == "corpus" else
                          {"text": x["question"], "instruction": QUERY_INSTRUCTION} for x in group]
                # Helper truncates by default; explicitly gate the exact preprocessing.
                conversations = [model.format_model_input(**x) for x in inputs]
                texts = model.processor.apply_chat_template(conversations, tokenize=False, add_generation_prompt=True)
                lengths = model.processor.tokenizer(texts, add_special_tokens=True, truncation=False)["input_ids"]
                if any(len(x) > 8192 for x in lengths):
                    raise ValueError("Embedding input would truncate")
                processed = model._preprocess_inputs(conversations)
                actual = [row[mask.bool()].tolist() for row, mask in
                          zip(processed["input_ids"], processed["attention_mask"])]
                if actual != lengths:
                    raise ValueError("Embedding helper changed/truncated input or fell back to NULL")
                processed = {k: v.to(model.model.device) for k, v in processed.items()}
                with torch.inference_mode():
                    outputs = model.forward(processed)
                    vectors = model._pooling_last(outputs["last_hidden_state"], outputs["attention_mask"])
                    vectors = torch.nn.functional.normalize(vectors.float(), p=2, dim=-1).cpu().numpy()
                if not np.isfinite(vectors).all() or vectors.shape[0] != len(group):
                    raise ValueError("Invalid embedding output")
                with path.open("xb") as f:
                    np.save(f, vectors, allow_pickle=False)
            block = np.load(path, allow_pickle=False)
            if block.shape != (len(group), 2048) or not np.isfinite(block).all():
                raise ValueError("Invalid cached embedding")
            if not np.allclose(np.linalg.norm(block, axis=1), 1, atol=1e-4):
                raise ValueError("Embedding must be unit-normalized")
            blocks.append(block)
            if start % 256 == 0:
                print(json.dumps({"embedded": kind, "offset": start, "total": len(items)}), flush=True)
        all_vectors.append(np.concatenate(blocks))
    from research_assistant.retrieval.hybrid_retrieval import reciprocal_rank_fusion
    doc_vectors, query_vectors = all_vectors
    pools = {}
    for q, vector in zip(data["queries"], query_vectors):
        scores = doc_vectors @ vector
        order = np.argsort(-scores, kind="stable")[:100]
        dense = [{"point_id": data["chunks"][int(i)]["id"], "score": float(scores[i])} for i in order]
        lexical = [{"point_id": ident, "score": 0.0} for ident in data["bm25"][q["id"]]]
        fused = reciprocal_rank_fusion(dense, lexical, limit=100, rank_constant=60)
        pools[q["id"]] = {"dense100": [d["point_id"] for d in dense],
                           "hybrid100": [d["point_id"] for d in fused]}
    result = {"bundle_sha256": sha(a.bundle), "embedding_manifest_sha256": sha(a.output / "manifest.json"),
              "pools": pools, "cache_sha256": {p.name: sha(p) for p in sorted(a.output.glob("*.npy"))}}
    path = a.output / "retrieval.json"
    if path.exists():
        if load(path) != result:
            raise ValueError("Retrieval output changed")
    else:
        write_new(path, result)


def read_cache(path, expected_ids):
    rows = {}
    if path.exists():
        with path.open(encoding="utf-8") as f:
            for line in f:
                row = json.loads(line)  # Damaged tail fails closed, no silent repair.
                if row["id"] not in expected_ids or row["id"] in rows:
                    raise ValueError("Unknown/duplicate cache row")
                rows[row["id"]] = row
    return rows


def validate_scores(row, ids):
    if row["candidate_ids"] != ids or len(row["scores"]) != len(ids):
        raise ValueError("Score cache candidate mismatch")
    if any(type(v) not in (float, int) or not math.isfinite(v) or not 0 <= v <= 1 for v in row["scores"]):
        raise ValueError("Invalid score cache")
    if len(row["document_tokens"]) != len(ids) or any(type(v) is not int or v < 1 for v in row["document_tokens"]):
        raise ValueError("Invalid document token lengths")


def score(a):
    from deployment.embedding.remote_reranker_service import Scorer, MODEL, REVISION
    data = load(a.bundle)
    validate_bundle(data)
    instruction = INSTRUCTION if a.instruction == "original" else BRIDGE
    retrieval = load(a.retrieval) if a.retrieval else None
    if retrieval and retrieval["bundle_sha256"] != sha(a.bundle):
        raise ValueError("Dense retrieval input mismatch")
    a.output.mkdir(exist_ok=True)
    files = model_fingerprint(a.model)
    expected = load(a.model_manifest)
    if expected["repo"] != MODEL or expected["revision"] != REVISION or any(
            files.get(name) != value for name, value in expected["files"].items()):
        raise ValueError("Reranker fingerprint mismatch")
    check_manifest(a.output / "manifest.json", {
        "version": VERSION, "bundle_sha256": sha(a.bundle), "script_sha256": sha(__file__),
        "scorer_sha256": sha(Path(__file__).resolve().parents[1] / "deployment/embedding/remote_reranker_service.py"),
        "model_files": files, "model": MODEL, "revision": REVISION, "instruction": instruction,
        "retrieval_sha256": sha(a.retrieval) if retrieval else None, "micro_batch": 4,
        "dtype": "bfloat16", "no_truncation": True})
    lookup = {c["id"]: c for c in data["chunks"]}
    cached = read_cache(a.output / "scores.jsonl", {q["id"] for q in data["queries"]})
    model = Scorer(str(a.model))
    for pos, q in enumerate(data["queries"]):
        ids = list(data["bm25"][q["id"]])
        if retrieval:
            ids = list(dict.fromkeys(ids + retrieval["pools"][q["id"]]["dense100"] +
                                     retrieval["pools"][q["id"]]["hybrid100"]))
        if q["id"] in cached:
            validate_scores(cached[q["id"]], ids)
            continue
        texts = [lookup[ident]["text"] for ident in ids]
        start = time.perf_counter()
        scores = model.scores(q["question"], texts, instruction)
        elapsed = time.perf_counter() - start
        doc_lengths = [len(v) for v in model.tokenizer(texts, add_special_tokens=False, truncation=False)["input_ids"]]
        row = {"id": q["id"], "candidate_ids": ids, "scores": scores, "seconds": elapsed,
               "document_tokens": doc_lengths, "pair_count": len(ids)}
        validate_scores(row, ids)
        with (a.output / "scores.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, allow_nan=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        if (pos + 1) % 25 == 0 or pos == 0:
            print(json.dumps({"scored": pos + 1, "total": len(data["queries"]),
                              "last_pair_count": len(ids), "last_seconds": elapsed}), flush=True)
    write_new(a.output / "complete.json", {"questions": len(data["queries"]), "scores_sha256": sha(a.output / "scores.jsonl")})


def select(ids, scores, lookup, *, mmr=False):
    """Fixed lambda=.8, rank-normalized relevance and body-token Jaccard novelty."""
    from research_assistant.retrieval.hybrid_retrieval import tokens
    if len(ids) != len(scores) or len(set(ids)) != len(ids):
        raise ValueError("Selection inputs mismatch")
    order = sorted(range(len(ids)), key=lambda i: (-scores[i], i))
    if not mmr:
        return [ids[i] for i in order[:6]]
    relevance = {i: 1 - rank / max(1, len(order) - 1) for rank, i in enumerate(order)}
    terms = {i: set(tokens(lookup[ids[i]]["text"].split("\n\n", 1)[-1])) for i in order}
    chosen = []
    while len(chosen) < min(6, len(ids)):
        def value(i):
            redundancy = max((len(terms[i] & terms[j]) / max(1, len(terms[i] | terms[j]))
                              for j in chosen), default=0)
            return .8 * relevance[i] - .2 * redundancy
        chosen.append(max((i for i in order if i not in chosen), key=lambda i: (value(i), -i)))
    return [ids[i] for i in chosen]


def oracle6(matches, n):
    dp = {0: 0}
    for mask in {sum(1 << i for i in m) for m in matches}:
        for seen, count in list(dp.items()):
            if count < 6:
                union = seen | mask
                dp[union] = min(dp.get(union, 7), count + 1)
    return max(m.bit_count() for m, count in dp.items() if count <= 6) / n if n else None


def evaluate(a):
    from benchmarks.setr_protocol import evidence_sets, metrics, summarize
    from statistics import mean
    import numpy as np
    data, gold = load(a.bundle), load(a.gold)
    validate_bundle(data)
    if gold["protocol"] != PROTOCOL or gold["role"] != "external_test_only":
        raise ValueError("Unexpected evaluation protocol")
    lookup = {c["id"]: c for c in data["chunks"]}
    query_lookup = {q["id"]: q for q in data["queries"]}
    if len(gold["groups"]) != len(query_lookup) or {g["id"] for g in gold["groups"]} != set(query_lookup):
        raise ValueError("Gold query set differs")
    retrieval = load(a.retrieval)
    if retrieval["bundle_sha256"] != sha(a.bundle):
        raise ValueError("Retrieval evaluation mismatch")
    caches = {}
    for name in ("original", "bridge", "hybrid"):
        path = a.run_dir / name
        manifest, complete = load(path / "manifest.json"), load(path / "complete.json")
        if manifest["bundle_sha256"] != sha(a.bundle) or complete["scores_sha256"] != sha(path / "scores.jsonl"):
            raise ValueError("Score artifacts mismatch")
        if manifest["instruction"] != (BRIDGE if name == "bridge" else INSTRUCTION):
            raise ValueError("Unexpected score instruction")
        caches[name] = read_cache(path / "scores.jsonl", set(query_lookup))
        if set(caches[name]) != set(query_lookup):
            raise ValueError("No partial benchmark report")
    rows = []
    for group in gold["groups"]:
        qid = group["id"]
        if group["question"] != query_lookup[qid]["question"]:
            raise ValueError("Gold question text differs")
        bm = data["bm25"][qid]
        if bm[:20] != [c["id"] for c in group["candidates"]]:
            raise ValueError("Frozen gold pool differs")
        if any(lookup[c["id"]]["text"] != c["text"] for c in group["candidates"]):
            raise ValueError("Frozen gold text differs")
        configs = [("bm25_top6", "original", bm[:20], False, True)]
        configs += [(f"{name}_bm25{depth}", name, bm[:depth], False, False)
                    for name in ("original", "bridge") for depth in (20, 50, 100)]
        configs += [(f"{name}_mmr_bm25{depth}", name, bm[:depth], True, False)
                    for name in ("original", "bridge") for depth in (20, 100)]
        configs += [(f"original_{channel}", "hybrid", retrieval["pools"][qid][channel], False, False)
                    for channel in ("dense100", "hybrid100")]
        for arm, cache, ids, mmr, lexical in configs:
            row = caches[cache][qid]
            expected_ids = bm if cache != "hybrid" else list(dict.fromkeys(
                bm + retrieval["pools"][qid]["dense100"] + retrieval["pools"][qid]["hybrid100"]))
            validate_scores(row, expected_ids)
            scores = dict(zip(row["candidate_ids"], row["scores"]))
            lengths = dict(zip(row["candidate_ids"], row["document_tokens"]))
            selected = ids[:6] if lexical else select(ids, [scores[i] for i in ids], lookup, mmr=mmr)
            candidates = [lookup[i] for i in ids]
            matched = evidence_sets(candidates, group["gold"])
            if (group["question_type"] == "null_query") != (not matched[0]):
                raise ValueError("Null/gold mismatch")
            indices = [ids.index(i) for i in selected]
            rows.append({"id": qid, "arm": arm, "question_type": group["question_type"], "status": "ok",
                         "selected_ids": selected, "candidate_pool_sha256": digest(ids),
                         "selected_document_tokens": sum(lengths[i] for i in selected),
                         "actual_score_cache_pair_count": row["pair_count"],
                         "actual_score_cache_seconds": row["seconds"],
                         "oracle_ic_at_6_diagnostic_only": oracle6(matched[1], len(matched[0])),
                         **metrics(candidates, group["gold"], indices, matched_evidence=matched)})
    arms = sorted({r["arm"] for r in rows})
    summary = {}
    baseline = {r["id"]: r["ic_at_6"] for r in rows if r["arm"] == "original_bm2520" and r["gold_count"]}
    for arm in arms:
        values = [r for r in rows if r["arm"] == arm]
        deltas = np.array([r["ic_at_6"] - baseline[r["id"]] for r in values if r["gold_count"]])
        rng = np.random.default_rng(20261112)
        boot = np.array([rng.choice(deltas, len(deltas), replace=True).mean() for _ in range(2000)])
        summary[arm] = {**summarize(values), "oracle_ic_at_6_diagnostic_only": mean(
            r["oracle_ic_at_6_diagnostic_only"] for r in values if r["gold_count"]),
            "mean_selected_document_tokens": mean(r["selected_document_tokens"] for r in values),
            "paired_delta_vs_original_bm2520": float(deltas.mean()),
            "exploratory_paired_bootstrap_95ci": np.quantile(boot, [.025, .975]).tolist()}
    report = {"version": VERSION, "protocol": PROTOCOL, "exploratory_not_independent_test": True,
              "no_training_no_production_changes": True, "final_budget": 6,
              "gold_used_only_by_evaluator": True, "summary": summary,
              "question_type_slices": {t: {arm: summarize([r for r in rows if r["arm"] == arm and r["question_type"] == t])
                                           for arm in arms} for t in sorted({r["question_type"] for r in rows})},
              "fingerprints": {"bundle": sha(a.bundle), "gold": sha(a.gold), "retrieval": sha(a.retrieval),
                               "script": sha(__file__), **{name: sha(a.run_dir / name / "scores.jsonl") for name in caches}},
              "notes": ["All 13 arms predeclared, failures retained; no claim of original SetR reproduction.",
                        "CIs exploratory, unadjusted for multiple comparisons; no independent generalization claim.",
                        "IC@6 fixed at six original chunks; token cost reported, not an equal-token result.",
                        "Score-cache time is actual union scoring cost, not per-arm deployment latency.",
                        "Oracle reads gold for diagnostics only; never used in prediction."], "rows": rows}
    write_new(a.output, report)
    print(json.dumps(summary, indent=2), flush=True)


def orchestrate(a):
    """Run only with an isolated code snapshot. Exit nonzero on any failed stage."""
    a.run_dir.mkdir(exist_ok=True)
    script = Path(__file__).resolve()
    plan = {"version": VERSION, "script_sha256": sha(script), "frozen_before_scoring": True,
            "gold_free_prediction": True, "final_budget": 6, "mmr_lambda": .8,
            "retrieval_depths": [20, 50, 100], "rrf_constant": 60,
            "reranker_instructions": {"original": INSTRUCTION, "bridge": BRIDGE},
            "embedding_query_instruction": QUERY_INSTRUCTION,
            "arms": ["bm25_top6"] + [f"{n}_bm25{k}" for n in ("original", "bridge") for k in (20, 50, 100)] +
                    [f"{n}_mmr_bm25{k}" for n in ("original", "bridge") for k in (20, 100)] +
                    ["original_dense100", "original_hybrid100"],
            "gpu_assignments": {"embedding_then_bridge": 1, "original_then_hybrid": 2},
            "not_used": ["GPU0", "GPU3", "production HTTP services", "gold-guided queries or selection"],
            "exploratory_benchmark_already_seen": True}
    check_manifest(a.run_dir / "plan.json", plan)
    env = {**os.environ, "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "TOKENIZERS_PARALLELISM": "false"}
    bundle = a.run_dir / "bundle.json"
    retrieval = a.run_dir / "embedding/retrieval.json"

    def run(stage, args, gpu=None, python=None):
        with (a.run_dir / (stage + ".log")).open("a", encoding="utf-8") as log:
            subprocess.run([str(python or sys.executable), "-u", str(script), *map(str, args)],
                           env={**env, "CUDA_VISIBLE_DEVICES": str(gpu) if gpu is not None else ""},
                           stdout=log, stderr=subprocess.STDOUT, check=True)

    if not bundle.exists():
        run("prepare", ["prepare", "--corpus", a.corpus, "--frozen-inputs", a.frozen_inputs, "--output", bundle])
    common = ["score", "--bundle", bundle, "--model", a.model, "--model-manifest", a.model_manifest]

    def original():
        if not (a.run_dir / "original/complete.json").exists():
            run("original", common + ["--instruction", "original", "--output", a.run_dir / "original"], gpu=2)

    def embedding_bridge():
        if not retrieval.exists():
            run("embedding", ["embed", "--bundle", bundle, "--model", a.embedding_model,
                              "--output", a.run_dir / "embedding"], gpu=1, python=a.embedding_python)
        if not (a.run_dir / "bridge/complete.json").exists():
            run("bridge", common + ["--instruction", "bridge", "--output", a.run_dir / "bridge"], gpu=1)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(original), pool.submit(embedding_bridge)]
        for future in futures:
            future.result()
    if not (a.run_dir / "hybrid/complete.json").exists():
        run("hybrid", common + ["--instruction", "original", "--retrieval", retrieval,
                               "--output", a.run_dir / "hybrid"], gpu=2)
    if not (a.run_dir / "results.json").exists():
        run("evaluate", ["evaluate", "--bundle", bundle, "--gold", a.gold, "--retrieval", retrieval,
                         "--run-dir", a.run_dir, "--output", a.run_dir / "results.json"])
    write_new(a.run_dir / "complete.json", {"results_sha256": sha(a.run_dir / "results.json")})


def main():
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    q = sub.add_parser("prepare")
    for flag in ("corpus", "frozen-inputs", "output"):
        q.add_argument("--" + flag, type=Path, required=True)
    q = sub.add_parser("embed")
    for flag in ("bundle", "model", "output"):
        q.add_argument("--" + flag, type=Path, required=True)
    q = sub.add_parser("score")
    for flag in ("bundle", "model", "model-manifest", "output"):
        q.add_argument("--" + flag, type=Path, required=True)
    q.add_argument("--instruction", choices=["original", "bridge"], required=True)
    q.add_argument("--retrieval", type=Path)
    q = sub.add_parser("evaluate")
    for flag in ("bundle", "gold", "retrieval", "run-dir", "output"):
        q.add_argument("--" + flag, type=Path, required=True)
    q = sub.add_parser("orchestrate")
    for flag in ("corpus", "frozen-inputs", "gold", "model", "model-manifest", "embedding-model", "embedding-python", "run-dir"):
        q.add_argument("--" + flag, type=Path, required=True)
    a = p.parse_args()
    globals()[a.command](a)


if __name__ == "__main__":
    main()
