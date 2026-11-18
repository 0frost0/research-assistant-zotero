import copy
import unittest

from benchmarks.analyze_ic6_joint import analyze, compare_rows
from benchmarks.setr_protocol import PROTOCOL


class JointAnalysisTests(unittest.TestCase):
    def row(self, arm, indices, *, qid="q", n=3, status="ok"):
        return {"arm": arm, "id": qid, "question_type": "inference_query" if n else "null_query",
                "gold_count": n, "covered_gold_indices": indices, "covered_gold_count": len(indices),
                "ic_at_6": len(indices)/n if n else None, "status": status}

    def test_equal_score_can_hide_a_fact_swap(self):
        row = compare_rows(self.row("coverage_anchors", [0, 1]), self.row("joint_8b", [1, 2]))
        self.assertEqual(row["retained_facts"], 1)
        self.assertEqual(row["gained_facts"], 1)
        self.assertEqual(row["lost_facts"], 1)
        self.assertEqual(row["outcome"], "tie")
        self.assertTrue(row["equal_score_fact_swap"])

    def test_format_failure_counts_lost_coverage_and_null_has_no_ic(self):
        row = compare_rows(self.row("coverage_anchors", [0, 1]), self.row("joint_8b", [], status="invalid"))
        self.assertEqual(row["delta_ic6"], -2/3)
        self.assertEqual(row["joint_failure_lost_ic"], 2/3)
        row = compare_rows(self.row("coverage_anchors", [], n=0), self.row("joint_8b", [], n=0))
        self.assertIsNone(row["delta_ic6"])
        self.assertEqual(row["outcome"], "null")

    def report(self):
        return {"version": "test", "protocol": PROTOCOL, "exploratory_not_independent_test": True,
                "rows": [self.row("coverage_anchors", [0]), self.row("joint_8b", [0, 1])],
                "summary": {arm: {"questions": 1, "answerable": 1, "null_questions": 0, "ic_at_6": value}
                            for arm, value in [("coverage_anchors", 1/3), ("joint_8b", 2/3)]}, "delta_ic6": 1/3}

    def test_complete_report_and_loss_identity(self):
        result = analyze(self.report())
        self.assertEqual(result["summary"]["win"], 1)
        self.assertEqual(result["summary"]["delta_ic6"], 1/3)
        self.assertEqual(result["summary"]["gained_ic"]-result["summary"]["lost_ic"], 1/3)

    def test_partial_duplicate_or_corrupt_counts_rejected(self):
        for mutate in [lambda r: r["rows"].pop(), lambda r: r["rows"].append(copy.deepcopy(r["rows"][0])),
                       lambda r: r["rows"][0].update(covered_gold_count=99),
                       lambda r: r["rows"][0].update(covered_gold_indices=[0, 0]),
                       lambda r: r["rows"][0].update(status="invalid"),
                       lambda r: r.update(delta_ic6=0),
                       lambda r: r["summary"]["joint_8b"].update(questions=2)]:
            report = self.report()
            mutate(report)
            with self.assertRaises(ValueError):
                analyze(report)
