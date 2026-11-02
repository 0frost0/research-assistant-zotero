import json
import os
from pathlib import Path
import tempfile
import unittest

from benchmarks.build_qa_smoke_corpus import build
from benchmarks.create_qa_review_template import (
    build_template, load_questions, load_questions_from_jobs, write_atomic,
)
from research_assistant.knowledge.jobs import QAJobStore


class QAReviewTemplateTests(unittest.TestCase):
    def test_template_contains_retrieval_proposals_but_no_gold_or_model_calls(self):
        with tempfile.TemporaryDirectory() as root:
            project = Path(root) / "project"
            build(project)
            dataset = json.loads((project / "dataset.json").read_text(encoding="utf-8"))
            questions = [{"id": "Q1", "query": dataset["cases"][0]["query"],
                          "kb_ids": dataset["cases"][0]["kb_ids"]}]
            result = build_template(project, questions, backend="tfidf", top_k=3)
            self.assertEqual(result["status"], "needs_human_review")
            self.assertEqual(result["provenance"], "retrieval_proposal")
            case = result["cases"][0]
            self.assertEqual(case["expected"], [])
            self.assertIsNone(case["no_answer"])
            self.assertIsNone(case["human_decision"])
            self.assertTrue(case["candidate_evidence"])

    def test_questions_require_kb_scope_and_ids_are_unique(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "questions.json"
            path.write_text(json.dumps([{"id": "Q1", "query": "问题", "kb_ids": ["kb"]},
                                        {"id": "Q1", "query": "重复", "kb_ids": ["kb"]}],
                                       ensure_ascii=False), encoding="utf-8")
            with self.assertRaises(ValueError):
                load_questions(path)

    def test_write_is_atomic_and_does_not_overwrite(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "template.json"
            write_atomic(path, {"status": "needs_human_review"})
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                write_atomic(path, {"status": "other"})

    def test_completed_qa_history_becomes_deduplicated_review_questions(self):
        with tempfile.TemporaryDirectory() as root:
            db = Path(root) / "qa.sqlite3"
            jobs = QAJobStore(db)
            first = jobs.create("ask", {"query": "方法的适用条件是什么？", "kb_ids": ["kb"], "scope": "original"})
            jobs.mark_running(first["job_id"])
            jobs.finish(first["job_id"], "succeeded", result={"status": "no_evidence", "scope": "original"})
            jobs.record_feedback(first["job_id"], {"rating": "incorrect"})
            duplicate = jobs.create("ask", {"query": "方法的适用条件是什么？", "kb_ids": ["kb"], "scope": "original"})
            jobs.mark_running(duplicate["job_id"])
            jobs.finish(duplicate["job_id"], "succeeded", result={"status": "answered"})
            notes = jobs.create("ask", {"query": "我的理解是什么？", "kb_ids": ["kb"], "scope": "notes"})
            jobs.mark_running(notes["job_id"])
            jobs.finish(notes["job_id"], "succeeded", result={"status": "answered"})
            questions = load_questions_from_jobs(db, limit=10, scope="original")
            self.assertEqual(len(questions), 1)
            self.assertEqual(questions[0]["query"], "方法的适用条件是什么？")
            self.assertTrue(questions[0]["id"].startswith("QJOB-"))
            self.assertEqual(questions[0]["feedback_rating"], "incorrect")
            self.assertEqual(questions[0]["review_priority"], 0)
            note_questions = load_questions_from_jobs(db, limit=10, scope="notes")
            self.assertEqual([item["query"] for item in note_questions], ["我的理解是什么？"])


if __name__ == "__main__":
    unittest.main()
