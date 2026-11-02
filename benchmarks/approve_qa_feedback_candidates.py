"""Turn a small, explicitly reviewed feedback sample into QA gold.

User feedback is only a prioritization signal.  This command requires a
separate human review file and refuses to infer ``expected`` evidence from the
feedback rating.  Approved evidence must already be present in the candidate's
proposed evidence, then the resulting dataset is checked against the current
knowledge snapshot before it is atomically written.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile
from datetime import datetime, timezone
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmarks.validate_knowledge_qa_dataset import validate_dataset
from research_assistant.knowledge.store import compact_text


def _now():
    return datetime.now(timezone.utc).isoformat()


def _evidence_key(item):
    return (item.get("source_id"), item.get("page"), compact_text(item.get("quote", "")))


def build_dataset(candidates, review, split="development"):
    if not isinstance(candidates, dict) or candidates.get("status") != "unapproved":
        raise ValueError("候选文件必须标记为 unapproved，不能直接把正式数据集当候选。")
    if candidates.get("id") != "qa-feedback-candidates-v1":
        raise ValueError("不支持的反馈候选格式。")
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
    candidates_by_id = {item.get("id"): item for item in candidates.get("cases", [])}
    seen = set()
    output_cases = []
    rejected = []
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
        decision = entry.get("decision")
        note = entry.get("note", "")
        if decision not in {"approve", "reject"} or not isinstance(note, str) or not note.strip():
            raise ValueError(f"审核记录必须包含 decision 和非空 note：{candidate_id}")
        if decision == "reject":
            rejected.append(candidate_id)
            continue
        # Personal notes are deliberately kept out of paper-fact QA gold.
        if candidate.get("scope", "original") != "original":
            raise ValueError(f"个人记录候选不能升级为论文原文 gold：{candidate_id}")
        no_answer = entry.get("no_answer")
        if type(no_answer) is not bool:
            raise ValueError(f"approve 必须明确填写 no_answer：{candidate_id}")
        expected = entry.get("expected")
        if not isinstance(expected, list) or (bool(expected) == no_answer):
            raise ValueError(f"expected 与 no_answer 不一致：{candidate_id}")
        proposed = {_evidence_key(item) for item in candidate.get("proposed_evidence", [])}
        gold = []
        for item in expected:
            if not isinstance(item, dict):
                raise ValueError(f"expected evidence 必须是对象：{candidate_id}")
            source, page, quote = item.get("source_id"), item.get("page"), item.get("quote")
            if not isinstance(source, str) or not isinstance(page, int) or not isinstance(quote, str) or not quote.strip():
                raise ValueError(f"expected evidence 缺少 source_id/page/quote：{candidate_id}")
            if _evidence_key(item) not in proposed:
                raise ValueError(f"expected evidence 不在候选引用中，需先核对并从候选证据选择：{candidate_id}")
            gold.append({"source_id": source, "page": page, "quote": quote})
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
    return {
        "id": "qa-feedback-approved-" + hashlib.sha256(
            json.dumps(candidates, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()[:12],
        "split": split,
        "created_at": _now(),
        "review_status": "human_approved",
        "reviewer": reviewer,
        "source_candidates_sha256": hashlib.sha256(
            json.dumps(candidates, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest(),
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
        report = validate_dataset(root, temporary, expected_split=split)
        if not report["valid"]:
            raise ValueError("生成的数据集未通过当前知识库预检：" + "；".join(report["errors"][:5]))
        os.replace(temporary, output_path)
    finally:
        temporary.unlink(missing_ok=True)
    return {"output": str(output_path), "cases": len(dataset["cases"]),
            "split": split, "checked_evidence": report["checked_evidence"]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--review", type=Path, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--split", choices=["development", "test"], default="development")
    args = parser.parse_args()
    print(json.dumps(approve(args.candidates, args.review, args.output, args.root, args.split), ensure_ascii=False))
