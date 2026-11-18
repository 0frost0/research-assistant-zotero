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
from research_assistant.knowledge.qa import (EvidenceQA, initial_queries, merge_candidates,
                                              normalized, select_candidates, algorithm_settings,
                                              rerank_candidates)
from research_assistant.knowledge.service import KnowledgeService, search_ready
from research_assistant.knowledge.store import KnowledgeStore
from research_assistant.core.service_context import stored_service_context
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


def evaluation_arms(strategies, gates, with_model):
    """Same strategy/algorithm with gate off/on; no hidden paid gate-only run."""
    if "on" in gates and not with_model:
        raise ValueError("sufficiency gate evaluation requires --with-model (paid calls)")
    if len(set(strategies)) != len(strategies) or len(set(gates)) != len(gates):
        raise ValueError("duplicate evaluation arms are not allowed")
    return [(strategy, gate == "on") for strategy in strategies for gate in gates]


def model_call_bound(arms):
    return sum(4 if strategy == "adaptive" else 3 if gate else 2 for strategy, gate in arms)


async def evaluate(args):
    arms = evaluation_arms(args.strategies, args.sufficiency_gates, args.with_model)
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
              "policy": "zotero-evidence-v3",
              "sufficiency_gates": args.sufficiency_gates,
              "max_generation_calls_per_case": model_call_bound(arms) if args.with_model else 0,
              "quality_boundary": "Uncalibrated model judgment; human support fields remain unscored.",
              "dataset_validation": {k: v for k, v in validation.items() if k != "errors"},
              "cases": []}
    for i, case in enumerate(cases):
        if bool(case["expected"]) == bool(case["no_answer"]):
            raise ValueError("Expected evidence must be empty only for no-answer cases")
        snapshot = store.snapshot(case["kb_ids"])
        versions = {b["id"]: b["revision"] for b in snapshot["bases"]}
        retrieval_cache = {}
        # Alternate order to reduce (not eliminate) warm-cache/provider order bias.
        ordered_arms = arms[i % len(arms):] + arms[:i % len(arms)]
        for strategy, gate in ordered_arms:
            options = {**case, "strategy": strategy, "sufficiency_gate": gate}
            if args.algorithm:
                options["algorithm"] = args.algorithm
            algorithm, mode = algorithm_settings(options, strategy)
            if mode not in retrieval_cache:
                begin = time.monotonic()
                found = await search_ready(service, {**case, "scope": "original", "k": 20,
                                                     "text_only": True, "retrieval_mode": mode})
                retrieval_cache[mode] = found, round((time.monotonic() - begin) * 1000)
            found, retrieval_ms = retrieval_cache[mode]
            actual_backend, actual_warning = found["backend"], found.get("warning")
            if args.with_model:
                result = await qa.ask(options)
                evidence, trace = result["evidence"], result["trace"]
            else:
                pool = found["results"]
                search_count, extra_ms = 1, 0
                for query in initial_queries(case["query"], case["query"], strategy)[1:]:
                    stage = time.monotonic()
                    extra = await search_ready(service, {**case, "query": query, "scope": "original",
                                                          "k": 20, "text_only": True, "retrieval_mode": mode})
                    extra_ms += round((time.monotonic() - stage) * 1000)
                    search_count += 1
                    pool = merge_candidates(extra["results"], pool)
                pool, rerank = await rerank_candidates(pool, case["query"],
                    enabled=algorithm in {"hybrid_rerank", "hybrid_set"})
                evidence = select_candidates(pool, strategy, question=case["query"],
                    selection_mode="complementary" if algorithm == "hybrid_set" else "legacy")
                trace = {"model_calls": 0, "total_tokens": 0, "unknown_usage_calls": 0,
                         "algorithm": algorithm, "rerank": rerank,
                         "candidate_recall": evidence_metrics(pool, case["expected"])["recall_at_6"],
                         "retrieval_calls": search_count, "candidates": len(pool),
                         "retrieval_ms": retrieval_ms + extra_ms}
                result = {"status": "retrieval_only", "claims": []}
            store.assert_current(snapshot)
            report["cases"].append({"id": case["id"], "strategy": strategy, "algorithm": algorithm,
                "sufficiency_gate": gate,
                "arm": strategy + ("+gate" if gate else "+no_gate"),
                **evidence_metrics(evidence, case["expected"]), "initial_retrieval_ms": retrieval_ms,
                 "retrieval_backend": trace.get("retrieval_backend", actual_backend), "warning": actual_warning,
                "revisions": versions,
                "no_answer": case["no_answer"], "status": result["status"], "trace": trace,
                "completed": result["status"] in {"answered", "partial", "no_evidence"} if args.with_model else None,
                "false_answer": bool(result["claims"]) if case["no_answer"] and args.with_model and result["status"] in {"answered", "partial", "no_evidence"} else None,
                "claims": result["claims"], "evidence": evidence,
                "human_support": None, "human_comparability": None})
        print(f"{i + 1}/{len(cases)}", flush=True)
    report["summary"] = {}
    for arm in sorted({c["arm"] for c in report["cases"]}):
        rows = [c for c in report["cases"] if c["arm"] == arm]
        metrics = {}
        for key in ("recall_at_6", "mrr_at_6", "false_answer", "completed"):
            values = [c[key] for c in rows if c[key] is not None]
            metrics[key] = {"mean": statistics.mean(values) if values else None, "n": len(values)}
        metrics["known_tokens"] = sum(c["trace"]["total_tokens"] for c in rows)
        metrics["unknown_usage_calls"] = sum(c["trace"]["unknown_usage_calls"] for c in rows)
        times = [c["trace"]["elapsed_ms"] for c in rows if "elapsed_ms" in c["trace"]]
        metrics["median_elapsed_ms"] = statistics.median(times) if times else None
        key = rows[0]["strategy"] if args.sufficiency_gates == ["off"] else arm
        report["summary"][key] = metrics
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--backend", choices=["tfidf", "multimodal"], default="multimodal")
    parser.add_argument("--with-model", action="store_true", help="Paid generation: default <=8 calls/case; all gate off/on arms <=18")
    parser.add_argument("--sufficiency-gates", nargs='+', choices=['off', 'on'], default=['off'],
                        help="Controlled gate ablation; on requires --with-model. Default unchanged.")
    parser.add_argument("--model-config", type=Path)
    parser.add_argument("--connection", help="Use the service profile saved in Zotero instead of relying on .env")
    parser.add_argument("--case-ids", nargs='+')
    parser.add_argument("--strategies", nargs='+', choices=['fixed', 'selected', 'adaptive'], default=['fixed', 'selected', 'adaptive'])
    parser.add_argument("--algorithm", choices=['legacy', 'hybrid', 'hybrid_rerank', 'hybrid_set'],
                        help="Freeze an algorithm for ablation; default adaptive uses hybrid_rerank")
    parser.add_argument("--max-cases", type=int, default=3)
    parser.add_argument("--expected-split", choices=["development", "test", "synthetic"],
                        help="Refuse to score a dataset declared with another split.")
    parser.add_argument("--allow-synthetic", action="store_true",
                        help="Allow the explicitly declared synthetic smoke dataset.")
    args = parser.parse_args()
    if args.max_cases < 1:
        parser.error("max-cases must be positive")
    load_dotenv(args.model_config or args.root / ".env")
    with stored_service_context(args.root, args.connection):
        asyncio.run(evaluate(args))
