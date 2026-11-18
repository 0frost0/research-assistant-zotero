import json
from pathlib import Path
import tempfile
import unittest
from argparse import Namespace
from contextlib import redirect_stdout
import io

from benchmarks.run_ic6_retrieval import (
    VERSION, BRIDGE, validate_bundle, select, oracle6, read_cache,
    validate_scores, check_manifest,
    evaluate, sha, INSTRUCTION, write_new,
)
from benchmarks.setr_protocol import PROTOCOL


class IC6RetrievalTests(unittest.TestCase):
    def bundle(self):
        chunks = [{"id": str(i), "source_id": "doc", "text": f"metadata\n\npassage {i}",
                   "word_start": 0, "word_end": 2} for i in range(100)]
        return {"version": VERSION, "chunks": chunks, "queries": [{"id": "q", "question": "Who?"}],
                "bm25": {"q": [c["id"] for c in chunks]}, "provenance": {}}

    def test_bundle_rejects_gold_or_type_even_if_not_used(self):
        for field in ("gold", "evidence_list", "answer", "question_type"):
            data = self.bundle()
            data["queries"][0][field] = "leak"
            with self.assertRaises(ValueError):
                validate_bundle(data)
            data = self.bundle()
            data["chunks"][0][field] = "leak"
            with self.assertRaises(ValueError):
                validate_bundle(data)

    def test_complete_real_unique_pools(self):
        validate_bundle(self.bundle())
        for ids in (["0"] * 100, [str(i) for i in range(101)], ["unknown"] + [str(i) for i in range(99)]):
            data = self.bundle()
            data["bm25"]["q"] = ids
            with self.assertRaises(ValueError):
                validate_bundle(data)

    def test_selection_is_deterministic_six_without_filling(self):
        data = self.bundle()
        lookup = {c["id"]: c for c in data["chunks"]}
        ids = list(lookup)
        for mmr in (False, True):
            result = select(ids, [.5] * 100, lookup, mmr=mmr)
            self.assertEqual(len(result), 6)
            self.assertEqual(len(set(result)), 6)
            self.assertEqual(result, select(ids, [.5] * 100, lookup, mmr=mmr))
            self.assertEqual(select(ids[:2], [.4, .8], lookup, mmr=mmr), ["1", "0"])

    def test_mmr_can_avoid_duplicate_at_cutoff_without_document_cap(self):
        ids = [str(i) for i in range(7)]
        lookup = {i: {"text": "header\n\n" + ("alpha beta gamma" if int(i) < 6 else "novel fact evidence")} for i in ids}
        self.assertEqual(select(ids, [1 - i / 10 for i in range(7)], lookup), ids[:6])
        # Small pool rank gap can dominate fixed diversity penalty; no guaranteed gain.
        self.assertEqual(len(select(ids, [1 - i / 10 for i in range(7)], lookup, mmr=True)), 6)

    def test_exact_oracle_respects_six_budget(self):
        self.assertEqual(oracle6([{i} for i in range(7)], 7), 6 / 7)
        self.assertEqual(oracle6([{0}, {0, 1}, {2}], 3), 1)
        self.assertIsNone(oracle6([set()], 0))

    def test_score_validation_and_candidate_order(self):
        row = {"candidate_ids": ["a", "b"], "scores": [.3, .7], "document_tokens": [20, 30]}
        validate_scores(row, ["a", "b"])
        with self.assertRaises(ValueError):
            validate_scores(row, ["b", "a"])
        row["scores"][0] = float("nan")
        with self.assertRaises(ValueError):
            validate_scores(row, ["a", "b"])

    def test_cache_rejects_duplicates_unknown_or_broken_tail(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "cache.jsonl"
            for raw in ('{"id":"q"}\n{"id":"q"}\n', '{"id":"other"}\n', '{"id":"q"}\n{'):
                path.write_text(raw, encoding="utf-8")
                with self.assertRaises((ValueError, json.JSONDecodeError)):
                    read_cache(path, {"q"})

    def test_changed_manifest_fails_without_overwrite(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "manifest.json"
            check_manifest(path, {"input": "a"})
            check_manifest(path, {"input": "a"})
            with self.assertRaises(ValueError):
                check_manifest(path, {"input": "b"})
            self.assertEqual(json.loads(path.read_text()), {"input": "a"})

    def test_bridge_instruction_bounded(self):
        self.assertLessEqual(len(BRIDGE), 512)

    def test_full_evaluator_joins_only_complete_predictions_and_separates_null(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = self.bundle()
            data["queries"].append({"id": "null", "question": "Unanswerable?"})
            data["bm25"]["null"] = data["bm25"]["q"]
            data["chunks"][0]["text"] = "metadata\n\nUnique supporting fact."
            write_new(root / "bundle.json", data)
            candidates = data["chunks"][:20]
            gold = {"protocol": PROTOCOL, "role": "external_test_only", "groups": [
                {"id": "q", "question": "Who?", "question_type": "inference_query",
                 "gold": [{"fact": "Unique supporting fact."}], "candidates": candidates},
                {"id": "null", "question": "Unanswerable?", "question_type": "null_query",
                 "gold": [], "candidates": candidates}]}
            write_new(root / "gold.json", gold)
            ids = data["bm25"]["q"]
            write_new(root / "retrieval.json", {"bundle_sha256": sha(root / "bundle.json"), "pools": {
                qid: {"dense100": ids, "hybrid100": ids} for qid in ("q", "null")}})
            for name in ("original", "bridge", "hybrid"):
                directory = root / name
                directory.mkdir()
                write_new(directory / "manifest.json", {"bundle_sha256": sha(root / "bundle.json"),
                    "instruction": BRIDGE if name == "bridge" else INSTRUCTION})
                with (directory / "scores.jsonl").open("w", encoding="utf-8") as f:
                    for qid in ("q", "null"):
                        f.write(json.dumps({"id": qid, "candidate_ids": ids,
                            "scores": [1 - i / 100 for i in range(100)], "document_tokens": [10] * 100,
                            "pair_count": 100, "seconds": 1.5}) + "\n")
                write_new(directory / "complete.json", {"scores_sha256": sha(directory / "scores.jsonl")})
            args = Namespace(bundle=root / "bundle.json", gold=root / "gold.json", retrieval=root / "retrieval.json",
                             run_dir=root, output=root / "results.json")
            with redirect_stdout(io.StringIO()):
                evaluate(args)
            report = json.loads(args.output.read_text(encoding="utf-8"))
            self.assertEqual(len(report["summary"]), 13)
            for summary in report["summary"].values():
                self.assertEqual(summary["answerable"], 1)
                self.assertEqual(summary["null_questions"], 1)
                self.assertEqual(summary["null_empty_selection_rate"], 0)
                self.assertEqual(summary["ic_at_6"], 1)
                self.assertEqual(summary["selected_count"], 6)
                self.assertEqual(summary["mean_selected_document_tokens"], 60)
            # Even a missing null prediction must fail the complete report gate.
            directory = root / "bridge"
            lines = (directory / "scores.jsonl").read_text().splitlines()
            (directory / "scores.jsonl").write_text(lines[0] + "\n")
            (directory / "complete.json").write_text(json.dumps({"scores_sha256": sha(directory / "scores.jsonl")}))
            args.output = root / "incomplete-results.json"
            with self.assertRaisesRegex(ValueError, "partial"):
                evaluate(args)
            self.assertFalse(args.output.exists())


if __name__ == "__main__":
    unittest.main()
