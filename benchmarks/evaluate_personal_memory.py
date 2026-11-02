"""Evaluate current personal-note retrieval without calling a model.

Dataset format::

    {"id": "memory-v1", "split": "development|test",
     "cases": [{"id": "M1", "query": "...", "kb_ids": ["..."],
                "expected": [{"entity_id": "...", "revision": 2,
                               "quote": "..."}], "no_answer": false}]}

The script measures retrieval and provenance only. It does not claim that a
later generated answer is scientifically correct.
"""
import argparse
import hashlib
import json
from pathlib import Path
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from research_assistant.knowledge.service import KnowledgeService
from research_assistant.knowledge.store import KnowledgeStore
from research_assistant.knowledge.qa import PERSONAL_NOTE_KINDS, note_candidate_relevant, normalized


def check_split(dataset, expected_split=None):
    """Refuse to score a dataset under a different declared split."""
    split = dataset.get("split")
    if split not in {"development", "test"}:
        raise ValueError("memory dataset split must be development or test")
    if expected_split is not None and split != expected_split:
        raise ValueError(f"dataset split is {split!r}, expected {expected_split!r}")
    return split


def memory_metrics(results, expected, k=6):
    ranked = results[:k]
    matched, first = set(), None
    revision_hits, links, personal_count = 0, 0, 0
    for rank, item in enumerate(ranked, 1):
        if item.get("kind") not in PERSONAL_NOTE_KINDS:
            continue
        personal_count += 1
        if item.get("url", "").startswith(("/knowledge?", "zotero://")):
            links += 1
        for index, gold in enumerate(expected):
            same_identity = item.get("entity_id") == gold.get("entity_id")
            same_kind = not gold.get("kind") or item.get("kind") == gold.get("kind")
            same_revision = item.get("revision") == gold.get("revision")
            quote = normalized(gold.get("quote", ""))
            same_quote = bool(quote) and quote in normalized(item.get("quote", ""))
            if same_identity and same_kind and same_quote:
                matched.add(index)
                revision_hits += int(same_revision)
                first = first or rank
    return {
        "recall_at_6": len(matched) / len(expected) if expected else None,
        "mrr_at_6": (1 / first if first else 0) if expected else None,
        "revision_accuracy": revision_hits / len(matched) if matched else None,
        "source_link_rate": links / personal_count if personal_count else None,
        "returned": len(ranked),
    }


def evaluate(args):
    raw = args.dataset.read_bytes()
    dataset = json.loads(raw)
    split = check_split(dataset, args.expected_split)
    cases = [c for c in dataset.get("cases", []) if not args.case_ids or c["id"] in args.case_ids]
    cases = cases[:args.max_cases]
    if not cases:
        raise ValueError("No evaluation cases")
    store = KnowledgeStore(args.root)
    service = KnowledgeService(store, args.backend)
    report = {"dataset_id": dataset["id"], "dataset_sha256": hashlib.sha256(raw).hexdigest(),
              "split": split, "expected_split": args.expected_split,
              "backend": args.backend, "k": 6, "cases": []}
    for index, case in enumerate(cases, 1):
        expected = case.get("expected", [])
        if bool(expected) == bool(case.get("no_answer")):
            raise ValueError(f"case {case['id']} has inconsistent expected/no_answer")
        snapshot = store.snapshot(case["kb_ids"])
        revisions = {base["id"]: base["revision"] for base in snapshot["bases"]}
        start = time.monotonic()
        found = service.search({"query": case["query"], "kb_ids": case["kb_ids"], "scope": "notes", "k": 20})
        store.assert_current(snapshot)
        elapsed = round((time.monotonic() - start) * 1000)
        raw_results = found.get("results", [])
        results = ([item for item in raw_results
                    if note_candidate_relevant(case["query"], item, args.min_coverage)]
                   if args.apply_memory_gate else raw_results)
        metrics = memory_metrics(results, expected)
        row = {"id": case["id"], "query": case["query"], "no_answer": bool(case.get("no_answer")),
               "revisions": revisions,
               "warning": found.get("warning"), "retrieval_backend": found.get("backend"),
               "elapsed_ms": elapsed, "gate_enabled": args.apply_memory_gate,
               "gate_min_coverage": args.min_coverage,
               "gate_filtered": len(raw_results) - len(results), **metrics,
               "false_answer": bool(results) if case.get("no_answer") else None}
        report["cases"].append(row)
        print(f"{index}/{len(cases)} {case['id']}", flush=True)
    numeric = ["recall_at_6", "mrr_at_6", "revision_accuracy", "source_link_rate", "false_answer"]
    report["summary"] = {key: {"mean": statistics.mean([r[key] for r in report["cases"] if r[key] is not None]),
                                "n": sum(r[key] is not None for r in report["cases"])}
                          for key in numeric}
    report["summary"]["median_elapsed_ms"] = statistics.median(r["elapsed_ms"] for r in report["cases"])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--backend", choices=["tfidf", "multimodal"], default="tfidf")
    parser.add_argument("--expected-split", choices=["development", "test"],
                        help="Require the dataset to have this split; use test for a frozen holdout run.")
    parser.add_argument("--apply-memory-gate", action="store_true",
                        help="Apply the same explainable lexical gate used by MemoryQA")
    parser.add_argument("--min-coverage", type=float, default=0.20,
                        help="Character n-gram coverage used when --apply-memory-gate is enabled")
    parser.add_argument("--case-ids", nargs="+")
    parser.add_argument("--max-cases", type=int, default=100)
    args = parser.parse_args()
    if args.max_cases < 1:
        parser.error("max-cases must be positive")
    if not 0 <= args.min_coverage <= 1:
        parser.error("min-coverage must be between 0 and 1")
    evaluate(args)
