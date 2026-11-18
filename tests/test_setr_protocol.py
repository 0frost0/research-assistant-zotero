import unittest

from benchmarks.setr_protocol import evidence_sets, metrics, messages, parse_selection
from benchmarks.setr_protocol import summarize, PROTOCOL
from benchmarks.prepare_setr_training import prepare
from benchmarks.train_setr_adapter import training_examples
from benchmarks.evaluate_setr import evaluate, read_predictions
from benchmarks.run_setr_generation import input_fingerprint, model_loader_name, select_groups, file_sha256
from benchmarks.audit_setr_teacher import audit
from benchmarks.run_setr_generation import verify_model_files
import json
from pathlib import Path
import tempfile


class SetRProtocolTests(unittest.TestCase):
    def test_local_verification_receipt_detects_changed_weights_and_configs(self):
        import hashlib
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            weight, config = root / "model.safetensors", root / "config.json"
            weight.write_bytes(b"verified weight")
            config.write_bytes(b"{}")
            manifest, receipt = root / "manifest.json", root / "receipt.json"
            manifest.write_text(json.dumps({"files": {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                                                        for p in (weight, config)}}))
            verify_model_files(root, manifest, receipt)
            with patch("benchmarks.run_setr_generation.file_sha256", wraps=file_sha256) as hashing:
                verify_model_files(root, manifest, receipt)
                self.assertNotIn(weight, [c.args[0] for c in hashing.call_args_list])
                self.assertIn(config, [c.args[0] for c in hashing.call_args_list])
            weight.write_bytes(b"changed weight")
            with self.assertRaises(ValueError):
                verify_model_files(root, manifest, receipt)

    def test_teacher_gate_checks_splits_independently_and_is_not_semantic_score(self):
        data = {"schema": "setr-style-source-v1", "groups": [
            {"id": "a", "split": "train"}, {"id": "b", "split": "dev"}]}
        predictions = {"a": {"status": "ok", "selected_indices": [0]},
                       "b": {"status": "output_limit", "selected_indices": []}}
        result = audit(data, predictions)
        self.assertFalse(result["passed"])
        self.assertEqual(result["splits"]["train"]["valid_rate"], 1)
        self.assertEqual(result["splits"]["dev"]["valid_rate"], 0)
        predictions["b"]["status"] = "ok"
        self.assertTrue(audit(data, predictions)["passed"])
        with self.assertRaises(ValueError):
            audit({**data, "role": "external_test_only"}, predictions)

    def test_training_script_captures_reload_probe_in_eval_mode(self):
        source = Path(__file__).resolve().parents[1] / "benchmarks" / "train_setr_adapter.py"
        text = source.read_text(encoding="utf-8")
        self.assertIn("model.eval()", text)
        self.assertLess(text.index("model.eval()"), text.index("expected = model"))
        self.assertIn("fresh.config.use_cache = False", text)
        self.assertLess(text.index("fresh.config.use_cache = False"), text.index("actual = fresh"))

    def test_qwen35_uses_multimodal_loader_for_text_only_checkpoint(self):
        self.assertEqual(model_loader_name("qwen3_5"), "AutoModelForImageTextToText")
        self.assertEqual(model_loader_name("qwen3_5_moe"), "AutoModelForImageTextToText")
        self.assertEqual(model_loader_name("qwen3"), "AutoModelForCausalLM")

    def test_smoke_limit_is_deterministic_and_source_only(self):
        data = {"groups": [{"id": str(i), "split": "train" if i % 2 else "dev"} for i in range(8)]}
        self.assertEqual([g["id"] for g in select_groups(data, "train", 0, 1, 2)], ["1", "3"])
        with self.assertRaises(ValueError):
            select_groups({**data, "role": "external_test_only"}, "external_test_only", 0, 1, 2)

    def test_streaming_hash_matches_file_identity(self):
        import hashlib
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "weight"
            p.write_bytes(b"local read-only weight")
            self.assertEqual(file_sha256(p), hashlib.sha256(p.read_bytes()).hexdigest())

    def test_setr_prompt_contains_only_question_and_numbered_text(self):
        prompt = messages("Which result?", [{"id": "secret-id", "text": "[2] result"}], "iri")
        self.assertIn("[1] (2) result", prompt[1]["content"])
        self.assertNotIn("secret-id", prompt[1]["content"])

    def test_iri_preserves_official_spacing_and_final_instruction(self):
        prompt = messages("Q", [{"text": "A"}], "iri")[1]["content"]
        self.assertIn("\n\n\nSearch Query: Q\n\n\nPlease follow", prompt)
        self.assertTrue(prompt.endswith("e.g., ### Final Selection: [4] [2]."))

    def test_parser_is_fail_closed_and_does_not_pad(self):
        self.assertEqual(parse_selection("### Final Selection: [2] [1]", 2), [1, 0])
        self.assertEqual(parse_selection("### Final Selection: []", 2), [])
        for text in ("[1]", "### Final Selection: [1] [1]", "### Final Selection: [3]"):
            with self.assertRaises(ValueError):
                parse_selection(text, 2)

    def test_ic_counts_distinct_literal_gold_facts_once(self):
        candidates = [{"text": "Fact A and Fact B"}, {"text": "Fact A"}, {"text": "noise"}]
        gold = [{"fact": "Fact A"}, {"fact": "Fact B"}]
        facts, matches = evidence_sets(candidates, gold)
        self.assertEqual(facts, ["FactA", "FactB"])
        self.assertEqual(matches, [{0, 1}, {0}, set()])
        result = metrics(candidates, gold, [0, 1, 2])
        self.assertEqual(result["ic"], 1.0)
        self.assertEqual(result["redundant_gold_passages"], 1)
        self.assertEqual(result["ic_at_1"], 1.0)

    def test_failed_null_is_not_successful_abstention(self):
        row = {**metrics([{"text": "noise"}], [], []), "status": "invalid_output"}
        self.assertEqual(summarize([row])["null_empty_selection_rate"], 0)
        row["status"] = "ok"
        self.assertEqual(summarize([row])["null_empty_selection_rate"], 1)
        self.assertIsNone(summarize([row])["ic_at_6"])

    def test_missing_gold_stays_in_denominator(self):
        result = metrics([{"text": "A"}], [{"fact": "A"}, {"fact": "B"}], [0])
        self.assertEqual(result["candidate_ic"], 0.5)
        self.assertEqual(result["ic_at_6"], 0.5)
        self.assertEqual(result["available_ic"], 1)

    def test_training_source_deduplicates_queries_and_isolates_passages(self):
        source = [{"id": str(i), "query": f"q{i // 2}",
                   "passages": [f"text {i // 2} {j}" for j in range(20)],
                   "conversations": [{"from": "gpt", "value": "DO NOT TRAIN ON OLD RESPONSE"}]}
                  for i in range(20)]
        data = prepare(source, train_limit=6, dev_limit=2)
        self.assertEqual(data["unique_top20_queries"], 10)
        self.assertEqual(data["split_counts"], {"dev": 2, "train": 6})
        self.assertNotIn("DO NOT TRAIN", json.dumps(data))
        self.assertEqual(len({g["id"] for g in data["groups"]}), 8)
        source[4]["passages"] = source[0]["passages"]
        source[5]["passages"] = source[0]["passages"]
        data = prepare(source, train_limit=6, dev_limit=2)
        passage_splits = {}
        for group in data["groups"]:
            for c in group["candidates"]:
                self.assertEqual(passage_splits.setdefault(c["text"], group["split"]), group["split"])

    def test_only_valid_teacher_selection_is_training_target(self):
        groups = [{"id": str(i), "split": "train", "question": "Q", "candidates": [{"text": "A"}]} for i in range(2)]
        pred = {"0": {"status": "ok", "selected_indices": [0]},
                "1": {"status": "output_limit", "selected_indices": []}}
        rows = training_examples(groups, pred)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["target"], "### Final Selection: [1]")

    def test_predictions_require_all_frozen_inputs(self):
        group = {"id": "q", "question": "Q", "candidates": [{"text": "A"}]}
        row = {"id": "q", "input_sha256": input_fingerprint(group, "selection"), "mode": "selection",
               "status": "ok", "output": "### Final Selection: [1]", "selected_indices": [0]}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pred.jsonl"
            path.write_text(json.dumps(row) + "\n", encoding="utf-8")
            self.assertEqual(read_predictions([path], [group])["q"]["selected_indices"], [0])
            row["input_sha256"] = "wrong"
            path.write_text(json.dumps(row) + "\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                read_predictions([path], [group])

    def test_same_pool_evaluation_and_null_denominator(self):
        data = {"protocol": PROTOCOL, "role": "external_test_only", "groups": [
            {"id": "a", "question": "Q", "question_type": "inference_query", "gold": [{"fact": "A"}],
             "candidates": [{"id": "p", "text": "A"}]},
            {"id": "b", "question": "N", "question_type": "null_query", "gold": [],
             "candidates": [{"id": "p", "text": "A"}]}]}
        result = evaluate(data)
        self.assertEqual(result["summary"]["bm25_top6"]["answerable"], 1)
        self.assertEqual(result["summary"]["bm25_top6"]["ic_at_6"], 1)
        self.assertEqual(result["summary"]["bm25_top6"]["null_questions"], 1)


if __name__ == "__main__":
    unittest.main()
