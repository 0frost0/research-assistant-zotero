"""Reproducible Zotero QA ablation. No paid generation unless --with-model.

Input: {id, split, cases:[{id, query, kb_ids, expected:[{source_id,page,quote}],
no_answer:bool}]}. Reference quotes must be checked against original papers.
Use separate development/test files; output records the exact dataset hash.
"""
import argparse
import asyncio
import hashlib
import json
from pathlib import Path
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv
from research_assistant.knowledge.qa import EvidenceQA, normalized, select_candidates
from research_assistant.knowledge.service import KnowledgeService
from research_assistant.knowledge.store import KnowledgeStore
from benchmarks.validate_knowledge_qa_dataset import validate_dataset


def check_split(dataset, expected_split=None):
    """Require an explicit split contract before scoring a QA dataset."""
    split = dataset.get("split")
    if split not in {"development", "test", "synthetic"}:
        raise ValueError("dataset split must be development, test, or synthetic")
    if expected_split is not None and split != expected_split:
        raise ValueError(f"dataset split is {split!r}, expected {expected_split!r}")
    return split


def evidence_metrics(evidence, expected):
    matched, first = set(), None
    for rank, evidence_item in enumerate(evidence, 1):
        for index, gold in enumerate(expected):
            if (gold["source_id"] == evidence_item["source_id"] and gold["page"] == evidence_item["page"] and
                    normalized(gold["quote"]) in normalized(evidence_item["quote"])):
                matched.add(index)
                first = first or rank
    return {"recall_at_6": len(matched) / len(expected) if expected else None,
            "mrr_at_6": (1 / first if first else 0) if expected else None}


async def evaluate(args):
    raw = args.dataset.read_bytes()
    dataset = json.loads(raw)
    split = check_split(dataset, args.expected_split)
    validation = validate_dataset(args.root, args.dataset, args.expected_split, args.allow_synthetic)
    if not validation["valid"]:
        raise ValueError("QA dataset validation failed: " + "; ".join(validation["errors"][:8]))
    cases = [c for c in dataset["cases"] if not args.case_ids or c['id'] in args.case_ids][:args.max_cases]
    if not cases:
        raise ValueError("No evaluation cases")
    store = KnowledgeStore(args.root)
    service = KnowledgeService(store, args.backend)
    qa = EvidenceQA(service)
    report = {"dataset_id": dataset["id"], "dataset_sha256": hashlib.sha256(raw).hexdigest(),
              "split": split, "expected_split": args.expected_split,
              "with_model": args.with_model, "backend": args.backend,
              "case_limit": args.max_cases, "k": 6, "char_budget": 12000,
              "dataset_validation": {k: v for k, v in validation.items() if k != "errors"},
              "cases": []}
    for i, case in enumerate(cases):
        if bool(case["expected"]) == bool(case["no_answer"]):
            raise ValueError("Expected evidence must be empty only for no-answer cases")
        snapshot = store.snapshot(case["kb_ids"])
        versions = {b["id"]: b["revision"] for b in snapshot["bases"]}
        begin = time.monotonic()
        found = service.search({**case, "scope": "original", "k": 20})
        retrieval_ms = round((time.monotonic() - begin) * 1000)
        strategies = list(args.strategies) if args.with_model else ["fixed", "selected"]
        # Alternate order to reduce (not eliminate) warm-cache/provider order bias.
        strategies = strategies[i % len(strategies):] + strategies[:i % len(strategies)]
        for strategy in strategies:
            if args.with_model:
                result = await qa.ask({**case, "strategy": strategy})
                evidence, trace = result["evidence"], result["trace"]
            else:
                evidence = select_candidates(found["results"], strategy)
                trace = {"model_calls": 0, "total_tokens": 0, "unknown_usage_calls": 0}
                result = {"status": "retrieval_only", "claims": []}
            store.assert_current(snapshot)
            report["cases"].append({"id": case["id"], "strategy": strategy,
                **evidence_metrics(evidence, case["expected"]), "initial_retrieval_ms": retrieval_ms,
                "retrieval_backend": found["backend"], "warning": found.get("warning"),
                "revisions": versions,
                "no_answer": case["no_answer"], "status": result["status"], "trace": trace,
                "completed": result["status"] in {"answered", "partial", "no_evidence"} if args.with_model else None,
                "false_answer": bool(result["claims"]) if case["no_answer"] and args.with_model and result["status"] in {"answered", "partial", "no_evidence"} else None,
                "claims": result["claims"], "evidence": evidence,
                "human_support": None, "human_comparability": None})
        print(f"{i + 1}/{len(cases)}", flush=True)
    report["summary"] = {}
    for strategy in sorted({c["strategy"] for c in report["cases"]}):
        rows = [c for c in report["cases"] if c["strategy"] == strategy]
        metrics = {}
        for key in ("recall_at_6", "mrr_at_6", "false_answer", "completed"):
            values = [c[key] for c in rows if c[key] is not None]
            metrics[key] = {"mean": statistics.mean(values) if values else None, "n": len(values)}
        metrics["known_tokens"] = sum(c["trace"]["total_tokens"] for c in rows)
        metrics["unknown_usage_calls"] = sum(c["trace"]["unknown_usage_calls"] for c in rows)
        times = [c["trace"]["elapsed_ms"] for c in rows if "elapsed_ms" in c["trace"]]
        metrics["median_elapsed_ms"] = statistics.median(times) if times else None
        report["summary"][strategy] = metrics
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--backend", choices=["tfidf", "multimodal"], default="multimodal")
    parser.add_argument("--with-model", action="store_true", help="Makes real API calls (at most 8/case across three strategies)")
    parser.add_argument("--model-config", type=Path)
    parser.add_argument("--case-ids", nargs='+')
    parser.add_argument("--strategies", nargs='+', choices=['fixed', 'selected', 'adaptive'], default=['fixed', 'selected', 'adaptive'])
    parser.add_argument("--max-cases", type=int, default=3)
    parser.add_argument("--expected-split", choices=["development", "test", "synthetic"],
                        help="Refuse to score a dataset declared with another split.")
    parser.add_argument("--allow-synthetic", action="store_true",
                        help="Allow the explicitly declared synthetic smoke dataset.")
    args = parser.parse_args()
    if args.max_cases < 1:
        parser.error("max-cases must be positive")
    load_dotenv(args.model_config or args.root / ".env")
    asyncio.run(evaluate(args))
