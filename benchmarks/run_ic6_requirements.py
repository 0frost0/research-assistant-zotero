"""Gold-free question decomposition and facility-location evidence selection.

Exploratory adaptation, not SetR reproduction. Fixed parameters, no test tuning.
"""
import argparse
import json
import math
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmarks.run_ic6_retrieval import (
    load, sha, write_new, check_manifest, model_fingerprint, validate_bundle,
    read_cache, validate_scores, BRIDGE,
)

PROMPT = '''Break the question into 2 to 4 distinct factual information needs that together suffice to answer it. Preserve all named entities, sources, dates, numbers and comparison conditions. For a comparison include the required fact for each object separately. For a dependent inference describe the unknown intermediate entity relationally; never guess its name or answer the question. Do not introduce background requirements not requested. Each need must be a self-contained retrieval query. Return ONLY a JSON array of 2 to 4 strings, no markdown or explanation. Every array element MUST be a plain JSON string, NEVER an object or dictionary. Do not use keys such as query or requirement. Required schema example: ["First self-contained factual question?", "Second self-contained factual question?"]'''
VERSION = "ic6-requirements-v2"


def parse_needs(text):
    values = json.loads(text)
    if (not isinstance(values, list) or not 2 <= len(values) <= 4 or
            any(not isinstance(v, str) or not 8 <= len(v.strip()) <= 1500 for v in values) or
            len(set(v.strip() for v in values)) != len(values)):
        raise ValueError("Invalid information needs")
    return [v.strip() for v in values]


def coverage_select(ids, original, need_scores):
    """Six steps: .7 mean rank-calibrated coverage + .3 original relevance.

    Each need uses exp(-rank/10), equal scores share rank; no probability claim.
    """
    n = len(ids)
    if n < 6 or len(set(ids)) != n or len(original) != n or not need_scores:
        raise ValueError("Invalid coverage selection inputs")

    def ranks(scores):
        if len(scores) != n:
            raise ValueError("Score length mismatch")
        order = sorted(range(n), key=lambda i: (-scores[i], i))
        result = [0.0] * n
        first_rank = 0
        for rank, i in enumerate(order):
            if rank == 0 or scores[i] != scores[order[rank - 1]]:
                first_rank = rank
            result[i] = math.exp(-first_rank / 10)
        return result

    base = ranks(original)
    needs = [ranks(scores) for scores in need_scores]
    covered = [0.0] * len(needs)
    chosen = []
    for _ in range(6):
        def gain(i):
            return .7 * sum(max(0, scores[i] - seen) for scores, seen in zip(needs, covered)) / len(needs) + (.3 / 6) * base[i]
        selected = max((i for i in range(n) if i not in chosen), key=lambda i: (gain(i), -i))
        chosen.append(selected)
        covered = [max(seen, scores[selected]) for scores, seen in zip(needs, covered)]
    return [ids[i] for i in chosen]


def decompose(a):
    import torch
    from transformers import AutoTokenizer, AutoModelForCausalLM
    data = load(a.bundle)
    validate_bundle(data)
    a.output.mkdir(exist_ok=True)
    check_manifest(a.output / "manifest.json", {
        "version": VERSION, "bundle": sha(a.bundle), "script": sha(__file__),
        "model": model_fingerprint(a.model), "prompt": PROMPT, "max_new_tokens": 512,
        "max_input_tokens": 4096, "greedy": True, "enable_thinking": False,
        "limit": a.limit, "dtype": "bfloat16"})
    queries = data["queries"][:a.limit] if a.limit else data["queries"]
    path = a.output / "needs.jsonl"
    cache = read_cache(path, {q["id"] for q in queries})
    tokenizer = AutoTokenizer.from_pretrained(str(a.model), local_files_only=True)
    model = AutoModelForCausalLM.from_pretrained(str(a.model), local_files_only=True,
        torch_dtype=torch.bfloat16, attn_implementation="sdpa").cuda().eval()
    for pos, q in enumerate(queries):
        if q["id"] in cache:
            if cache[q["id"]]["question_sha256"] != __import__('hashlib').sha256(q['question'].encode()).hexdigest():
                raise ValueError("Cached question changed")
            continue
        text = tokenizer.apply_chat_template([{"role": "system", "content": PROMPT},
            {"role": "user", "content": q["question"]}], tokenize=False, add_generation_prompt=True,
            enable_thinking=False)
        inputs = tokenizer(text, return_tensors="pt", truncation=False).to(model.device)
        if inputs.input_ids.shape[1] > 4096:
            raise ValueError("Input would truncate")
        start = time.perf_counter()
        with torch.inference_mode():
            output = model.generate(**inputs, do_sample=False, max_new_tokens=512,
                pad_token_id=tokenizer.eos_token_id)
        generated = output[0, inputs.input_ids.shape[1]:]
        raw = tokenizer.decode(generated, skip_special_tokens=True)
        status, needs = "ok", []
        try:
            if len(generated) >= 512:
                raise ValueError("output limit")
            needs = parse_needs(raw)
        except (ValueError, TypeError):
            status = "invalid"
        row = {"id": q["id"], "question_sha256": __import__('hashlib').sha256(q['question'].encode()).hexdigest(),
               "status": status, "needs": needs, "raw_output": raw,
               "seconds": time.perf_counter() - start, "output_tokens": len(generated)}
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        if pos == 0 or (pos + 1) % 25 == 0:
            print(json.dumps({"decomposed": pos + 1, "total": len(queries), "status": status}), flush=True)
    write_new(a.output / "complete.json", {"needs_sha256": sha(path), "queries": len(queries)})


def predict(a):
    from deployment.embedding.remote_reranker_service import Scorer, MODEL, REVISION
    data = load(a.bundle)
    validate_bundle(data)
    queries = {q["id"]: q for q in data["queries"]}
    retrieval = load(a.retrieval)
    if retrieval['bundle_sha256'] != sha(a.bundle):
        raise ValueError('Retrieval changed')
    complete = load(a.needs / "complete.json")
    if complete["needs_sha256"] != sha(a.needs / "needs.jsonl"):
        raise ValueError("Needs changed")
    needs = read_cache(a.needs / "needs.jsonl", set(queries))
    if set(needs) != set(queries) or load(a.needs / "manifest.json")["limit"]:
        raise ValueError("Incomplete needs; smoke cannot enter full prediction")
    baseline = read_cache(a.baseline / "scores.jsonl", set(queries))
    manifest = load(a.baseline / 'manifest.json')
    if (set(baseline) != set(queries) or manifest['bundle_sha256'] != sha(a.bundle) or
            manifest['instruction'] != BRIDGE or manifest['retrieval_sha256'] != sha(a.retrieval) or
            load(a.baseline / 'complete.json')['scores_sha256'] != sha(a.baseline / 'scores.jsonl')):
        raise ValueError("Incomplete or changed baseline")
    a.output.mkdir(exist_ok=True)
    files = model_fingerprint(a.model)
    pinned = load(a.model_manifest)
    if pinned['repo'] != MODEL or pinned['revision'] != REVISION or any(files.get(k) != v for k,v in pinned['files'].items()):
        raise ValueError('Reranker identity mismatch')
    check_manifest(a.output / "manifest.json", {"version": VERSION, "script": sha(__file__),
        "bundle": sha(a.bundle), "retrieval": sha(a.retrieval), "needs": sha(a.needs / "needs.jsonl"),
        "baseline": sha(a.baseline / "scores.jsonl"), "model": files, "instruction": BRIDGE,
        "coverage_weight": .7, "original_sum_weight": .3 / 6, "rank_transform": "exp(-rank/10), tied scores share first rank", "budget": 6,
        "failure_policy": "empty selection, not fallback", "pool": "hybrid100"})
    path = a.output / "predictions.jsonl"
    cache = read_cache(path, set(queries))
    lookup = {c["id"]: c for c in data["chunks"]}
    model = Scorer(str(a.model))
    for pos, q in enumerate(data["queries"]):
        qid = q["id"]
        ids = retrieval["pools"][qid]["hybrid100"]
        if qid in cache:
            if cache[qid]['candidate_ids'] != ids:
                raise ValueError('Cached pool changed')
            continue
        if needs[qid]['question_sha256'] != __import__('hashlib').sha256(q['question'].encode()).hexdigest():
            raise ValueError('Needs question mismatch')
        union = list(dict.fromkeys(data['bm25'][qid] + retrieval['pools'][qid]['dense100'] + ids))
        validate_scores(baseline[qid], union)
        scores = dict(zip(union, baseline[qid]["scores"]))
        row = {"id": qid, "candidate_ids": ids, "status": needs[qid]["status"],
               "selected_ids": [], "need_scores": [], "seconds": 0.0, "pair_count": 0}
        if row["status"] == "ok":
            parse_needs(json.dumps(needs[qid]['needs']))
            start = time.perf_counter()
            for need in needs[qid]["needs"]:
                values = model.scores(need, [lookup[i]["text"] for i in ids], BRIDGE)
                validate_scores({"candidate_ids": ids, "scores": values, "document_tokens": [1]*len(ids)}, ids)
                row["need_scores"].append(values)
            row["seconds"] = time.perf_counter() - start
            row["pair_count"] = len(ids) * len(row["need_scores"])
            row["selected_ids"] = coverage_select(ids, [scores[i] for i in ids], row["need_scores"])
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, allow_nan=False) + "\n")
            f.flush()
            os.fsync(f.fileno())
        if pos == 0 or (pos + 1) % 25 == 0:
            print(json.dumps({"predicted": pos + 1, "total": len(queries), "status": row["status"]}), flush=True)
    write_new(a.output / "complete.json", {"predictions_sha256": sha(path)})


def evaluate(a):
    from benchmarks.setr_protocol import PROTOCOL, metrics, summarize
    data, gold = load(a.bundle), load(a.gold)
    validate_bundle(data)
    if gold['protocol'] != PROTOCOL or gold['role'] != 'external_test_only':
        raise ValueError('Gold protocol mismatch')
    predictions = read_cache(a.predictions / "predictions.jsonl", {q["id"] for q in data["queries"]})
    if load(a.predictions / 'complete.json')['predictions_sha256'] != sha(a.predictions / 'predictions.jsonl'):
        raise ValueError('Predictions changed')
    queries = {q['id']:q for q in data['queries']}
    if (set(predictions) != set(queries) or len(gold['groups']) != len(queries) or
            {g['id'] for g in gold['groups']} != set(queries)):
        raise ValueError("No partial report")
    lookup = {c["id"]: c for c in data["chunks"]}
    rows = []
    for group in gold["groups"]:
        if group['question'] != queries[group['id']]['question']:
            raise ValueError('Question mismatch')
        p = predictions[group["id"]]
        ids = p["candidate_ids"]
        if len(ids) != 100 or len(set(ids)) != 100 or not set(ids) <= set(lookup):
            raise ValueError('Invalid real corpus pool')
        selected = p["selected_ids"]
        if (p['status']=='ok' and len(selected)!=6) or (p['status']!='ok' and selected):
            raise ValueError('Invalid selection budget/status')
        row = {"id": group["id"], "question_type": group["question_type"], "status": p["status"],
               "selected_ids": selected, **metrics([lookup[i] for i in ids], group["gold"],
                                                   [ids.index(i) for i in selected])}
        if (group['question_type']=='null_query') != (row['gold_count']==0):
            raise ValueError('Null mismatch')
        rows.append(row)
    report = {"version": VERSION, "exploratory_not_independent_test": True, "summary": summarize(rows),
              "slices": {t: summarize([r for r in rows if r["question_type"] == t]) for t in sorted({r["question_type"] for r in rows})},
              "fingerprints": {"gold": sha(a.gold), "bundle": sha(a.bundle), "script": sha(__file__),
                               "predictions": sha(a.predictions / 'predictions.jsonl')}, "rows": rows}
    write_new(a.output, report)
    print(json.dumps(report["summary"], indent=2), flush=True)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    for command, flags in [("decompose", ("bundle", "model", "output")),
                           ("predict", ("bundle", "retrieval", "needs", "baseline", "model", "model-manifest", "output")),
                           ("evaluate", ("bundle", "gold", "predictions", "output"))]:
        s = sub.add_parser(command)
        for flag in flags:
            s.add_argument("--" + flag, type=Path, required=True)
        if command == "decompose":
            s.add_argument("--limit", type=int, default=0)
    args = p.parse_args()
    globals()[args.command](args)
