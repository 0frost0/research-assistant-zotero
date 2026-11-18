"""QASPER supervision derivation contracts, invented tiny input only."""
from copy import deepcopy
import unittest

from benchmarks.prepare_qasper_selector import build, partition
from research_assistant.retrieval.selector_dataset import validate_dataset


def paper():
    paragraphs = ["Our algorithm uses iterative graph smoothing for noisy observations.",
                  "The main experiment reports 75 percent accuracy on the first benchmark.",
                  *[f"Distinct background paragraph {i} discusses related algorithms and historical results." for i in range(22)]]
    return {"full_text": [{"section_name": "Methods", "paragraphs": paragraphs}],
            "qas": [{"question_id": "question-a", "question": "What algorithm and results are reported?",
                     "answers": [{"annotation_id": "expert-a", "answer": {"unanswerable": False,
                                                                           "evidence": paragraphs[:2]}}]}]}


class QasperTests(unittest.TestCase):
    def test_audit_recomputes_metrics_and_rejects_tampering(self):
        from benchmarks.audit_selector_report import audit, interval
        from benchmarks.build_selector_synthetic import build as synthetic
        from benchmarks.evaluate_evidence_selector import evaluate
        data = synthetic()
        # Supply one annotator support set for the invented audit fixture.
        for group in data["groups"]:
            group["alternative_evidence_sets"] = [[c["id"] for c in group["candidates"]
                                                     if c["covers"] and not c["id"].endswith("duplicate")]]
        report = evaluate(data)
        self.assertTrue(audit(data, report)["valid"])
        altered = deepcopy(report)
        altered["rows"][0]["dimension_coverage_at_6"] = 0
        self.assertFalse(audit(data, altered)["valid"])
        altered = deepcopy(report)
        altered["rows"][0]["selected_ids"].append("not-in-pool")
        with self.assertRaisesRegex(ValueError, "selected IDs"):
            audit(data, altered)
        stats = interval([("p1", .5), ("p1", .25), ("p2", -.25)], samples=100)
        self.assertEqual(stats["papers"], 2)
        self.assertEqual(stats["questions"], 3)
        self.assertEqual(stats, interval([("p1", .5), ("p1", .25), ("p2", -.25)], samples=100))

    def test_real_annotations_derive_incremental_and_redundancy_labels(self):
        data = build({"train": {"training-paper": paper()}})
        self.assertEqual(validate_dataset(data), [])
        self.assertEqual(data["provenance"]["label_status"], "public_evidence_derived")
        self.assertEqual({r["label"] for r in data["examples"]}, {0, 1})
        self.assertTrue(any(r["selected"] and r["candidate"]["covers"] and not r["label"]
                            for r in data["examples"]))

    def test_heldout_pool_is_not_gold_injected(self):
        data = build({"dev": {"heldout-paper": paper()}})
        self.assertEqual(data["groups"][0]["split"], "test")
        self.assertEqual(data["groups"][0]["candidate_order"], "bm25_top20_no_gold_injection")
        self.assertFalse(any("window" in c["id"] for c in data["groups"][0]["candidates"]))

    def test_float_and_unmatched_annotations_are_not_silently_dropped(self):
        for text in ("FLOAT SELECTED: table 1", "unmatched support"):
            p = paper()
            p["qas"][0]["answers"][0]["answer"]["evidence"].append(text)
            self.assertFalse(build({"train": {"training-paper": p}})["groups"])

    def test_official_split_paper_overlap_refused(self):
        with self.assertRaisesRegex(ValueError, "duplicated"):
            build({"train": {"paper": paper()}, "dev": {"paper": paper()}})

    def test_no_answer_text_or_extractive_spans_in_prompt(self):
        from research_assistant.retrieval.selector_dataset import format_prompt
        p = paper()
        p["qas"][0]["answers"][0]["answer"]["free_form_answer"] = "SECRET ANSWER LABEL"
        data = build({"train": {"training-paper": p}})
        self.assertNotIn("SECRET ANSWER LABEL", format_prompt(data["examples"][0]))


if __name__ == "__main__":
    unittest.main()
