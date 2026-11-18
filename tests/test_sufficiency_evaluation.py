"""Evaluation-arm/budget contracts only; never contact a provider."""
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from benchmarks.evaluate_knowledge_qa import evaluate, evaluation_arms, model_call_bound


class SufficiencyEvaluationTests(unittest.TestCase):
    def test_default_budget_and_paired_gate_budget(self):
        strategies = ["fixed", "selected", "adaptive"]
        self.assertEqual(model_call_bound(evaluation_arms(strategies, ["off"], True)), 8)
        self.assertEqual(model_call_bound(evaluation_arms(strategies, ["off", "on"], True)), 18)
        self.assertEqual(evaluation_arms(["adaptive"], ["off", "on"], True),
                         [("adaptive", False), ("adaptive", True)])

    def test_no_silent_paid_gate_or_duplicate_arms(self):
        for strategies, gates, paid in ((["adaptive"], ["on"], False),
                                        (["adaptive"] * 2, ["off"], True),
                                        (["fixed"], ["off", "off"], True)):
            with self.assertRaises(ValueError):
                evaluation_arms(strategies, gates, paid)


class PairedEvaluationTests(unittest.IsolatedAsyncioTestCase):
    async def test_paired_report_uses_same_cases_algorithm_and_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dataset = root / "dataset.json"
            dataset.write_text(json.dumps({"id": "fixture", "split": "synthetic", "cases": [
                {"id": "one", "query": "question", "kb_ids": ["kb"], "no_answer": False,
                 "expected": [{"source_id": "paper.pdf", "page": 1, "quote": "passage"}]}]}))
            args = SimpleNamespace(root=root, dataset=dataset, output=root / "report.json",
                expected_split="synthetic", allow_synthetic=True, strategies=["adaptive"],
                sufficiency_gates=["off", "on"], with_model=True, algorithm="hybrid",
                case_ids=None, max_cases=1, backend="tfidf")
            store = Mock(spec=["snapshot", "assert_current"])
            store.snapshot.return_value = {"bases": [{"id": "kb", "revision": 7}]}
            qa = Mock()
            qa.ask = AsyncMock(return_value={"status": "no_evidence", "claims": [], "evidence": [],
                "trace": {"total_tokens": 0, "unknown_usage_calls": 1}})
            with patch("benchmarks.evaluate_knowledge_qa.validate_dataset", return_value={"valid": True}), \
                 patch("benchmarks.evaluate_knowledge_qa.KnowledgeStore", return_value=store), \
                 patch("benchmarks.evaluate_knowledge_qa.KnowledgeService"), \
                 patch("benchmarks.evaluate_knowledge_qa.EvidenceQA", return_value=qa), \
                 patch("benchmarks.evaluate_knowledge_qa.search_ready", new=AsyncMock(
                     return_value={"results": [], "backend": "fixture"})):
                await evaluate(args)
            requests = [call.args[0] for call in qa.ask.call_args_list]
            self.assertEqual([r["sufficiency_gate"] for r in requests], [False, True])
            self.assertTrue(all(r["algorithm"] == "hybrid" and r["kb_ids"] == ["kb"] for r in requests))
            report = json.loads(args.output.read_text())
            self.assertEqual(set(report["summary"]), {"adaptive+gate", "adaptive+no_gate"})
            self.assertEqual(report["max_generation_calls_per_case"], 8)
            self.assertTrue(all(r["revisions"] == {"kb": 7} and r["human_support"] is None for r in report["cases"]))
