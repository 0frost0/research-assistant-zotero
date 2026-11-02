"""Evaluation readers must not behave like a restarting web worker."""
from pathlib import Path
from contextlib import closing
import sqlite3
import tempfile
import unittest

from benchmarks.create_qa_review_template import load_questions_from_jobs
from benchmarks.create_memory_review_template import load_questions_from_jobs_allow_empty
from benchmarks.export_qa_feedback_candidates import export_candidates
from research_assistant.knowledge.jobs import QAJobStore


class JobReadOnlyTests(unittest.TestCase):
    def test_all_evaluation_readers_leave_live_tasks_and_database_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "qa.sqlite3"
            store = QAJobStore(path)
            for scope in ("original", "notes"):
                done = store.create("ask", {"query": "我的理解", "kb_ids": ["kb"], "scope": scope})
                store.finish(done["job_id"], "succeeded", result={"status": "no_evidence"})
                store.record_feedback(done["job_id"], {"rating": "correct_no_answer"})
            queued = store.create("ask", {"query": "排队问题"})
            running = store.create("ask", {"query": "运行问题"})
            store.mark_running(running["job_id"])
            before = path.read_bytes()
            readers = [
                lambda: load_questions_from_jobs(path),
                lambda: load_questions_from_jobs(path, scope="notes"),
                lambda: load_questions_from_jobs_allow_empty(path),
                lambda: export_candidates(path, root / "candidates.json"),
            ]
            for reader in readers:
                with self.subTest(reader=reader):
                    reader()
                    self.assertEqual(store.public(queued["job_id"])["state"], "queued")
                    self.assertEqual(store.public(running["job_id"])["state"], "running")
                    self.assertEqual(path.read_bytes(), before)
            store.finish(running["job_id"], "succeeded", result={"status": "answered"})
            self.assertEqual(store.public(running["job_id"])["state"], "succeeded")

    def test_readonly_store_rejects_writes_and_does_not_create_missing_database(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "db with spaces #1.sqlite3"
            store = QAJobStore(path)
            pending = store.create("ask", {"query": "记录"})
            readonly = QAJobStore(path, readonly=True)
            self.assertEqual(readonly.public(pending["job_id"])["state"], "queued")
            with self.assertRaises(sqlite3.OperationalError):
                readonly.create("ask", {"query": "不应写入"})
            missing = Path(directory) / "missing" / "qa.sqlite3"
            with self.assertRaises(FileNotFoundError):
                load_questions_from_jobs_allow_empty(missing)
            self.assertFalse(missing.parent.exists())

    def test_old_schema_is_not_silently_migrated_by_reader(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "old.sqlite3"
            with closing(sqlite3.connect(path)) as con, con:
                con.execute("CREATE TABLE unrelated(id INTEGER)")
            before = path.read_bytes()
            with self.assertRaisesRegex(ValueError, "任务表"):
                QAJobStore(path, readonly=True)
            self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
