"""Durable async QA job state tests; no model or network calls."""

import json
import tempfile
import unittest
from pathlib import Path

from research_assistant.knowledge.jobs import QAJobStore


class QAJobStoreTests(unittest.TestCase):
    def test_lifecycle_and_result_are_persisted(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "qa_jobs.sqlite3"
            store = QAJobStore(path)
            job = store.create("ask", {"query": "问题", "kb_ids": ["kb-1"]})
            self.assertEqual(job["state"], "queued")
            self.assertEqual(store.mark_running(job["job_id"])["state"], "running")
            done = store.finish(job["job_id"], "succeeded", result={"status": "answered"})
            self.assertEqual(done["result"], {"status": "answered"})
            self.assertEqual(done["question"], "问题")
            self.assertEqual(done["scope"], "original")
            self.assertEqual(store.list()[0]["state"], "succeeded")

    def test_restart_marks_unfinished_jobs_interrupted_without_result(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / "qa_jobs.sqlite3"
            first = QAJobStore(path)
            queued = first.create("ask", {"query": "排队", "kb_ids": ["kb-1"]})
            running = first.create("ask", {"query": "执行", "kb_ids": ["kb-1"]})
            first.mark_running(running["job_id"])
            second = QAJobStore(path)
            for ident in (queued["job_id"], running["job_id"]):
                value = second.public(ident)
                self.assertEqual(value["state"], "interrupted")
                self.assertIsNone(value.get("result"))
                self.assertIn("未自动重试", value["error"])

    def test_invalid_or_oversized_job_is_rejected(self):
        with tempfile.TemporaryDirectory() as root:
            store = QAJobStore(Path(root) / "qa_jobs.sqlite3")
            with self.assertRaises(ValueError): store.create("compare", {})
            with self.assertRaises(ValueError): store.create("ask", {"query": "x" * 100_001})

    def test_manual_retry_is_bounded_and_preserves_question(self):
        with tempfile.TemporaryDirectory() as root:
            store = QAJobStore(Path(root) / "qa_jobs.sqlite3")
            original = store.create("ask", {"query": "原问题", "kb_ids": ["kb-1"], "scope": "original"})
            store.finish(original["job_id"], "failed", error="模型暂不可用")
            retry = store.retry(original["job_id"])
            self.assertEqual(retry["state"], "queued")
            self.assertEqual(retry["question"], "原问题")
            self.assertEqual(retry["retry_of"], original["job_id"])
            self.assertEqual(retry["retry_count"], 1)
            with self.assertRaises(ValueError): store.retry(retry["job_id"])
            previous = retry
            for _ in range(2):
                store.finish(previous["job_id"], "failed", error="再次失败")
                previous = store.retry(previous["job_id"])
            store.finish(previous["job_id"], "failed", error="达到上限")
            with self.assertRaises(ValueError): store.retry(previous["job_id"])

    def test_cancel_is_terminal_and_cannot_cancel_completed_job(self):
        with tempfile.TemporaryDirectory() as root:
            store = QAJobStore(Path(root) / "qa_jobs.sqlite3")
            queued = store.create("ask", {"query": "停止排队任务", "kb_ids": ["kb-1"]})
            cancelled = store.cancel(queued["job_id"])
            self.assertEqual(cancelled["state"], "cancelled")
            self.assertIn("主动停止", cancelled["error"])
            self.assertEqual(store.cancel(queued["job_id"])["state"], "cancelled")
            with self.assertRaises(ValueError): store.retry(queued["job_id"])
            done = store.create("ask", {"query": "已完成", "kb_ids": ["kb-1"]})
            store.finish(done["job_id"], "succeeded", result={"status": "answered"})
            with self.assertRaises(ValueError): store.cancel(done["job_id"])

    def test_conversation_history_is_bounded_and_scoped_to_same_kb_set(self):
        with tempfile.TemporaryDirectory() as root:
            store = QAJobStore(Path(root) / "qa_jobs.sqlite3")
            first = store.create("ask", {"query": "第一问", "kb_ids": ["kb-1"],
                                          "conversation_id": "session-1"})
            store.finish(first["job_id"], "succeeded", result={
                "claims": [{"text": "第一条已核验结论", "citations": []}],
            })
            other = store.create("ask", {"query": "不应混入", "kb_ids": ["kb-2"],
                                          "conversation_id": "session-1"})
            store.finish(other["job_id"], "succeeded", result={"claims": [{"text": "错误范围"}]})
            for index in range(5):
                job = store.create("ask", {"query": f"后续问题 {index}", "kb_ids": ["kb-1"],
                                            "conversation_id": "session-1"})
                store.finish(job["job_id"], "succeeded", result={"claims": [{"text": f"结论 {index}"}]})
            history = store.conversation_history("session-1", ["kb-1"], limit=3)
            self.assertEqual([item["question"] for item in history], ["后续问题 2", "后续问题 3", "后续问题 4"])
            self.assertNotIn("错误范围", str(history))

    def test_feedback_keeps_answer_snapshot_and_is_one_time(self):
        with tempfile.TemporaryDirectory() as root:
            store = QAJobStore(Path(root) / "qa_jobs.sqlite3")
            job = store.create("ask", {"query": "我之前如何理解？", "kb_ids": ["kb-1"], "scope": "notes"})
            result = {
                "status": "answered", "scope": "notes", "revisions": {"kb-1": 3},
                "evidence": [{"evidence_id": "N1", "kind": "user_note", "entity_id": "n1",
                              "revision": 3, "quote": "我的理解是需要长期验证。",
                              "url": "zotero://select/library/items/N1"}],
                "claims": [{"text": "需要长期验证。", "scope": "notes", "citations": [
                    {"evidence_id": "N1", "support_quote": "需要长期验证。", "revision": 3}]}],
                "gaps": [],
            }
            store.finish(job["job_id"], "succeeded", result=result)
            feedback = store.record_feedback(job["job_id"], {"rating": "helpful"})
            self.assertEqual(feedback["job_id"], job["job_id"])
            self.assertEqual(store.public(job["job_id"])["feedback"]["rating"], "helpful")
            exported = store.list_feedback()
            self.assertEqual(exported[0]["snapshot"]["revisions"], {"kb-1": 3})
            self.assertEqual(exported[0]["snapshot"]["evidence"][0]["revision"], 3)
            with self.assertRaises(ValueError):
                store.record_feedback(job["job_id"], {"rating": "incorrect"})

    def test_feedback_rejects_unfinished_or_invalid_jobs(self):
        with tempfile.TemporaryDirectory() as root:
            store = QAJobStore(Path(root) / "qa_jobs.sqlite3")
            queued = store.create("ask", {"query": "问题", "kb_ids": ["kb-1"]})
            with self.assertRaises(ValueError):
                store.record_feedback(queued["job_id"], {"rating": "helpful"})
            with self.assertRaises(ValueError):
                store.record_feedback(queued["job_id"], {"rating": "unknown"})

    def test_pruning_removes_feedback_orphans(self):
        with tempfile.TemporaryDirectory() as root:
            store = QAJobStore(Path(root) / "qa_jobs.sqlite3")
            first = store.create("ask", {"query": "最早的问题", "kb_ids": ["kb-1"]})
            store.finish(first["job_id"], "succeeded", result={"status": "no_evidence"})
            store.record_feedback(first["job_id"], {"rating": "correct_no_answer"})
            for index in range(200):
                store.create("ask", {"query": f"后续问题 {index}", "kb_ids": ["kb-1"]})
            with self.assertRaises(KeyError):
                store.public(first["job_id"])
            self.assertEqual(store.list_feedback(), [])

    def test_feedback_candidate_query_prioritizes_before_loading_snapshots(self):
        with tempfile.TemporaryDirectory() as root:
            store = QAJobStore(Path(root) / "qa_jobs.sqlite3")
            for rating in ("helpful", "incorrect", "partial"):
                job = store.create("ask", {"query": rating, "kb_ids": ["kb-1"]})
                store.finish(job["job_id"], "succeeded", result={"status": "no_evidence"})
                store.record_feedback(job["job_id"], {"rating": rating})
            selected = store.list_feedback_candidates(2)
            self.assertEqual([item["rating"] for item in selected], ["incorrect", "partial"])

    def test_stats_are_content_free_and_split_note_jobs(self):
        with tempfile.TemporaryDirectory() as root:
            store = QAJobStore(Path(root) / "qa_jobs.sqlite3")
            original = store.create("ask", {"query": "论文问题", "kb_ids": ["kb-1"], "scope": "original"})
            note = store.create("ask", {"query": "个人记录问题", "kb_ids": ["kb-1"], "scope": "notes"})
            store.finish(original["job_id"], "failed", error="测试")
            store.finish(note["job_id"], "succeeded", result={"status": "answered"})
            stats = store.stats()
            self.assertEqual(stats["total"], 2)
            self.assertEqual(stats["by_state"], {"failed": 1, "succeeded": 1})
            self.assertEqual(stats["notes_total"], 1)
            self.assertEqual(stats["notes_succeeded"], 1)
            self.assertNotIn("个人记录问题", json.dumps(stats, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
