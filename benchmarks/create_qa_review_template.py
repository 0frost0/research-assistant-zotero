"""Create a local, retrieval-only QA review template for a Zotero library.

This command never calls a generation model and never turns retrieval results
into gold labels.  It collects a small set of user-written questions and the
current top evidence so a researcher can verify the page/quote in Zotero,
then feed an explicitly reviewed file to ``approve_qa_feedback_candidates``
or the normal QA dataset validator.

Input JSON can be either a list of objects or ``{"cases": [...]}``:
[{"id": "Q1", "query": "...", "kb_ids": ["..."]}]
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

from research_assistant.knowledge.jobs import QAJobStore
from research_assistant.knowledge.qa import select_candidates
from research_assistant.knowledge.service import KnowledgeService
from research_assistant.knowledge.store import KnowledgeStore, text


def load_questions(path: Path):
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取问题文件：{exc}") from exc
    cases = raw.get("cases") if isinstance(raw, dict) else raw
    if not isinstance(cases, list) or not cases:
        raise ValueError("问题文件必须是非空数组或包含 cases 数组的对象。")
    return load_questions_payload(cases)


def load_questions_payload(cases):
    """Validate questions from a file or from completed QA history."""
    normalized, seen = [], set()
    for index, case in enumerate(cases, 1):
        if not isinstance(case, dict):
            raise ValueError(f"第 {index} 条问题必须是对象。")
        ident = str(case.get("id") or f"Q{index:03d}").strip()
        if not ident or ident in seen:
            raise ValueError(f"问题 ID 无效或重复：{ident!r}")
        seen.add(ident)
        query = text(case.get("query"), f"问题 {ident}", 2000)
        kb_ids = case.get("kb_ids")
        if (not isinstance(kb_ids, list) or not kb_ids
                or any(not isinstance(value, str) or not value.strip() for value in kb_ids)):
            raise ValueError(f"问题 {ident} 必须指定至少一个 kb_id。")
        item = {"id": ident, "query": query, "kb_ids": list(dict.fromkeys(kb_ids))}
        # These fields are produced only by the QA-history loader. They help
        # reviewers understand queue priority without affecting gold labels.
        for key in ("source_job_id", "feedback_rating", "review_priority"):
            if key in case:
                item[key] = case[key]
        normalized.append(item)
    if not normalized:
        raise ValueError("没有可用的问题。")
    return normalized


def load_questions_from_jobs(path: Path, limit=50, scope="original"):
    """Read only deduplicated questions from completed QA jobs."""
    return load_questions_payload(QAJobStore(path, readonly=True).review_questions(limit, scope))


def build_template(root: Path, questions, backend="multimodal", top_k=10):
    if type(top_k) is not int or not 1 <= top_k <= 20:
        raise ValueError("top_k 必须在 1 到 20 之间。")
    store = KnowledgeStore(root)
    service = KnowledgeService(store, backend=backend)
    cases = []
    for case in questions:
        snapshot = store.snapshot(case["kb_ids"])
        found = service.search({"query": case["query"], "kb_ids": case["kb_ids"],
                                "scope": "original", "k": top_k})
        candidates = select_candidates(found.get("results", []), strategy="selected",
                                       limit=top_k, char_budget=20_000)
        evidence = [{key: item.get(key) for key in (
            "source_id", "page", "quote", "title", "url", "kind", "revision"
        ) if item.get(key) is not None} for item in candidates]
        cases.append({
            "id": case["id"], "query": case["query"], "kb_ids": case["kb_ids"],
            **{key: case[key] for key in ("source_job_id", "feedback_rating", "review_priority") if key in case},
            "expected": [], "no_answer": None,
            "candidate_evidence": evidence,
            "human_decision": None,
            "knowledge_revisions": {base["id"]: base["revision"] for base in snapshot["bases"]},
            "retrieval_warning": found.get("warning"),
        })
    return {
        "id": "qa-review-template-v1",
        "split": "development",
        "status": "needs_human_review",
        "provenance": "retrieval_proposal",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "backend": backend,
        "cases": cases,
        "instructions": [
            "逐条在 Zotero 中核对 candidate_evidence 的页码和摘录。",
            "将确认的证据复制到 expected；没有答案时把 no_answer 改为 true。",
            "不要把 candidate_evidence 或模型回答直接当作 gold。",
            "完成后删除 candidate_evidence 并运行 validate_knowledge_qa_dataset.py。",
        ],
    }


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
        # The template contains paper excerpts and may later contain private
        # review notes. Keep it readable only by the current account.
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
    parser = argparse.ArgumentParser(description="为当前 Zotero 知识库生成待人工核对的 QA 评测模板")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--questions", type=Path)
    source.add_argument("--qa-jobs", type=Path,
                        help="从已完成问答历史提取问题；只读问题和知识库 ID")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--backend", choices=["tfidf", "multimodal"], default="multimodal")
    parser.add_argument("--top-k", type=int, default=10)
    parser.add_argument("--job-limit", type=int, default=50)
    parser.add_argument("--scope", choices=["original"], default="original",
                        help="QA 模板只接受论文原文问题；个人记录请使用 create_memory_review_template.py")
    parser.add_argument("--model-config", type=Path)
    args = parser.parse_args(argv)
    load_dotenv(args.model_config or args.root / ".env")
    if args.job_limit < 1 or args.job_limit > 1000:
        parser.error("--job-limit 必须在 1 到 1000 之间")
    questions = (load_questions(args.questions) if args.questions else
                 load_questions_from_jobs(args.qa_jobs, args.job_limit, args.scope))
    payload = build_template(args.root, questions, args.backend, args.top_k)
    write_atomic(args.output, payload)
    print(json.dumps({"output": str(args.output), "cases": len(payload["cases"]),
                      "status": payload["status"], "model_calls": 0}, ensure_ascii=False))


if __name__ == "__main__":
    main()
