"""Turn explicitly reviewed personal-memory candidates into a gold dataset.

The candidate file is only a retrieval proposal or a user-feedback sample.
This command requires a separate human review file and refuses to infer gold
records from a rating.  Every approved record must still be present at the
same entity/revision in the current knowledge database; stale notes therefore
fail closed instead of silently changing the benchmark.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmarks.validate_personal_memory_dataset import validate_dataset
from research_assistant.knowledge.store import compact_text


def _now():
    return datetime.now(timezone.utc).isoformat()


def _key(item):
    return (item.get("entity_id"), item.get("revision"), compact_text(item.get("quote", "")))


def _candidate_records(candidate):
    """Normalize both supported intermediate candidate formats."""
    # Feedback export calls this field proposed_evidence; the retrieval-only
    # memory review template calls it candidate_records.
    values = candidate.get("proposed_evidence")
    if values is None:
        values = candidate.get("candidate_records", [])
    return [item for item in values if isinstance(item, dict)]


def build_dataset(candidates, review, split="development"):
    if not isinstance(candidates, dict):
        raise ValueError("候选文件必须是 JSON 对象。")
    if candidates.get("id") not in {"qa-feedback-candidates-v1", "personal-memory-review-template-v1"}:
        raise ValueError("不支持的个人记忆候选格式。")
    if split not in {"development", "test"}:
        raise ValueError("split 只能是 development 或 test。")
    if not isinstance(review, dict) or not isinstance(review.get("reviewer"), str):
        raise ValueError("审核文件必须包含 reviewer。")
    reviewer = review["reviewer"].strip()
    if not reviewer or len(reviewer) > 80:
        raise ValueError("reviewer 必须为 1 到 80 个字符。")
    entries = review.get("cases")
    if not isinstance(entries, list) or not entries:
        raise ValueError("审核文件必须包含至少一条 cases。")
    candidates_by_id = {item.get("id"): item for item in candidates.get("cases", [])
                        if isinstance(item, dict)}
    seen, output_cases, rejected = set(), [], []
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("每条审核记录必须是对象。")
        candidate_id = entry.get("candidate_id")
        if candidate_id in seen:
            raise ValueError(f"重复审核记录：{candidate_id}")
        seen.add(candidate_id)
        candidate = candidates_by_id.get(candidate_id)
        if not candidate:
            raise ValueError(f"找不到候选：{candidate_id}")
        # A combined answer may contain both scopes.  It is never a safe
        # source for this dataset; the reviewer must use a notes-only run.
        candidate_scope = candidate.get("scope", "notes") if candidates.get("id") == "personal-memory-review-template-v1" else candidate.get("scope")
        if candidate_scope != "notes":
            raise ValueError(f"个人记忆候选必须是 scope=notes：{candidate_id}")
        decision = entry.get("decision")
        note = entry.get("note", "")
        if decision not in {"approve", "reject"} or not isinstance(note, str) or not note.strip():
            raise ValueError(f"审核记录必须包含 decision 和非空 note：{candidate_id}")
        if decision == "reject":
            rejected.append(candidate_id)
            continue
        no_answer = entry.get("no_answer")
        if type(no_answer) is not bool:
            raise ValueError(f"approve 必须明确填写 no_answer：{candidate_id}")
        expected = entry.get("expected")
        if not isinstance(expected, list) or (bool(expected) == no_answer):
            raise ValueError(f"expected 与 no_answer 不一致：{candidate_id}")
        proposed = {_key(item) for item in _candidate_records(candidate)}
        gold = []
        for item in expected:
            if not isinstance(item, dict):
                raise ValueError(f"expected record 必须是对象：{candidate_id}")
            entity_id, revision, quote = item.get("entity_id"), item.get("revision"), item.get("quote")
            if (not isinstance(entity_id, str) or not entity_id.strip()
                    or type(revision) is not int or revision < 1
                    or not isinstance(quote, str) or not quote.strip()):
                raise ValueError(f"expected record 缺少 entity_id/revision/quote：{candidate_id}")
            if _key(item) not in proposed:
                raise ValueError(f"expected record 不在候选记录中，需先核对并从候选选择：{candidate_id}")
            gold.append({key: item[key] for key in ("entity_id", "revision", "kind", "quote")
                         if item.get(key) is not None})
        output_cases.append({
            "id": candidate_id,
            "query": candidate.get("query", ""),
            "kb_ids": candidate.get("kb_ids", []),
            "expected": gold,
            "no_answer": no_answer,
            "reviewer": reviewer,
            "review_note": note.strip(),
        })
    if not output_cases:
        raise ValueError("没有通过人工审核的 case，不能生成数据集。")
    source_hash = hashlib.sha256(
        json.dumps(candidates, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()
    return {
        "id": "personal-memory-approved-" + source_hash[:12],
        "split": split,
        "created_at": _now(),
        "review_status": "human_approved",
        "reviewer": reviewer,
        "source_candidates_sha256": source_hash,
        "approved_count": len(output_cases),
        "rejected_count": len(rejected),
        "rejected_candidate_ids": rejected,
        "cases": output_cases,
    }


def approve(candidates_path: Path, review_path: Path, output_path: Path, root: Path, split="development"):
    candidates = json.loads(candidates_path.read_text(encoding="utf-8"))
    review = json.loads(review_path.read_text(encoding="utf-8"))
    dataset = build_dataset(candidates, review, split)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json",
                                     dir=output_path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(dataset, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    try:
        report = validate_dataset(root, temporary)
        if not report["valid"]:
            raise ValueError("生成的数据集未通过当前知识库预检：" + "；".join(report["errors"][:5]))
        os.replace(temporary, output_path)
    finally:
        temporary.unlink(missing_ok=True)
    return {"output": str(output_path), "cases": len(dataset["cases"]),
            "split": split, "checked_records": report["record_count"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="将人工确认的个人记忆候选封存为评测集")
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--review", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--split", choices=["development", "test"], default="development")
    args = parser.parse_args()
    print(json.dumps(approve(args.candidates, args.review, args.output, args.root, args.split),
                     ensure_ascii=False))
