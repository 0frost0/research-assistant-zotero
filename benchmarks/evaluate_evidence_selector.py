"""Same-pool offline selector metrics. Reports IDs/counts only, no excerpts.

Synthetic evaluation is protocol verification, not a real-paper quality claim.
Unjudged anchor-proxy candidates are refused rather than counted as negatives.
"""
import argparse
import hashlib
import json
import math
from pathlib import Path
from statistics import mean
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from research_assistant.retrieval.evidence_selection import select_complementary
from research_assistant.retrieval.learned_selector import greedy_select
from research_assistant.retrieval.selector_dataset import MODEL, REVISION, load_dataset

# Same instruction as production reranking.INSTRUCTION; duplicated here to
# keep the offline worker free from knowledge-store/client dependencies.
RELEVANCE_INSTRUCTION = (
    "Retrieve passages that directly answer the question, preserving its conditions, entities and numbers. "
    "For a definition question, prefer a passage explaining what the requested concept is, "
    "not a passage that merely mentions it."
)


def valid_scores(scores, expected):
    return (len(scores) == expected and all(
        isinstance(s, (int, float)) and not isinstance(s, bool) and math.isfinite(s) and 0 <= s <= 1
        for s in scores))


def relevance_cache_key(question, documents):
    """Identical question wording across papers must not share pool scores."""
    return hashlib.sha256(json.dumps([question, list(documents)], ensure_ascii=False).encode()).hexdigest()


def set_metrics(group, selected):
    target = {d["id"] for d in group["dimensions"]}
    pool = {d for c in group["candidates"] for d in c["covers"]}
    covered, redundant, irrelevant = set(), 0, 0
    for candidate in selected:
        dimensions = set(candidate["covers"])
        redundant += int(bool(dimensions) and dimensions <= covered)
        irrelevant += int(not dimensions)
        covered.update(dimensions)
    return {"candidate_dimension_recall": len(pool & target) / len(target),
            "dimension_coverage_at_6": len(covered & target) / len(target),
            "available_dimension_recall_at_6": len(covered & pool) / len(pool) if pool else None,
            "all_required_dimensions_at_6": int(target <= covered),
            "redundant_selection_rate": redundant / len(selected) if selected else 0,
            "off_target_selection_rate": irrelevant / len(selected) if selected else 0,
            "missing_candidate_dimensions": sorted(target - pool),
            "uncovered_dimensions": sorted(target - covered),
            "by_category": {category: mean(int(d["id"] in covered) for d in group["dimensions"]
                                              if d["category"] == category)
                            for category in {d["category"] for d in group["dimensions"]}}}


def top_k(candidates, *, char_budget=12000):
    chosen, size = [], 0
    for c in candidates:
        if len(chosen) < 6 and size + len(c["quote"]) <= char_budget:
            chosen.append(c)
            size += len(c["quote"])
    return chosen


def evaluate(dataset, *, split="dev", relevance_scorer=None, selector_scorer=None, untrained_selector_scorer=None):
    if dataset["provenance"]["label_status"] == "development_anchor_proxy":
        raise ValueError("anchor-only unjudged material cannot establish dimension coverage")
    rows = []
    for group in dataset["groups"]:
        if group["split"] != split:
            continue
        candidates = group["candidates"]
        arms = {"fixed_top_k": top_k(candidates),
                "heuristic_untrained": select_complementary(candidates, group["question"])}
        if relevance_scorer:
            scores = relevance_scorer(group["question"], [c["quote"] for c in candidates])
            if not valid_scores(scores, len(candidates)):
                bad = [(i, repr(s), type(s).__name__) for i, s in enumerate(scores)
                       if not isinstance(s, (int, float)) or isinstance(s, bool) or
                       not math.isfinite(s) or not 0 <= s <= 1][:3]
                raise ValueError(f"invalid reranker scores group={group['id']} count={len(scores)} bad={bad}")
            ranked = [{**c, "ranking": {"rerank_score": score}} for c, score in zip(candidates, scores)]
            ranked.sort(key=lambda c: c["ranking"]["rerank_score"], reverse=True)
            arms["qwen_untrained_top_k"] = top_k(ranked)
            arms["qwen_plus_heuristic"] = select_complementary(ranked, group["question"])
        if selector_scorer:
            arms["lora_selector"] = greedy_select(candidates, group["question"], selector_scorer)
        if untrained_selector_scorer:
            arms["qwen_untrained_set_prompt"] = greedy_select(candidates, group["question"], untrained_selector_scorer)
        for arm, selected in arms.items():
            rows.append({"group_id": group["id"], "paper": group["paper"], "arm": arm,
                         "candidate_set_sha256": hashlib.sha256(json.dumps(sorted(c["id"] for c in candidates)).encode()).hexdigest(),
                         "selected_ids": [c["id"] for c in selected], **set_metrics(group, selected)})
    if not rows:
        raise ValueError("no evaluation groups in requested split")
    metrics = ["candidate_dimension_recall", "dimension_coverage_at_6", "available_dimension_recall_at_6",
               "all_required_dimensions_at_6", "redundant_selection_rate", "off_target_selection_rate"]
    summary = {arm: {key: mean(r[key] for r in rows if r["arm"] == arm and r[key] is not None)
                    if any(r["arm"] == arm and r[key] is not None for r in rows) else None
                    for key in metrics} for arm in sorted({r["arm"] for r in rows})}
    return {"dataset_id": dataset["id"], "label_status": dataset["provenance"]["label_status"],
            "split": split, "independent_test": False, "effectiveness_claim": False,
            "production_writes": False, "answer_model_calls": 0, "summary": summary, "rows": rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--split", choices=["dev", "test"], default="dev")
    parser.add_argument("--base-model", type=Path)
    parser.add_argument("--model-manifest", type=Path)
    parser.add_argument("--adapter", type=Path)
    args = parser.parse_args()
    if args.adapter and (args.base_model is None or args.model_manifest is None):
        parser.error("--adapter requires local --base-model and --model-manifest")
    data = load_dataset(args.dataset)
    if args.output.exists():
        raise FileExistsError("report already exists")
    relevance, selector, untrained = None, None, None
    if args.adapter:
        import torch
        from peft import PeftModel
        from deployment.embedding.remote_reranker_service import Scorer
        from benchmarks.train_evidence_selector import bounded_encode
        manifest = json.loads(args.model_manifest.read_text())
        trained = json.loads((args.adapter / "manifest.json").read_text())
        dataset_sha = hashlib.sha256(args.dataset.read_bytes()).hexdigest()
        if (manifest.get("repo") != MODEL or manifest.get("revision") != REVISION or
                Path(manifest.get("path", "")).resolve() != args.base_model.resolve() or
                trained.get("dataset_sha256") != dataset_sha or trained.get("revision") != REVISION):
            raise ValueError("adapter/base/dataset manifest mismatch")
        scorer = Scorer(str(args.base_model))
        # Baseline and adapter use the exact same base weights, sequentially.
        score_cache = {}
        for group in data["groups"]:
            if group["split"] == args.split:
                documents = [c["quote"] for c in group["candidates"]]
                score_cache[relevance_cache_key(group["question"], documents)] = scorer.scores(
                    group["question"], documents, RELEVANCE_INSTRUCTION)
        relevance = lambda q, docs: score_cache[relevance_cache_key(q, docs)]
        scorer.model = PeftModel.from_pretrained(scorer.model, args.adapter).eval()
        def score_prompts(prompts):
            scores = []
            with torch.inference_mode():
                for start in range(0, len(prompts), 4):
                    encoded = bounded_encode(scorer.tokenizer, prompts[start:start + 4], 4096).to("cuda")
                    logits = scorer.model(**encoded, logits_to_keep=1).logits[:, -1, [scorer.no, scorer.yes]].float()
                    scores.extend(logits.softmax(-1)[:, 1].cpu().tolist())
            return scores
        selector = score_prompts
        def untrained(prompts):
            with scorer.model.disable_adapter():
                return score_prompts(prompts)
    report = evaluate(data, split=args.split, relevance_scorer=relevance, selector_scorer=selector,
                      untrained_selector_scorer=untrained)
    report["evaluation_protocol"] = "selector-same-pool-v2-question-and-ordered-documents-cache"
    report["evaluator_sha256"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    if args.adapter:
        report["adapter_manifest_sha256"] = hashlib.sha256((args.adapter / "manifest.json").read_bytes()).hexdigest()
        report["base_model"] = MODEL
        report["revision"] = REVISION
    report["dataset_sha256"] = hashlib.sha256(args.dataset.read_bytes()).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    print(json.dumps({k: v for k, v in report.items() if k != "rows"}, indent=2))


if __name__ == "__main__":
    main()
