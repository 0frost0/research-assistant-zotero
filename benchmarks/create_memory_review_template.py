"""Create a retrieval-only review template for personal Zotero records.

The output is an intermediate artifact, not a personal-memory gold dataset.
It keeps the current record revision and source link so a researcher can
confirm what was actually written before freezing a development/test split.
No generation model is called.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import os
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from dotenv import load_dotenv

from benchmarks.create_qa_review_template import (
    load_questions,
    load_questions_from_jobs,
    load_questions_payload,
)
from research_assistant.knowledge.jobs import QAJobStore
from research_assistant.knowledge.qa import select_note_candidates
from research_assistant.knowledge.service import KnowledgeService
from research_assistant.knowledge.store import KnowledgeStore


def build_template(root: Path, questions, backend="multimodal", top_k=10):
    if type(top_k) is not int or not 1 <= top_k <= 20:
        raise ValueError("top_k 必须在 1 到 20 之间。")
    store = KnowledgeStore(root)
    service = KnowledgeService(store, backend=backend)
    cases = []
    for case in questions:
        snapshot = store.snapshot(case["kb_ids"])
        found = service.search({"query": case["query"], "kb_ids": case["kb_ids"],
                                "scope": "notes", "k": top_k})
        candidates = select_note_candidates(found.get("results", []), limit=top_k,
                                            char_budget=20_000)
        records = [{key: item.get(key) for key in (
            "entity_id", "revision", "kind", "title", "quote", "source_id", "url", "page"
        ) if item.get(key) is not None} for item in candidates]
        cases.append({
            "id": case["id"], "query": case["query"], "kb_ids": case["kb_ids"],
            "expected": [], "no_answer": None,
            "candidate_records": records,
            "human_decision": None,
            "knowledge_revisions": {base["id"]: base["revision"] for base in snapshot["bases"]},
            "retrieval_warning": found.get("warning"),
        })
    return {
        "id": "personal-memory-review-template-v1",
        "split": "development",
        # An empty history is an expected initial state, not a failed run.
        # Keep it explicit so automation cannot mistake it for a reviewed set.
        "status": "needs_human_review" if cases else "empty_history",
        "provenance": "retrieval_proposal",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "backend": backend,
        "cases": cases,
        "instructions": [
            "只核对当前有效、已确认的个人记录；论文原文不能作为个人记录 gold。",
            "将确认的 entity_id、revision、kind 和逐字摘录复制到 expected。",
            "没有相关记录时把 no_answer 改为 true。",
            "完成后删除 candidate_records 并运行 validate_personal_memory_dataset.py。",
            "empty_history 只表示当前没有可供核对的个人记录，不是可用于评测的空答案集。",
        ],
    }


def load_questions_from_jobs_allow_empty(path: Path, limit=50, scope="notes"):
    """Load completed personal-memory questions, allowing an empty history."""
    raw = QAJobStore(path, readonly=True).review_questions(limit, scope)
    return load_questions_payload(raw) if raw else []


def write_atomic(path: Path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise FileExistsError(f"输出文件已存在，为避免覆盖请先移走：{path}")
    fd, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent, text=True)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        os.replace(name, path)
        # Personal notes and source links are private research data.
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    except Exception:
        try:
            os.unlink(name)
        except OSError:
            pass
        raise


def main(argv=None):
    parser = argparse.ArgumentParser(description="为个人文献记忆生成待人工核对的评测模板")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--questions", type=Path)
    source.add_argument("--qa-jobs", type=Path,
                        help="从仅笔记问答历史提取问题；不会读取论文原文回答")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--backend", choices=["tfidf", "multimodal"], default="multimodal")
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--job-limit", type=int, default=50)
    parser.add_argument("--allow-empty", action="store_true",
                        help="--qa-jobs 没有可用问题时生成 cases=[] 的 empty_history 模板")
    parser.add_argument("--model-config", type=Path)
    args = parser.parse_args(argv)
    load_dotenv(args.model_config or args.root / ".env")
    if args.job_limit < 1 or args.job_limit > 1000:
        parser.error("--job-limit 必须在 1 到 1000 之间")
    if args.questions and args.allow_empty:
        parser.error("--allow-empty 只能与 --qa-jobs 一起使用")
    if args.questions:
        questions = load_questions(args.questions)
    elif args.allow_empty:
        questions = load_questions_from_jobs_allow_empty(args.qa_jobs, args.job_limit, "notes")
    else:
        questions = load_questions_from_jobs(args.qa_jobs, args.job_limit, "notes")
    payload = build_template(args.root, questions, args.backend, args.top_k)
    write_atomic(args.output, payload)
    print(json.dumps({"output": str(args.output), "cases": len(payload["cases"]),
                      "status": payload["status"], "model_calls": 0}, ensure_ascii=False))


if __name__ == "__main__":
    main()
