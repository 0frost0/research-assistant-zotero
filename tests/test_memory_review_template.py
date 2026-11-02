import json
import os
from pathlib import Path
import tempfile
import unittest

from benchmarks.create_memory_review_template import (
    build_template,
    load_questions,
    load_questions_from_jobs,
    load_questions_from_jobs_allow_empty,
    write_atomic,
)
from research_assistant.knowledge.jobs import QAJobStore
from research_assistant.knowledge.store import KnowledgeStore


class MemoryReviewTemplateTests(unittest.TestCase):
    def test_template_keeps_current_record_revision_and_does_not_make_gold(self):
        with tempfile.TemporaryDirectory() as root:
            project = Path(root) / "project"
            store = KnowledgeStore(project)
            base = store.create({"connection": "fixture", "library": "user:0",
                                 "collection": "notes", "name": "Notes"})
            store.sync(base["id"], {"name": "Notes", "include_children": True,
                                     "complete": True, "expected_revision": 0,
                                     "entities": [{"key": "NOTE0001", "kind": "note",
                                                   "title": "我的理解", "body": "我记录了方法的适用条件。",
                                                   "confirmed": True}]})
            result = build_template(project, [{"id": "M1", "query": "适用条件",
                                                "kb_ids": [base["id"]]}], backend="tfidf", top_k=5)
            case = result["cases"][0]
            self.assertEqual(case["expected"], [])
            self.assertIsNone(case["no_answer"])
            self.assertTrue(case["candidate_records"])
            record = case["candidate_records"][0]
            self.assertEqual(record["kind"], "external_note")
            self.assertEqual(record["revision"], 1)
            self.assertIn("适用条件", record["quote"])

    def test_shared_question_loader_and_atomic_output_are_safe(self):
        with tempfile.TemporaryDirectory() as root:
            questions = Path(root) / "questions.json"
            questions.write_text(json.dumps({"cases": [{"id": "M1", "query": "记录", "kb_ids": ["kb"]}]},
                                            ensure_ascii=False), encoding="utf-8")
            self.assertEqual(load_questions(questions)[0]["id"], "M1")
            output = Path(root) / "memory-template.json"
            write_atomic(output, {"status": "needs_human_review"})
            if os.name != "nt":
                self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            with self.assertRaises(FileExistsError):
                write_atomic(output, {"status": "other"})

    def test_history_loader_accepts_only_personal_memory_scope(self):
        with tempfile.TemporaryDirectory() as root:
            jobs = QAJobStore(Path(root) / "qa.sqlite3")
            original = jobs.create("ask", {"query": "论文说了什么？", "kb_ids": ["kb"], "scope": "original"})
            jobs.mark_running(original["job_id"])
            jobs.finish(original["job_id"], "succeeded", result={"status": "answered"})
            memory = jobs.create("ask", {"query": "我之前如何理解？", "kb_ids": ["kb"], "scope": "notes"})
            jobs.mark_running(memory["job_id"])
            jobs.finish(memory["job_id"], "succeeded", result={"status": "answered"})
            questions = load_questions_from_jobs(Path(root) / "qa.sqlite3", 10, "notes")
            self.assertEqual([item["query"] for item in questions], ["我之前如何理解？"])

    def test_empty_history_is_explicit_and_does_not_become_gold(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "qa.sqlite3"
            QAJobStore(path)
            questions = load_questions_from_jobs_allow_empty(path, 10, "notes")
            self.assertEqual(questions, [])
            template = build_template(Path(root) / "project", questions, backend="tfidf", top_k=5)
            self.assertEqual(template["status"], "empty_history")
            self.assertEqual(template["cases"], [])


if __name__ == "__main__":
    unittest.main()
