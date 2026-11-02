"""Validate QA gold against the current Zotero knowledge snapshot.

This is a preflight check, not a quality score. It catches stale paper IDs,
wrong pages, quotes that are not present in the cited page, duplicate case IDs,
and accidental use of a development/synthetic set as a frozen test set.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from research_assistant.knowledge.store import KnowledgeStore, compact_text


def _error(errors, message):
    if len(errors) < 100:
        errors.append(message)


def validate_dataset(root, dataset_path, expected_split=None, allow_synthetic=False):
    raw = dataset_path.read_bytes()
    try:
        dataset = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {"valid": False, "errors": [f"dataset is not UTF-8 JSON: {exc}"]}
    errors = []
    if not isinstance(dataset, dict) or not isinstance(dataset.get("id"), str) or not dataset["id"].strip():
        _error(errors, "dataset.id is required")
    split = dataset.get("split") if isinstance(dataset, dict) else None
    if split not in {"development", "test", "synthetic"}:
        _error(errors, "dataset.split must be development, test, or synthetic")
    if split == "synthetic" and not allow_synthetic:
        _error(errors, "synthetic datasets require --allow-synthetic")
    if expected_split is not None and split != expected_split:
        _error(errors, f"dataset split is {split!r}, expected {expected_split!r}")
    cases = dataset.get("cases") if isinstance(dataset, dict) else None
    if not isinstance(cases, list) or not cases:
        _error(errors, "dataset.cases must be a non-empty list")
        cases = []
    store = KnowledgeStore(root)
    documents, _, _ = store.corpus()
    seen_case_ids = set()
    checked = 0
    for index, case in enumerate(cases):
        prefix = f"case[{index}]"
        if not isinstance(case, dict):
            _error(errors, f"{prefix} must be an object")
            continue
        case_id = case.get("id")
        if not isinstance(case_id, str) or not case_id.strip():
            _error(errors, f"{prefix}.id is required")
        elif case_id in seen_case_ids:
            _error(errors, f"duplicate case id: {case_id}")
        else:
            seen_case_ids.add(case_id)
        if not isinstance(case.get("query"), str) or not 1 <= len(case["query"].strip()) <= 2000:
            _error(errors, f"{prefix}.query must be 1..2000 characters")
        kb_ids = case.get("kb_ids")
        if (not isinstance(kb_ids, list) or not kb_ids or
                not all(isinstance(value, str) and value.strip() for value in kb_ids) or
                len(set(kb_ids)) != len(kb_ids)):
            _error(errors, f"{prefix}.kb_ids must contain unique knowledge-base IDs")
            kb_ids = []
        try:
            snapshot = store.snapshot(kb_ids) if kb_ids else {"entities": []}
        except Exception as exc:
            _error(errors, f"{prefix}: knowledge-base snapshot failed ({type(exc).__name__})")
            snapshot = {"entities": []}
        allowed = {entity["data"].get("hash") + ".pdf"
                   for entity in snapshot["entities"]
                   if entity["data"].get("kind") == "document" and entity["data"].get("hash")}
        expected = case.get("expected")
        no_answer = case.get("no_answer")
        if type(no_answer) is not bool:
            _error(errors, f"{prefix}.no_answer must be boolean")
            no_answer = bool(no_answer)
        if not isinstance(expected, list):
            _error(errors, f"{prefix}.expected must be a list")
            expected = []
        if bool(expected) == no_answer:
            _error(errors, f"{prefix}: no_answer must be true exactly when expected is empty")
        seen_gold = set()
        for gold_index, gold in enumerate(expected):
            label = f"{prefix}.expected[{gold_index}]"
            if not isinstance(gold, dict):
                _error(errors, f"{label} must be an object")
                continue
            source = gold.get("source_id")
            page = gold.get("page")
            quote = gold.get("quote")
            if not isinstance(source, str) or not source.endswith(".pdf"):
                _error(errors, f"{label}.source_id must identify a PDF")
                continue
            if source not in allowed:
                _error(errors, f"{label}: source is not in the selected knowledge base")
                continue
            if type(page) is not int or page < 1:
                _error(errors, f"{label}.page must be a positive integer")
                continue
            if not isinstance(quote, str) or not quote.strip():
                _error(errors, f"{label}.quote is required")
                continue
            key = (source, page, compact_text(quote))
            if key in seen_gold:
                _error(errors, f"{label}: duplicate gold evidence")
            seen_gold.add(key)
            pages = documents.get(source[:-4], [])
            target = next((item for item in pages if item.get("page") == page), None)
            if target is None:
                _error(errors, f"{label}: page does not exist in the stored PDF")
            elif compact_text(quote) not in compact_text(target.get("text", "")):
                _error(errors, f"{label}: quote is not present in the cited page")
            else:
                checked += 1
    return {"valid": not errors, "dataset_id": dataset.get("id") if isinstance(dataset, dict) else None,
            "split": split, "expected_split": expected_split,
            "dataset_sha256": hashlib.sha256(raw).hexdigest(), "case_count": len(cases),
            "checked_evidence": checked, "errors": errors}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--expected-split", choices=["development", "test", "synthetic"])
    parser.add_argument("--allow-synthetic", action="store_true")
    args = parser.parse_args()
    report = validate_dataset(args.root, args.dataset, args.expected_split, args.allow_synthetic)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["valid"] else 1)
