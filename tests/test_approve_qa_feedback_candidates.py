import unittest
import json
import tempfile
from pathlib import Path

from benchmarks.approve_qa_feedback_candidates import approve, build_dataset
from benchmarks.build_qa_smoke_corpus import build as build_smoke_corpus


class ApproveQAFeedbackTests(unittest.TestCase):
    def candidate_file(self):
        return {
            "id": "qa-feedback-candidates-v1", "status": "unapproved",
            "cases": [{
                "id": "feedback-one", "job_id": "job-1", "query": "研究纳入多少人？",
                "scope": "original", "kb_ids": ["kb-1"], "proposed_no_answer": False,
                "proposed_evidence": [{"source_id": "abc.pdf", "page": 2,
                                       "quote": "The study enrolled 200 adults."}],
            }, {
                "id": "feedback-two", "job_id": "job-2", "query": "有没有剂量信息？",
                "scope": "original", "kb_ids": ["kb-1"], "proposed_no_answer": True,
                "proposed_evidence": [],
            }]
        }

    def test_requires_explicit_human_decision_and_evidence(self):
        candidates = self.candidate_file()
        with self.assertRaises(ValueError):
            build_dataset(candidates, {"reviewer": "研究者", "cases": [
                {"candidate_id": "feedback-one", "decision": "approve", "note": "看起来对"}
            ]})
        dataset = build_dataset(candidates, {"reviewer": "研究者", "cases": [
            {"candidate_id": "feedback-one", "decision": "approve", "note": "已核对原文",
             "no_answer": False, "expected": [{"source_id": "abc.pdf", "page": 2,
                                                  "quote": "The study enrolled 200 adults."}]},
            {"candidate_id": "feedback-two", "decision": "approve", "note": "原文没有剂量",
             "no_answer": True, "expected": []},
        ]})
        self.assertEqual(dataset["review_status"], "human_approved")
        self.assertEqual(dataset["approved_count"], 2)
        self.assertFalse(dataset["cases"][1]["expected"])

    def test_rejects_notes_and_evidence_not_proposed(self):
        candidates = self.candidate_file()
        candidates["cases"][0]["scope"] = "notes"
        with self.assertRaises(ValueError):
            build_dataset(candidates, {"reviewer": "研究者", "cases": [
                {"candidate_id": "feedback-one", "decision": "approve", "note": "核对",
                 "no_answer": False, "expected": [{"source_id": "abc.pdf", "page": 2,
                                                      "quote": "The study enrolled 100 adults."}]}
            ]})

    def test_approved_output_is_checked_against_current_snapshot(self):
        with tempfile.TemporaryDirectory() as root_name:
            root = Path(root_name) / "corpus"
            build_smoke_corpus(root)
            source = json.loads((root / "dataset.json").read_text(encoding="utf-8"))["cases"][0]
            candidates = {"id": "qa-feedback-candidates-v1", "status": "unapproved", "cases": [{
                "id": "feedback-real", "query": source["query"], "scope": "original",
                "kb_ids": source["kb_ids"], "proposed_evidence": [{
                    "source_id": source["expected"][0]["source_id"],
                    "page": source["expected"][0]["page"],
                    "quote": source["expected"][0]["quote"],
                }],
            }]}
            candidate_path = root / "candidates.json"
            review_path = root / "review.json"
            output_path = root / "approved.json"
            candidate_path.write_text(json.dumps(candidates, ensure_ascii=False), encoding="utf-8")
            review_path.write_text(json.dumps({"reviewer": "研究者", "cases": [{
                "candidate_id": "feedback-real", "decision": "approve", "note": "已核对页面",
                "no_answer": False, "expected": candidates["cases"][0]["proposed_evidence"],
            }]}, ensure_ascii=False), encoding="utf-8")
            result = approve(candidate_path, review_path, output_path, root)
            self.assertEqual(result["checked_evidence"], 1)
            self.assertEqual(json.loads(output_path.read_text(encoding="utf-8"))["split"], "development")
        candidates["cases"][0]["scope"] = "original"
        with self.assertRaises(ValueError):
            build_dataset(candidates, {"reviewer": "研究者", "cases": [
                {"candidate_id": "feedback-one", "decision": "approve", "note": "核对",
                 "no_answer": False, "expected": [{"source_id": "abc.pdf", "page": 2,
                                                      "quote": "The study enrolled 100 adults."}]}
            ]})


if __name__ == "__main__":
    unittest.main()
