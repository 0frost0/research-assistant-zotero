import json
from pathlib import Path
import tempfile
import unittest

from benchmarks.run_public_sufficiency import public_input, validate_input, parse_output, score, compare, messages
from benchmarks.run_public_sufficiency import VERSION, digest, file_sha256, write_new
from benchmarks.score_public_sufficiency_pilot import verify_run


def test_supervision_removed_from_prompt():
    source = {"question": "Q", "answer": "SECRET_ANSWER", "answerable": True,
              "question_decomposition": ["SECRET_STEP"],
              "paragraphs": [{"title": "T", "paragraph_text": "Evidence", "is_supporting": True, "idx": 7}]}
    item = {"id": "opaque", **public_input(source)}
    serialized = json.dumps(messages(item, "chain"))
    assert "SECRET" not in serialized and "is_supporting" not in serialized and "opaque" not in serialized
    with unittest.TestCase().assertRaises(ValueError):
        validate_input({**item, "answerable": True})
    item["documents"][0]["is_supporting"] = True
    with unittest.TestCase().assertRaises(ValueError):
        validate_input(item)


def test_no_truthy_or_duplicate_labels():
    for text in ['{"sufficient":"false"}', '{"sufficient":0}',
                 '{"sufficient":true,"sufficient":false}',
                 '{"sufficient":true,"extra":1}', 'true']:
        with unittest.TestCase().assertRaises(ValueError):
            parse_output(text, "direct")


def test_chain_uses_only_strict_final_line():
    assert parse_output('An earlier hypothesis was {"sufficient":true}\n{"sufficient": false}', "chain") is False
    assert parse_output('Audit text\n{"sufficient": true}', "chain_strict") is True
    assert parse_output('Audit text\n{"sufficient": false}', "chain_guard") is False
    with unittest.TestCase().assertRaises(ValueError):
        parse_output('{"sufficient":false}\nActually maybe yes.', "chain")


def test_failures_do_not_earn_correct_rejections():
    gold = [{"id": "p", "group": "q", "sufficient": True}, {"id": "n", "group": "q", "sufficient": False}]
    all_bad = {"p": {"status": "output_limit", "prediction": None},
               "n": {"status": "invalid_output", "prediction": None}}
    metrics, _ = score(gold, all_bad)
    assert metrics["balanced_accuracy"] == 0
    assert metrics["insufficient_failure_rate"] == 1
    assert metrics["invalid_rate"] == 1
    assert metrics["observed_false_pass_rate"] == 0
    assert score(gold, {})[0]["balanced_accuracy"] == 0


def test_all_refuse_cannot_claim_improvement():
    gold = [{"id": f"{i}-{v}", "group": str(i), "sufficient": v} for i in range(8) for v in [True, False]]
    baseline = {r["id"]: {"status": "ok", "prediction": True} for r in gold}
    candidate = {r["id"]: {"status": "ok", "prediction": False} for r in gold}
    assert score(gold, candidate)[0]["balanced_accuracy"] == .5
    assert not compare(gold, baseline, candidate)["meaningful_gain_passed"]


def test_paired_gain_and_foreign_rows():
    gold = [{"id": f"{i}-{v}", "group": str(i), "sufficient": v} for i in range(8) for v in [True, False]]
    baseline = {r["id"]: {"status": "ok", "prediction": True} for r in gold}
    candidate = {r["id"]: {"status": "ok", "prediction": r["sufficient"]} for r in gold}
    result = compare(gold, baseline, candidate)
    assert result["meaningful_gain_passed"]
    assert result["paired_bootstrap_95_accuracy_delta"] == [.5, .5]
    with unittest.TestCase().assertRaises(ValueError):
        score(gold, {**candidate, "foreign": {"status": "ok", "prediction": True}})


def receipt_fixture(root):
    inputs = [{"id": "input-a", "question": "Q", "documents": []},
              {"id": "input-b", "question": "Q2", "documents": []}]
    input_path = root / "inputs.json"
    write_new(input_path, inputs)
    manifest = {"version": VERSION, "modes": ["direct"], "prompts": {"direct": "test"},
                "inputs_sha256": file_sha256(input_path), "model_files": {"test": "hash"},
                "script_sha256": "hash", "no_truncation": True, "max_input_tokens": 16000}
    write_new(root / "manifest.json", manifest)
    rows = [{"id": item["id"], "mode": "direct", "run_id": digest(manifest),
             "input_sha256": digest(item), "status": "ok", "prediction": False,
             "raw_output": '{"sufficient": false}', "input_tokens": 10}
            for item in inputs]
    return inputs, input_path, rows, manifest


def write_receipt(root, rows, manifest):
    (root / "predictions.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    (root / "complete.json").write_text(json.dumps({
        "run_id": digest(manifest), "rows": len(rows),
        "predictions_sha256": file_sha256(root / "predictions.jsonl"),
    }), encoding="utf-8")


def test_pilot_rejects_modified_prediction_receipt():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        inputs, path, rows, manifest = receipt_fixture(root)
        write_receipt(root, rows, manifest)
        assert len(verify_run(root, path, inputs)[1]["direct"]) == 2
        prediction_path = root / "predictions.jsonl"
        prediction_path.write_text(prediction_path.read_text(encoding="utf-8").replace(
            '"id":', '"id" :'), encoding="utf-8")  # Same JSON, different bytes.
        with unittest.TestCase().assertRaisesRegex(ValueError, "receipt"):
            verify_run(root, path, inputs)


def test_pilot_rejects_duplicate_rows_even_with_matching_hash():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        inputs, path, rows, manifest = receipt_fixture(root)
        write_receipt(root, [rows[0], rows[0]], manifest)
        with unittest.TestCase().assertRaisesRegex(ValueError, "Duplicate"):
            verify_run(root, path, inputs)


def test_pilot_rejects_repaired_or_misparsed_prediction():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        inputs, path, rows, manifest = receipt_fixture(root)
        rows[0]["prediction"] = True  # Conflicts with the raw response.
        write_receipt(root, rows, manifest)
        with unittest.TestCase().assertRaisesRegex(ValueError, "successful prediction"):
            verify_run(root, path, inputs)
        rows[0]["status"] = "invalid_output"
        write_receipt(root, rows, manifest)
        with unittest.TestCase().assertRaisesRegex(ValueError, "silently repaired"):
            verify_run(root, path, inputs)


def load_tests(loader, tests, pattern):
    return unittest.TestSuite(unittest.FunctionTestCase(value) for name, value in globals().items()
                              if name.startswith("test_") and callable(value))
