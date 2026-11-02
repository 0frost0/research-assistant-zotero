import json
import tempfile
import unittest
from pathlib import Path

from benchmarks.export_qa_feedback_candidates import export_candidates
from research_assistant.knowledge.jobs import QAJobStore


class QAFeedbackExportTests(unittest.TestCase):
    def _completed(self, store, query, scope, rating):
        job = store.create("ask", {"query": query, "scope": scope, "kb_ids": ["kb-1"]})
        store.finish(job["job_id"], "succeeded", result={
            "status": "answered", "scope": scope, "revisions": {"kb-1": 3},
                "claims": [{"text": "结论", "scope": scope, "citations": [{
                    "evidence_id": "E1", "support_quote": "原文证据"
                }]}],
                "evidence": [{"evidence_id": "E1", "source_id": "source.pdf",
                               "page": 2, "quote": "原文证据", "revision": 3}],
                "gaps": []
        })
        store.record_feedback(job["job_id"], {"rating": rating, "note": "待核对"})

    def test_export_is_unapproved_and_keeps_audit_fields(self):
        with tempfile.TemporaryDirectory() as root:
            db = Path(root) / "qa.sqlite3"
            output = Path(root) / "candidates.json"
            store = QAJobStore(db)
            self._completed(store, "原文问题", "original", "partial")
            self._completed(store, "记录问题", "notes", "correct_no_answer")

            info = export_candidates(db, output, limit=10)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(info["case_count"], 2)
            self.assertEqual(payload["status"], "unapproved")
            self.assertEqual(payload["split"], "development")
            self.assertNotIn("expected", payload["cases"][0])
            self.assertEqual(payload["cases"][0]["provenance"], "user_feedback_candidate")
            self.assertEqual(payload["cases"][0]["revisions"], {"kb-1": 3})
            self.assertEqual(payload["cases"][0]["proposed_evidence"][0]["page"], 2)
            self.assertEqual(payload["cases"][0]["proposed_evidence"][0]["source_id"], "source.pdf")
            self.assertTrue(any(item["proposed_no_answer"] for item in payload["cases"]))

    def test_scope_filter_does_not_mix_original_and_notes(self):
        with tempfile.TemporaryDirectory() as root:
            db = Path(root) / "qa.sqlite3"
            output = Path(root) / "notes.json"
            store = QAJobStore(db)
            self._completed(store, "原文问题", "original", "helpful")
            self._completed(store, "记录问题", "notes", "helpful")

            export_candidates(db, output, limit=10, scope="notes")
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(len(payload["cases"]), 1)
            self.assertEqual(payload["cases"][0]["scope"], "notes")

    def test_prioritizes_negative_feedback_and_supports_rating_filter(self):
        with tempfile.TemporaryDirectory() as root:
            db = Path(root) / "qa.sqlite3"
            output = Path(root) / "candidates.json"
            store = QAJobStore(db)
            for rating in ("helpful", "partial", "incorrect", "correct_no_answer"):
                job = store.create("ask", {"query": rating, "scope": "original", "kb_ids": ["kb-1"]})
                store.finish(job["job_id"], "succeeded", result={"status": "no_evidence"})
                store.record_feedback(job["job_id"], {"rating": rating})
            export_candidates(db, output, limit=3)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual([case["rating"] for case in payload["cases"]],
                             ["incorrect", "partial", "correct_no_answer"])
            self.assertEqual(payload["cases"][0]["review_priority"], "high")
            export_candidates(db, output, limit=10, ratings=["helpful"])
            filtered = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual([case["rating"] for case in filtered["cases"]], ["helpful"])


if __name__ == "__main__":
    unittest.main()
