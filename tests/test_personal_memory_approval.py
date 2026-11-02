"""Human-gated personal-memory dataset approval tests."""

import json
from pathlib import Path
import tempfile
import unittest

from benchmarks.approve_personal_memory_candidates import approve, build_dataset
from research_assistant.knowledge.store import KnowledgeStore


class PersonalMemoryApprovalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "app"
        self.store = KnowledgeStore(self.root)
        self.base = self.store.create({"connection": "fixture", "library": "user:0",
                                       "collection": "memory", "name": "Memory"})
        self.note = self.store.save_note(self.base["id"], {
            "title": "方法理解", "body": "我理解这个方法需要先做校准再比较。",
            "type": "understanding"})

    def candidates(self, scope="notes"):
        return {
            "id": "personal-memory-review-template-v1",
            "status": "needs_human_review",
            "cases": [{"id": "M1", "query": "我之前如何理解校准？",
                       "kb_ids": [self.base["id"]], "scope": scope,
                       "candidate_records": [{"entity_id": self.note["id"],
                                               "revision": self.note["revision"],
                                               "kind": "user_note",
                                               "quote": self.note["body"]}]}],
        }

    def test_approval_requires_notes_scope_and_current_record(self):
        review = {"reviewer": "tester", "cases": [{
            "candidate_id": "M1", "decision": "approve", "no_answer": False,
            "note": "在笔记正文和版本号中核对。", "expected": [{
                "entity_id": self.note["id"], "revision": 1,
                "kind": "user_note", "quote": self.note["body"]}]}]}
        dataset = build_dataset(self.candidates(), review, "development")
        self.assertEqual(dataset["review_status"], "human_approved")
        self.assertEqual(dataset["cases"][0]["expected"][0]["revision"], 1)
        original = self.candidates()
        original["id"] = "qa-feedback-candidates-v1"
        original["cases"][0]["scope"] = "original"
        original["cases"][0]["proposed_evidence"] = original["cases"][0].pop("candidate_records")
        with self.assertRaises(ValueError):
            build_dataset(original, review)

    def test_stale_revision_fails_before_writing_output(self):
        candidates = self.candidates()
        review = {"reviewer": "tester", "cases": [{
            "candidate_id": "M1", "decision": "approve", "no_answer": False,
            "note": "旧版本不能封存。", "expected": [{
                "entity_id": self.note["id"], "revision": 1,
                "kind": "user_note", "quote": self.note["body"]}]}]}
        self.store.save_note(self.base["id"], {"body": "我修正了这个理解。",
                                               "expected_revision": 1}, self.note["id"])
        candidate_path = self.root / "candidate.json"
        review_path = self.root / "review.json"
        output_path = self.root / "gold.json"
        candidate_path.write_text(json.dumps(candidates, ensure_ascii=False), encoding="utf-8")
        review_path.write_text(json.dumps(review, ensure_ascii=False), encoding="utf-8")
        with self.assertRaises(ValueError):
            approve(candidate_path, review_path, output_path, self.root)
        self.assertFalse(output_path.exists())

    def test_rejected_only_queue_does_not_make_gold(self):
        review = {"reviewer": "tester", "cases": [{
            "candidate_id": "M1", "decision": "reject", "note": "没有足够上下文。"}]}
        with self.assertRaises(ValueError):
            build_dataset(self.candidates(), review)


if __name__ == "__main__":
    unittest.main()
