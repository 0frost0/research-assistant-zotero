"""Export user-scored QA answers as *unapproved* evaluation candidates.

Feedback is a sampling signal, never a gold label.  The output deliberately
uses ``proposed_*`` fields so the existing evaluation validators cannot accept
it without a separate human review and source/revision check.
"""
import argparse
import json
from pathlib import Path
import hashlib
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from research_assistant.knowledge.jobs import QAJobStore


RATING_PRIORITY = {"incorrect": 0, "partial": 1, "correct_no_answer": 2, "helpful": 3}


def export_candidates(db_path: Path, output: Path, limit=100, scope=None, ratings=None):
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("limit must be between 1 and 1000")
    # Prioritize and limit in SQLite; only the selected snapshots are loaded.
    feedback = QAJobStore(db_path, readonly=True).list_feedback_candidates(limit, scope, ratings)
    cases = []
    for item in feedback:
        snapshot = item["snapshot"]
        evidence_by_id = {
            evidence.get("evidence_id"): evidence
            for evidence in snapshot.get("evidence", [])
            if evidence.get("evidence_id")
        }
        citations = []
        for claim in snapshot.get("claims", []):
            for citation in claim.get("citations", []):
                citation = {**evidence_by_id.get(citation.get("evidence_id"), {}), **citation}
                if not citation.get("quote") and citation.get("support_quote"):
                    citation["quote"] = citation["support_quote"]
                citation["claim_text"] = claim.get("text", "")
                citation["claim_scope"] = claim.get("scope")
                citations.append(citation)
        cases.append({
            "id": "feedback-" + item["feedback_id"],
            "job_id": item["job_id"],
            "query": item["question"],
            "scope": item["scope"],
            "rating": item["rating"],
            "review_priority": ("high" if item["rating"] in {"incorrect", "partial"}
                                else "medium" if item["rating"] == "correct_no_answer" else "low"),
            "note": item["note"],
            "kb_ids": item["kb_ids"],
            "result_hash": item["result_hash"],
            "revisions": snapshot.get("revisions", {}),
            "proposed_no_answer": item["rating"] == "correct_no_answer" or
                                  snapshot.get("status") == "no_evidence",
            "proposed_evidence": citations,
            "provenance": "user_feedback_candidate",
        })
    raw = json.dumps({"id": "qa-feedback-candidates-v1", "split": "development",
                      "status": "unapproved", "cases": cases},
                     ensure_ascii=False, indent=2).encode("utf-8")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(raw)
    return {"output": str(output), "case_count": len(cases),
            "sha256": hashlib.sha256(raw).hexdigest()}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True,
                        help="Path to .data/zotero/qa_jobs.sqlite3")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--scope", choices=["original", "notes", "both"])
    parser.add_argument("--rating", action="append", choices=sorted(RATING_PRIORITY),
                        help="只导出指定评分；可重复使用，默认按 incorrect/partial 优先混合导出")
    args = parser.parse_args()
    if not 1 <= args.limit <= 1000:
        parser.error("limit must be between 1 and 1000")
    print(json.dumps(export_candidates(args.db, args.output, args.limit, args.scope, args.rating),
                     ensure_ascii=False, indent=2))
