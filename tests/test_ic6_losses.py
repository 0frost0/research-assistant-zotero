from argparse import Namespace
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest

from benchmarks.audit_ic6_losses import account, audit, summarize
from benchmarks.run_ic6_retrieval import VERSION, sha, write_new
from benchmarks.setr_protocol import PROTOCOL, metrics


class LossAuditTests(unittest.TestCase):
    def test_additive_missing_retrieval_shortlist_selection(self):
        row = account(list("abcd"), [{0}, {1}, {2}], [0, 1], [0], upstream_ok=True)
        self.assertEqual([row[k] for k in ("retrieval_loss", "shortlist_loss", "selection_loss", "achieved_ic6")], [.25]*4)
        self.assertEqual(row["upstream_failure_loss"], 0)

    def test_failed_upstream_not_mislabeled_as_shortlist_or_selection(self):
        row = account(list("abcd"), [{0, 1, 2}], [], [], upstream_ok=False)
        self.assertEqual(row["retrieval_loss"], .25)
        self.assertEqual(row["upstream_failure_loss"], .75)
        self.assertEqual(row["shortlist_loss"], 0)
        self.assertEqual(row["selection_loss"], 0)

    def test_overlap_not_double_counted(self):
        row = account(list("ab"), [{0}, {0, 1}, {1}], [0, 1, 2], [0, 1], upstream_ok=True)
        self.assertEqual(row["achieved_ic6"], 1)
        self.assertEqual(row["selected_gold_count"], 2)

    def test_null_separate_and_failure_kept_in_denominator(self):
        ok = account(["a"], [{0}], [0], [0], upstream_ok=True)
        failed = account(["a"], [{0}], [], [], upstream_ok=False)
        null = account([], [set()], [0], [], upstream_ok=True)
        report = summarize([ok, failed, null])
        self.assertEqual(report["questions"], 3)
        self.assertEqual(report["answerable"], 2)
        self.assertEqual(report["null_questions"], 1)
        self.assertEqual(report["achieved_ic6"], .5)
        self.assertIsNone(null["achieved_ic6"])

    def test_invalid_nested_budget_or_fact_indices_rejected(self):
        for short, chosen in [([0], [1]), ([0, 0], [0]), ([7], []), ([0], [0, 0]),
                               (list(range(7)), list(range(7)))]:
            with self.assertRaises(ValueError):
                account(["a"], [{0}]*7, short, chosen, upstream_ok=True)
        with self.assertRaises(ValueError):
            account(["a"], [{0}], [0], [], upstream_ok=False)
        with self.assertRaises(ValueError):
            account(["a"], [{1}], [0], [], upstream_ok=True)

    def test_complete_audit_reproduces_baseline_rejects_partial_and_emits_no_text(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chunks = [{"id": str(i), "source_id": "doc", "text": "SECRET_FACT" if i == 0 else "other",
                       "word_start": i, "word_end": i + 1} for i in range(100)]
            ids = [c["id"] for c in chunks]
            bundle = {"version": VERSION, "chunks": chunks, "queries": [{"id": "q", "question": "query"}],
                      "bm25": {"q": ids}, "provenance": {}}
            write_new(root / "bundle.json", bundle)
            write_new(root / "retrieval.json", {"bundle_sha256": sha(root / "bundle.json"),
                      "pools": {"q": {"hybrid100": ids}}})
            group = {"id": "q", "question": "query", "status": "ok", "candidate_ids": ids[:32],
                     "baseline_selected_ids": ids[:6]}
            write_new(root / "inputs.json", {"chunks": chunks, "groups": [group],
                      "fingerprints": {"bundle": sha(root / "bundle.json"), "retrieval": sha(root / "retrieval.json")}})
            label = {"id": "q", "question": "query", "question_type": "inference_query",
                     "gold": [{"fact": "SECRET_FACT"}]}
            write_new(root / "gold.json", {"protocol": PROTOCOL, "role": "external_test_only", "groups": [label]})
            row = {"id": "q", "status": "ok", "question_type": label["question_type"],
                   "selected_ids": ids[:6], **metrics(chunks, label["gold"], list(range(6)))}
            report = {"summary": {"ic_at_6": 1}, "rows": [row],
                      "fingerprints": {"bundle": sha(root / "bundle.json"), "gold": sha(root / "gold.json")}}
            write_new(root / "baseline.json", report)
            args = Namespace(bundle=root / "bundle.json", retrieval=root / "retrieval.json", inputs=root / "inputs.json",
                             gold=root / "gold.json", baseline_report=root / "baseline.json", output=root / "audit.json")
            with redirect_stdout(io.StringIO()):
                audit(args)
            result = args.output.read_text()
            self.assertNotIn("SECRET_FACT", result)
            self.assertEqual(json.loads(result)["summary"]["achieved_ic6"], 1)
            report["rows"] = []
            args.baseline_report.write_text(json.dumps(report))
            args.output = root / "bad.json"
            with self.assertRaises(ValueError):
                audit(args)
            self.assertFalse(args.output.exists())
