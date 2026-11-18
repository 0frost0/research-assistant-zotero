import unittest
import contextlib
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
from benchmarks.run_library_acceptance import assess


class AcceptanceAssessmentTests(unittest.TestCase):
    def test_failed_request_is_not_counted_as_successful_refusal(self):
        self.assertFalse(assess({"no_answer": True}, {"status": "unavailable", "claims": []})[
            "expected_behavior_observed"])

    def test_wrong_page_cannot_pass_same_paper_anchor_check(self):
        case = {"expected": [{"source_id": "paper.pdf", "page": 2, "quote": "correct excerpt"}]}
        result = {"status": "answered", "claims": [{"citations": [
            {"source_id": "paper.pdf", "page": 1, "quote": "correct excerpt"}]}]}
        self.assertFalse(assess(case, result)["expected_behavior_observed"])

    def test_valid_anchor_is_distinct_from_human_semantic_review(self):
        case = {"expected": [{"source_id": "paper.pdf", "page": 2, "quote": "correct excerpt"}]}
        result = {"status": "partial", "claims": [{"citations": [
            {"source_id": "paper.pdf", "page": 2, "quote": "correct excerpt with context"}]}]}
        row = assess(case, result)
        self.assertTrue(row["expected_behavior_observed"])
        self.assertIsNone(row["human_semantic_review"])

    def test_uncited_parent_text_cannot_rescue_the_actual_quote(self):
        case = {"expected": [{"source_id": "paper.pdf", "page": 2, "quote": "correct excerpt"}]}
        result = {"status": "answered", "claims": [{"citations": [
            {"source_id": "paper.pdf", "page": 2, "quote": "correct excerpt plus background",
             "support_quote": "background"}]}]}
        self.assertFalse(assess(case, result)["expected_behavior_observed"])

    def test_old_note_revision_is_not_current_memory(self):
        case = {"expected": [{"entity_id": "n1", "revision": 2}]}
        result = {"status": "answered", "claims": [{"citations": [{"entity_id": "n1", "revision": 1}]}]}
        self.assertFalse(assess(case, result)["expected_behavior_observed"])

    def test_no_answer_only_memory_evaluation_reports_unmeasured_metrics(self):
        from benchmarks.build_personal_memory_smoke_corpus import build
        from benchmarks.evaluate_personal_memory import evaluate
        with tempfile.TemporaryDirectory() as directory, contextlib.redirect_stdout(io.StringIO()):
            root = Path(directory) / 'corpus'
            build(root)
            dataset = root / 'dataset.json'
            data = json.loads(dataset.read_text(encoding='utf-8'))
            case = next(c for c in data['cases'] if c['no_answer'])
            output = root / 'report.json'
            evaluate(SimpleNamespace(root=root, dataset=dataset, output=output, expected_split='test',
                                     case_ids=[case['id']], max_cases=1, backend='tfidf',
                                     apply_memory_gate=True, min_coverage=0.20))
            report = json.loads(output.read_text(encoding='utf-8'))
            self.assertEqual(report['summary']['recall_at_6'], {'mean': None, 'n': 0})
