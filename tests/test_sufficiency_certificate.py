import json
from pathlib import Path
import tempfile
import unittest

from benchmarks.sufficiency_certificate import decide, grounded, infer, parse_certificate, stage_messages
from benchmarks import sufficiency_certificate as protocol
from benchmarks.run_public_sufficiency import VERSION, digest, file_sha256, write_new
from benchmarks.score_public_sufficiency_pilot import verify_run


class CertificateTests(unittest.TestCase):
    def setUp(self):
        self.item = {"id": "opaque", "question": "Where does A work?", "documents": [
            {"title": "Not body evidence", "text": "A works at B. B is in C."}]}
        self.certificate = {"answer": "B", "steps": [
            {"claim": "A works at B", "document": 1, "quote": "A works at B."}]}

    def generated(self, value):
        return {"status": "generated", "raw_output": json.dumps(value),
                "seconds": .1, "input_tokens": 100, "output_tokens": 20}

    def test_exact_quote_does_not_override_relation_verifier(self):
        stages = [{"name": "propose", **self.generated(self.certificate)},
                  {"name": "verify", **self.generated({"supported": [False], "complete": True})}]
        self.assertEqual(decide(self.item, stages), ("ok", False, "relation_or_coverage_rejected", True))

    def test_missing_quote_blocks_verifier_and_title_is_not_body(self):
        for quote in ("A works at C.", "Not body evidence"):
            self.certificate["steps"][0]["quote"] = quote
            calls = []
            def generate(messages, limit):
                calls.append(messages)
                return self.generated(self.certificate)
            result = infer(self.item, generate)
            self.assertEqual(len(calls), 1)
            self.assertEqual(result["reason"], "quote_not_found")
            self.assertIs(result["prediction"], False)

    def test_schema_errors_are_failures_not_correct_rejections(self):
        for raw in ('{"answer":null,"answer":"B","steps":[]}',
                    '{"answer":"B","steps":[]}', '{"answer":null,"steps":[{}]}'):
            with self.assertRaises(ValueError):
                parse_certificate(raw)
            stages = [{"name": "propose", "status": "generated", "raw_output": raw}]
            self.assertEqual(decide(self.item, stages)[:2], ("invalid_output", None))

    def test_verifier_length_and_bool_are_strict(self):
        for value in ({"supported": [], "complete": True},
                      {"supported": ["true"], "complete": True},
                      {"supported": [True], "complete": 1}):
            stages = [{"name": "propose", **self.generated(self.certificate)},
                      {"name": "verify", **self.generated(value)}]
            self.assertEqual(decide(self.item, stages)[:2], ("invalid_output", None))

    def test_two_fresh_calls_preserve_full_context(self):
        calls = []
        def generate(messages, limit):
            calls.append(messages)
            return self.generated(self.certificate if len(calls) == 1 else
                                  {"supported": [True], "complete": True})
        result = infer(self.item, generate)
        self.assertIs(result["prediction"], True)
        self.assertEqual(len(calls), 2)
        for messages in calls:
            self.assertEqual([m["role"] for m in messages], ["system", "user"])
            self.assertIn(self.item["documents"][0]["text"], messages[1]["content"])
            self.assertNotIn("opaque", json.dumps(messages))

    def test_out_of_range_and_boolean_document_ids(self):
        self.certificate["steps"][0]["document"] = 2
        self.assertFalse(grounded(self.certificate, self.item))
        self.certificate["steps"][0]["document"] = True
        with self.assertRaises(ValueError):
            parse_certificate(json.dumps(self.certificate))

    def test_generation_limits_are_failures(self):
        stages = [{"name": "propose", "status": "output_limit", "raw_output": ""}]
        self.assertEqual(decide(self.item, stages)[:2], ("output_limit", None))
        stages = [{"name": "propose", **self.generated(self.certificate)},
                  {"name": "verify", "status": "oom", "raw_output": ""}]
        self.assertEqual(decide(self.item, stages)[:2], ("oom", None))
        with self.assertRaisesRegex(ValueError, "Missing verifier"):
            decide(self.item, stages[:1])

    def test_receipt_replay_rejects_forged_decision_and_counters(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            input_path = root / "inputs.json"
            write_new(input_path, [self.item])
            manifest = {"version": VERSION, "modes": ["certificate"],
                        "prompts": {"certificate": protocol.PROPOSER},
                        "inputs_sha256": file_sha256(input_path), "script_sha256": "test",
                        "model_files": {"test": "test"}, "no_truncation": True,
                        "max_input_tokens": 16000, "certificate_protocol": {
                            "sha256": file_sha256(protocol.__file__), "limits": protocol.LIMITS,
                            "proposer": protocol.PROPOSER, "verifier": protocol.VERIFIER}}
            write_new(root / "manifest.json", manifest)
            responses = iter([self.certificate, {"supported": [False], "complete": True}])
            result = infer(self.item, lambda *_: self.generated(next(responses)))
            row = {"id": self.item["id"], "input_sha256": digest(self.item),
                   "run_id": digest(manifest), "mode": "certificate", **result}
            def save():
                (root / "predictions.jsonl").write_text(json.dumps(row) + "\n", encoding="utf-8")
                (root / "complete.json").write_text(json.dumps({
                    "rows": 1, "run_id": digest(manifest),
                    "predictions_sha256": file_sha256(root / "predictions.jsonl")}), encoding="utf-8")
            save()
            self.assertIs(verify_run(root, input_path, [self.item])[1]["certificate"][self.item["id"]]["prediction"], False)
            row["prediction"] = True
            save()
            with self.assertRaisesRegex(ValueError, "replay mismatch"):
                verify_run(root, input_path, [self.item])
            row["prediction"] = False
            row["output_tokens"] = 0
            save()
            with self.assertRaisesRegex(ValueError, "aggregate mismatch"):
                verify_run(root, input_path, [self.item])


if __name__ == "__main__":
    unittest.main()
