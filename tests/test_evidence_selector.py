"""Deterministic contracts, not claims about learned research-paper quality."""
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from benchmarks.build_selector_synthetic import build
from benchmarks.build_evidence_selector_dataset import build as build_proxy
from benchmarks.evaluate_evidence_selector import (evaluate, set_metrics, RELEVANCE_INSTRUCTION,
                                                  valid_scores, relevance_cache_key)
from benchmarks.train_evidence_selector import bounded_encode, training_rows
from research_assistant.retrieval.learned_selector import greedy_select
from research_assistant.retrieval.selector_dataset import (format_prompt, validate_dataset,
    write_dataset, load_dataset, make_example)


class SelectorTests(unittest.TestCase):
    def test_identical_questions_do_not_collide_across_candidate_pools(self):
        q = "What datasets are used?"
        a = relevance_cache_key(q, ["paper A", "second A"])
        b = relevance_cache_key(q, ["paper B", "second B"])
        self.assertNotEqual(a, b)
        self.assertNotEqual(a, relevance_cache_key(q, ["second A", "paper A"]))
        self.assertEqual(a, relevance_cache_key(q, ["paper A", "second A"]))

    def test_score_validation_is_finite_and_rejects_bool(self):
        self.assertTrue(valid_scores([0, .5, 1], 3))
        self.assertFalse(valid_scores([True], 1))
        self.assertFalse(valid_scores([float("nan")], 1))
        self.assertFalse(valid_scores([2], 1))
    def test_offline_relevance_instruction_matches_production(self):
        from research_assistant.retrieval.reranking import INSTRUCTION
        self.assertEqual(RELEVANCE_INSTRUCTION, INSTRUCTION)

    def test_synthetic_valid_and_has_state_dependent_labels(self):
        data = build()
        self.assertEqual(validate_dataset(data), [])
        rows = [r for r in data["examples"] if r["split"] == "train" and
                r["candidate"]["id"].endswith("duplicate")]
        self.assertEqual({r["label"] for r in rows}, {0, 1})

    def test_prompt_does_not_contain_gold_or_ids(self):
        row = build()["examples"][0]
        prompt = format_prompt(row)
        for token in ("covers", "added_dimensions", "label", "source_id", row["candidate"]["id"]):
            self.assertNotIn(token, prompt)
        self.assertIn(row["candidate"]["quote"], prompt)

    def test_cross_split_distractor_rejected(self):
        data = build()
        data["groups"][0]["candidates"].append(deepcopy(data["groups"][1]["candidates"][0]))
        self.assertTrue(any("leaks" in e for e in validate_dataset(data)))

    def test_same_source_or_text_in_two_splits_rejected(self):
        for key in ("source_id", "quote"):
            data = build()
            data["groups"][1]["candidates"][0][key] = data["groups"][0]["candidates"][0][key]
            self.assertTrue(any("multiple splits" in e for e in validate_dataset(data)))

    def test_duplicate_split_rejected(self):
        data = build()
        data["paper_splits"]["dev"].extend(data["paper_splits"]["train"])
        self.assertTrue(validate_dataset(data))

    def test_altered_labels_and_boolean_labels_rejected(self):
        for value in (0, True):
            data = build()
            data["examples"][0]["label"] = value
            self.assertTrue(validate_dataset(data))

    def test_unknown_or_malformed_inputs_rejected(self):
        for value in (None, "bad", 3):
            data = build()
            data["groups"][0]["candidates"][0] = value
            self.assertTrue(validate_dataset(data))

    def test_proxy_never_automatically_labels_other_need_negative(self):
        source = {"id": "fixture", "split": "development", "cases": [
            {"id": str(i), "paper": "paper", "query": str(i), "category": "method",
             "expected": [{"source_id": "a", "page": i + 1, "quote": "fact" + str(i)}]}
            for i in range(2)]}
        data = build_proxy(source)
        self.assertEqual(validate_dataset(data), [])
        self.assertTrue(any(c["covers"] is None for c in data["groups"][0]["candidates"]))
        with self.assertRaisesRegex(ValueError, "development-only"):
            training_rows(data)
        with self.assertRaisesRegex(ValueError, "unjudged"):
            evaluate(data)

    def test_train_uses_only_train_and_synthetic_requires_explicit_flag(self):
        data = build()
        with self.assertRaisesRegex(ValueError, "synthetic"):
            training_rows(data)
        self.assertEqual({r["split"] for r in training_rows(data, synthetic_smoke=True)}, {"train"})

    def test_save_refuses_overwrite_and_roundtrips(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "dataset.json"
            data = build()
            write_dataset(path, data)
            self.assertEqual(load_dataset(path), data)
            with self.assertRaises(FileExistsError):
                write_dataset(path, data)

    def test_no_token_truncation(self):
        class Tokenizer:
            def __call__(self, prompts, **kw):
                assert kw["truncation"] is False
                return {"input_ids": [[1] * len(p) for p in prompts]}
            def pad(self, encoded, **kw):
                return encoded
        with self.assertRaisesRegex(ValueError, "no conditions"):
            bounded_encode(Tokenizer(), ["123456"], 4)
        self.assertEqual(bounded_encode(Tokenizer(), ["123"], 4)["input_ids"], [[1, 1, 1]])

    def test_missing_candidate_dimension_exposed(self):
        group = deepcopy(build()["groups"][0])
        group["candidates"] = [c for c in group["candidates"] if "limitation" not in c["covers"]]
        result = set_metrics(group, group["candidates"][:6])
        self.assertEqual(result["candidate_dimension_recall"], .75)
        self.assertEqual(result["available_dimension_recall_at_6"], 1)
        self.assertEqual(result["missing_candidate_dimensions"], ["limitation"])

    def test_complementary_and_redundant_labels(self):
        group = build()["groups"][0]
        first, duplicate, condition = group["candidates"][:3]
        self.assertEqual(make_example(group, [first], duplicate)["label"], 0)
        self.assertEqual(make_example(group, [first], condition)["label"], 1)

    def test_greedy_is_bounded_and_fail_closed(self):
        group = build()["groups"][0]
        seen = []
        def scorer(prompts):
            seen.extend(prompts)
            return [0.5] * len(prompts)
        selected = greedy_select(group["candidates"], group["question"], scorer, char_budget=200)
        self.assertLessEqual(sum(len(c["quote"]) for c in selected), 200)
        self.assertLessEqual(len(selected), 6)
        self.assertTrue(seen)
        for scores in ([float("nan")] * 7, [], [2.] * 7):
            with self.assertRaises(ValueError):
                greedy_select(group["candidates"], group["question"], lambda _: scores)

    def test_greedy_never_exceeds_six_or_mutates_pool(self):
        group = build()["groups"][0]
        original = deepcopy(group)
        selected = greedy_select(group["candidates"], group["question"], lambda p: [.5] * len(p), limit=20)
        self.assertEqual(len(selected), 6)
        self.assertEqual(group, original)

    def test_evaluator_rejects_bad_reranker_scores(self):
        for scores in ([], [float("nan")] * 7):
            with self.assertRaisesRegex(ValueError, "reranker"):
                evaluate(build(), relevance_scorer=lambda q, d: scores)

    def test_eval_same_pool_and_no_quotes_in_report(self):
        data = build()
        report = evaluate(data)
        self.assertEqual(len({r["candidate_set_sha256"] for r in report["rows"]}), 1)
        self.assertFalse(report["effectiveness_claim"])
        self.assertNotIn(data["groups"][1]["candidates"][0]["quote"], str(report))


if __name__ == "__main__":
    unittest.main()
