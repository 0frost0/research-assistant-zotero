import copy
import json
from argparse import Namespace
from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest

from benchmarks.run_ic6_joint import (
    OUTPUT_SCHEMA, SHORTLIST_SEED_VERSION, VERSION, evaluate, groups_for, parse_joint, parse_selection, prompt, shortlist, validate_input, validate_prediction,
)
from benchmarks.run_ic6_retrieval import sha, write_new
from benchmarks.setr_protocol import PROTOCOL, digest


class JointTests(unittest.TestCase):
    def data(self):
        chunks = [{"id": str(i), "text": "source\n\nfact", "source_id": "doc", "word_start": 0, "word_end": 1} for i in range(100)]
        group = {"id": "q", "question": "Original question", "needs": ["First needed fact", "Second needed fact"],
                 "status": "ok", "candidate_ids": [str(i) for i in range(32)],
                 "baseline_selected_ids": [str(i) for i in range(6)]}
        return {"version": VERSION, "groups": [group], "chunks": chunks,
                "document_tokens": {str(i): 5 for i in range(100)}, "fingerprints": {}}

    def test_strict_parse_budget_and_support(self):
        self.assertEqual(parse_joint('{"support":[[1],[2]],"selection":[2,1]}', 32, 2)["selection"], [2, 1])
        self.assertEqual(parse_joint('{"support":[[],[]],"selection":[]}', 32, 2)["selection"], [])
        for obj in [
            {"support": [[1], [2]], "selection": [True]},
            {"support": [[1], [2]], "selection": [3]},
            {"support": [[1], [2]], "selection": [1, 1]},
            {"support": [[1], [33]], "selection": [33]},
            {"support": [[1]], "selection": [1]},
            {"support": [[1], [2]], "selection": [1], "extra": 0},
            {"support": [list(range(1, 10)), []], "selection": []},
            {"support": [list(range(1, 8)), []], "selection": list(range(1, 8))},
        ]:
            with self.assertRaises(ValueError):
                parse_joint(json.dumps(obj), 32, 2)
        with self.assertRaises(ValueError):
            parse_joint('```json\n{"support":[[],[]],"selection":[]}\n```', 32, 2)

    def test_shortlist_preserves_anchors_and_need_tops_without_labels(self):
        ids = [str(i) for i in range(100)]
        base = [i/100 for i in range(100)]
        needs = [[(100-i)/100 for i in range(100)], [(i%20)/20 for i in range(100)]]
        anchors = ids[40:46]
        pool = shortlist(ids, base, needs, anchors, "q")
        self.assertEqual(len(pool), 32)
        self.assertEqual(len(set(pool)), 32)
        self.assertTrue(set(anchors + ids[:6]) <= set(pool))
        self.assertEqual(pool, shortlist(ids, base, needs, anchors, "q"))
        self.assertNotEqual(pool, shortlist(ids, base, needs, anchors, "another"))
        self.assertEqual(SHORTLIST_SEED_VERSION, "ic6-joint-8b-v1")
        with self.assertRaises(ValueError):
            shortlist(ids, base, needs, ids[:5], "q")

    def test_selection_only_strict_contract_is_not_support_mapping_repair(self):
        self.assertEqual(parse_selection('{"selection":[6,1]}', 32)["selection"], [6, 1])
        self.assertEqual(parse_selection('{"selection":[]}', 32)["selection"], [])
        for raw in ['{"selection":[true]}', '{"selection":[1.0]}', '{"selection":[0]}',
                    '{"selection":[33]}', '{"selection":[1,1]}', '{"selection":[1,2,3,4,5,6,7]}',
                    '{"selection":"1"}', '{"selection":[],"support":[]}',
                    '{"selection":[1],"selection":[2]}', '[1,2]', '```json\n{"selection":[]}\n```']:
            with self.assertRaises(ValueError):
                parse_selection(raw, 32)
        # v2's unsupported selection remains invalid under its original contract.
        with self.assertRaises(ValueError):
            parse_joint('{"support":[[1],[2]],"selection":[12]}', 32, 2)

    def test_gold_answer_type_fields_rejected(self):
        for key in ("gold", "answer", "question_type", "fact"):
            data = self.data()
            data["groups"][0][key] = "forbidden"
            with self.assertRaises(ValueError):
                validate_input(data)
            data = self.data()
            data["chunks"][0][key] = "forbidden"
            with self.assertRaises(ValueError):
                validate_input(data)
        validate_input(self.data())

    def test_prompt_never_emits_upstream_scores_or_selected_ids(self):
        data = self.data()
        g = copy.deepcopy(data["groups"][0])
        g["gold"] = "SECRET GOLD"
        g["scores"] = "SECRET SCORE"
        messages = str(prompt(g, {c["id"]: c for c in data["chunks"]}))
        self.assertNotIn("SECRET", messages)
        self.assertIn("Original question", messages)
        self.assertIn("PASSAGE 32", messages)
        self.assertIn("at most SIX numbers TOTAL", messages)
        self.assertIn('Return only {"selection":[...]}', messages)

    def test_shards_disjoint_complete_and_smoke_is_bounded(self):
        data = {"groups": [{"id": str(i)} for i in range(2556)]}
        a, b = (groups_for(data, i, 2, 0) for i in range(2))
        self.assertEqual(len(a) + len(b), 2556)
        self.assertFalse({g["id"] for g in a} & {g["id"] for g in b})
        self.assertEqual(len(groups_for(data, 0, 1, 3)), 3)
        with self.assertRaises(ValueError):
            groups_for(data, 2, 2, 0)

    def test_cache_output_maps_exactly_no_repair(self):
        g = self.data()["groups"][0]
        row = {"input_sha256": digest(g), "candidate_ids": g["candidate_ids"], "status": "ok",
               "raw_output": '{"selection":[2,1]}', "selected_ids": ["1", "0"]}
        validate_prediction(row, g)
        row["selected_ids"] = ["0", "1"]
        with self.assertRaises(ValueError):
            validate_prediction(row, g)
        row["status"] = "invalid"
        with self.assertRaises(ValueError):
            validate_prediction(row, g)

    def test_complete_evaluation_counts_failure_and_null_without_dropping(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = self.data()
            first = data["groups"][0]
            second, third = copy.deepcopy(first), copy.deepcopy(first)
            second.update(id="failed", status="invalid", needs=[], candidate_ids=[], baseline_selected_ids=[])
            third.update(id="null", question="Unanswerable question")
            data["groups"] += [second, third]
            write_new(root / "inputs.json", data)
            gold = {"protocol": PROTOCOL, "role": "external_test_only", "groups": [
                {"id": g["id"], "question": g["question"], "question_type": "null_query" if g["id"] == "null" else "inference_query",
                 "gold": [] if g["id"] == "null" else [{"fact": "fact"}]} for g in data["groups"]]}
            write_new(root / "gold.json", gold)
            directories = []
            for shard in range(2):
                directory = root / f"shard{shard}"
                directory.mkdir()
                directories.append(directory)
                write_new(directory / "manifest.json", {"version": VERSION, "output_schema": OUTPUT_SCHEMA,
                          "inputs": sha(root / "inputs.json"), "limit": 0,
                          "shard": shard, "shards": 2})
                with (directory / "predictions.jsonl").open("w", encoding="utf-8") as f:
                    for g in groups_for(data, shard, 2, 0):
                        ok = g["status"] == "ok"
                        raw = '{"selection":[1,2]}' if g["id"] == "q" else '{"selection":[]}'
                        f.write(json.dumps({"id": g["id"], "input_sha256": digest(g), "candidate_ids": g["candidate_ids"],
                            "status": "ok" if ok else "upstream_invalid", "raw_output": raw if ok else "",
                            "selected_ids": ["0", "1"] if g["id"] == "q" else [],
                            "seconds": 1, "input_tokens": 100, "output_tokens": 20}) + "\n")
                write_new(directory / "complete.json", {"predictions_sha256": sha(directory / "predictions.jsonl")})
            args = Namespace(inputs=root / "inputs.json", gold=root / "gold.json", prediction_dir=directories,
                             output=root / "results.json")
            with redirect_stdout(io.StringIO()):
                evaluate(args)
            report = json.loads(args.output.read_text())
            joint = report["summary"]["joint_8b"]
            self.assertEqual(joint["questions"], 3)
            self.assertEqual(joint["answerable"], 2)
            self.assertEqual(joint["invalid_outputs"], 1)
            self.assertEqual(joint["null_questions"], 1)
            self.assertEqual(joint["null_empty_selection_rate"], 1)
            self.assertEqual(joint["ic_at_6"], .5)
            self.assertEqual(report["summary"]["coverage_anchors"]["ic_at_6"], .5)
            manifest_path = directories[1] / "manifest.json"
            manifest = json.loads(manifest_path.read_text())
            manifest["output_schema"] = "support_mapping"
            manifest_path.write_text(json.dumps(manifest))
            args.output = root / "wrong-schema.json"
            with self.assertRaises(ValueError):
                evaluate(args)
            self.assertFalse(args.output.exists())
            manifest["output_schema"] = OUTPUT_SCHEMA
            manifest_path.write_text(json.dumps(manifest))
            # A manifest with changed contract must not get silently merged.
            manifest = json.loads((directories[1] / "manifest.json").read_text())
            manifest["limit"] = 3
            (directories[1] / "manifest.json").write_text(json.dumps(manifest))
            args.output = root / "bad.json"
            with self.assertRaises(ValueError):
                evaluate(args)
            self.assertFalse(args.output.exists())
