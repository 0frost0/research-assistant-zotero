"""AI provenance and immutable imports, using isolated SQLite fixtures only."""

import copy
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from benchmarks.review_evidence import compile_decisions, review_packet
from research_assistant.evaluation.retrieval_evaluation import EvaluationStore, ai_summary, review_snapshot_hash, score_ranking
from tests.test_retrieval_lab import fixture_case, hit


class AIReviewTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = EvaluationStore(Path(self.tmp.name) / "evaluation.sqlite3")
        case = fixture_case()
        case["rankings"] = {"raw": [hit("t1"), hit("i1", 2)], "displayed": [hit("t1")]}
        case["metrics"] = {m: score_ranking(case, ranks) for m, ranks in case["rankings"].items()}
        self.run = {"run_id": "fixture-run", "created_at": "today", "dataset_hash": "v1", "cases": [case],
                    "settings": {"threshold": .35}, "suitability": "not_established"}
        self.store.save(self.run)
        self.decisions = {"run_id": self.run["run_id"], "snapshot_hash": review_snapshot_hash(self.run),
            "method": "Fixture, not a real annotation", "limitations": ["Test only"], "source_checks": ["Synthetic"],
            "rubric": {"direct": {"grade": 3, "note": "Direct fixture evidence"},
                       "partial": {"grade": 2, "note": "Partial fixture evidence"}},
            "cases": {"C1": {"labels": ["E001:direct", "E002:partial"], "status": "consistent",
                             "note": "Synthetic case", "confidence": "high", "needs_human_review": False}}}
        self.package = compile_decisions(self.run, self.decisions)

    def test_compiler_requires_explicit_alias_order_and_all_cases(self):
        pool, cases = review_packet(self.run)
        self.assertEqual(len(pool), 2)
        self.assertEqual(len(cases[0]["review_items"]), 2)
        for change in (lambda d: d["cases"]["C1"].update(labels=["E002:direct", "E001:partial"]),
                       lambda d: d["cases"]["C1"].update(labels=["E001:direct"]),
                       lambda d: d.update(cases={}), lambda d: d.update(snapshot_hash="wrong")):
            data = copy.deepcopy(self.decisions); change(data)
            with self.assertRaises(ValueError): compile_decisions(self.run, data)

    def test_ai_does_not_create_human_grades_or_approvals(self):
        before = self.store.get(self.run["run_id"])
        self.store.import_ai_review(self.package)
        after = self.store.get(self.run["run_id"])
        self.assertEqual(after["judgments"], [])
        self.assertEqual(after["approvals"], [])
        self.assertEqual(after["summary"], before["summary"])
        self.assertEqual(after["human_relevance"], before["human_relevance"])
        self.assertEqual(after["suitability"], "not_established")
        self.assertEqual(after["ai_review"]["annotation_origin"], "ai_assisted")
        self.assertEqual(after["ai_relevance"]["raw"]["all"]["mean_grade"], 2.5)
        self.assertEqual(after["ai_relevance"]["displayed"]["all"]["mean_grade"], 3)
        self.assertEqual(after["ai_relevance"]["coverage"]["all"]["pairs"], 2)
        self.assertEqual(review_snapshot_hash(after), review_snapshot_hash(self.run))

    def test_human_annotation_remains_independent_and_is_preserved(self):
        self.store.judge(self.run["run_id"], {"case_id": "C1", "point_id": "t1", "grade": 0, "reviewer": "fixture-human"})
        self.store.judge(self.run["run_id"], {"case_id": "C1", "action": "approve_gold", "reviewer": "fixture-human"})
        self.store.import_ai_review(self.package)
        result = self.store.get(self.run["run_id"])
        self.assertEqual(result["human_relevance"]["raw"]["all"]["mean_grade"], 0)
        self.assertEqual(result["ai_relevance"]["raw"]["all"]["mean_grade"], 2.5)
        self.assertEqual(result["approvals"][0]["reviewer"], "fixture-human")

    def test_invalid_imports_are_atomic(self):
        changes = [lambda p: p.update(annotation_origin="human"), lambda p: p.update(snapshot_hash="wrong"),
                   lambda p: p.update(dataset_hash="other"), lambda p: p.update(run_id="unknown"),
                   lambda p: p["judgments"].pop(), lambda p: p["judgments"].append(p["judgments"][0]),
                   lambda p: p["judgments"][0].update(point_id="unknown"),
                   lambda p: p["judgments"][0].update(grade=True), lambda p: p["judgments"][0].update(grade=4),
                   lambda p: p["judgments"][0].update(grade=2.0), lambda p: p["judgments"][0].update(note=""),
                   lambda p: p.update(case_reviews=[]), lambda p: p["case_reviews"].append(p["case_reviews"][0]),
                   lambda p: p["case_reviews"][0].update(needs_human_review="false"),
                   lambda p: p["case_reviews"][0].update(status="human_approved"),
                   lambda p: p["case_reviews"][0].update(note=""), lambda p: p.update(source_checks=[])]
        for change in changes:
            package = copy.deepcopy(self.package); change(package)
            with self.subTest(package=package), self.assertRaises(ValueError): self.store.import_ai_review(package)
            self.assertIsNone(self.store.get(self.run["run_id"])["ai_review"])

    def test_same_import_is_idempotent_different_import_is_refused(self):
        self.store.import_ai_review(self.package)
        first = self.store.get(self.run["run_id"])["ai_review"]
        self.store.import_ai_review(copy.deepcopy(self.package))
        self.assertEqual(self.store.get(self.run["run_id"])["ai_review"], first)
        changed = copy.deepcopy(self.package); changed["judgments"][0]["grade"] = 0
        with self.assertRaisesRegex(ValueError, "immutable"): self.store.import_ai_review(changed)
        self.assertEqual(self.store.get(self.run["run_id"])["ai_review"], first)

    def test_snapshot_includes_text_rankings_and_settings_not_human_state(self):
        for change in (lambda r: r["cases"][0]["rankings"]["raw"][0].update(text="changed"),
                       lambda r: r["settings"].update(threshold=.9), lambda r: r.update(model="another")):
            run = copy.deepcopy(self.run); change(run)
            self.assertNotEqual(review_snapshot_hash(run), review_snapshot_hash(self.run))
        changed = {**self.run, "judgments": [{"grade": 3}], "approvals": ["fake"]}
        self.assertEqual(review_snapshot_hash(changed), review_snapshot_hash(self.run))

    def test_ai_never_copied_to_other_run_with_same_dataset(self):
        self.store.import_ai_review(self.package)
        self.store.save({**self.run, "run_id": "other-run"})
        self.assertIsNone(self.store.get("other-run")["ai_review"])
        package = copy.deepcopy(self.package); package["run_id"] = "other-run"
        with self.assertRaises(ValueError): self.store.import_ai_review(package)

    def test_probe_and_negative_coverage_is_separate(self):
        base = self.run["cases"][0]
        rows = [base, {**base, "id": "P1", "split": "self_match_probe"},
                {**base, "id": "N1", "unanswerable": True}]
        judgments = [{"case_id": c["id"], "point_id": h["point_id"], "grade": 0 if c["id"] == "N1" else 3}
                     for c in rows for h in c["rankings"]["raw"]]
        summary = ai_summary(rows, judgments)
        self.assertEqual(summary["raw"]["all"]["returned"], 2)
        self.assertEqual(summary["raw"]["all"]["mean_grade"], 3)
        self.assertEqual(summary["coverage"]["all"]["pairs"], 6)
        self.assertEqual(summary["coverage"]["negative_controls"]["grade_counts"]["0"], 2)
        self.assertEqual(summary["coverage"]["self_match_probe"]["pairs"], 2)

    def test_saved_run_payload_unchanged(self):
        with closing(sqlite3.connect(self.store.path)) as con:
            before = con.execute("SELECT payload FROM runs").fetchone()[0]
        self.store.import_ai_review(self.package)
        with closing(sqlite3.connect(self.store.path)) as con:
            self.assertEqual(con.execute("SELECT payload FROM runs").fetchone()[0], before)


if __name__ == "__main__":
    unittest.main()
