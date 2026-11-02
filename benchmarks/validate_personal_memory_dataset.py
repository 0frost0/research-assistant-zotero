"""Validate personal-memory gold data against the current SQLite snapshot.

This is a preflight check, not a quality score.  It prevents a frozen dataset
from silently referring to an archived revision, a missing Zotero record, or a
quote that never existed in the recorded note body.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from research_assistant.knowledge.qa import PERSONAL_NOTE_KINDS, normalized
from research_assistant.knowledge.store import KnowledgeStore


def _records(snapshot):
    records = {}
    for note in snapshot["notes"]:
        value = note["data"]
        records[note["id"]] = {
            "kind": "user_note", "revision": note["revision"],
            "confirmed": value.get("confirmed") is True,
            "archived": value.get("archived") is True,
            "text": value.get("body", ""),
        }
    for entity in snapshot["entities"]:
        value = entity["data"]
        if value.get("kind") not in {"note", "annotation"}:
            continue
        records[entity["id"]] = {
            "kind": "external_" + value["kind"], "revision": entity["revision"],
            "confirmed": value.get("confirmed") is True,
            "archived": False,
            "text": value.get("body", "") + "\n" + value.get("comment", ""),
        }
    return records


def validate_dataset(root, dataset_path):
    raw = dataset_path.read_bytes()
    try:
        dataset = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        return {"valid": False, "errors": [f"dataset is not valid UTF-8 JSON: {exc}"]}
    errors = []
    if not isinstance(dataset, dict):
        return {"valid": False, "errors": ["dataset root must be an object"]}
    if not isinstance(dataset.get("id"), str) or not dataset["id"].strip():
        errors.append("dataset.id is required")
    if dataset.get("split") not in {"development", "test"}:
        errors.append("dataset.split must be development or test")
    cases = dataset.get("cases")
    if not isinstance(cases, list) or not cases:
        errors.append("dataset.cases must be a non-empty list")
        cases = []
    seen_cases = set()
    store = KnowledgeStore(root)
    all_records = {}
    for case in cases:
        if not isinstance(case, dict):
            errors.append("each case must be an object")
            continue
        case_id = case.get("id")
        if not isinstance(case_id, str) or not case_id.strip():
            errors.append("case id is required")
            continue
        if case_id in seen_cases:
            errors.append(f"duplicate case id: {case_id}")
        seen_cases.add(case_id)
        query = case.get("query")
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 2000:
            errors.append(f"{case_id}: query must contain 1-2000 characters")
        kb_ids = case.get("kb_ids")
        if not isinstance(kb_ids, list) or not kb_ids or len(kb_ids) > 20 or not all(isinstance(x, str) and x for x in kb_ids):
            errors.append(f"{case_id}: kb_ids must contain 1-20 ids")
            continue
        try:
            snapshots = [store.snapshot([kb_id]) for kb_id in dict.fromkeys(kb_ids)]
        except Exception as exc:
            errors.append(f"{case_id}: cannot load knowledge base snapshot: {type(exc).__name__}")
            continue
        records = {}
        for snapshot in snapshots:
            records.update(_records(snapshot))
        all_records.update(records)
        expected = case.get("expected", [])
        no_answer = case.get("no_answer") is True
        if not isinstance(expected, list) or (bool(expected) == no_answer):
            errors.append(f"{case_id}: expected and no_answer are inconsistent")
            continue
        identities = set()
        for gold in expected:
            if not isinstance(gold, dict):
                errors.append(f"{case_id}: expected item must be an object")
                continue
            identity = (gold.get("entity_id"), gold.get("revision"))
            if identity in identities:
                errors.append(f"{case_id}: duplicate expected identity {identity}")
            identities.add(identity)
            record = records.get(gold.get("entity_id"))
            if record is None:
                errors.append(f"{case_id}: missing record {gold.get('entity_id')}")
                continue
            if gold.get("kind") and gold["kind"] not in PERSONAL_NOTE_KINDS:
                errors.append(f"{case_id}: unsupported expected kind {gold['kind']}")
            if gold.get("kind") and gold["kind"] != record["kind"]:
                errors.append(f"{case_id}: kind mismatch for {gold.get('entity_id')}")
            if record["revision"] != gold.get("revision"):
                errors.append(f"{case_id}: revision is stale for {gold.get('entity_id')}")
            if not record["confirmed"] or record["archived"]:
                errors.append(f"{case_id}: expected record is not current and confirmed")
            quote = gold.get("quote")
            if not isinstance(quote, str) or not 1 <= len(quote.strip()) <= 12000:
                errors.append(f"{case_id}: expected quote must contain 1-12000 characters")
            elif normalized(quote) not in normalized(record["text"]):
                errors.append(f"{case_id}: quote is not present in record {gold.get('entity_id')}")
    return {
        "valid": not errors,
        "dataset_id": dataset.get("id"),
        "split": dataset.get("split"),
        "dataset_sha256": hashlib.sha256(raw).hexdigest(),
        "case_count": len(cases),
        "record_count": len(all_records),
        "errors": errors,
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--dataset", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = validate_dataset(args.root, args.dataset)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(0 if report["valid"] else 2)
