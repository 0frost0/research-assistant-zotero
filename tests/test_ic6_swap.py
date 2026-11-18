import copy
import json
from argparse import Namespace
from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest

from benchmarks.run_ic6_swap import VERSION, apply_swap, evaluate, parse_swap, pick_challengers, pilot_ids, prompt, validate_row
from benchmarks.run_ic6_retrieval import sha, write_new
from benchmarks.setr_protocol import PROTOCOL, digest


class SwapTests(unittest.TestCase):
    def test_strict_swap_and_explicit_noop(self):
        self.assertEqual(parse_swap('{"drop":0,"add":0}', 4), {"drop":0,"add":0})
        for raw in ['{"drop":0,"add":1}', '{"drop":1,"add":0}', '{"drop":7,"add":1}',
                    '{"drop":1,"add":5}', '{"drop":true,"add":1}', '{"drop":1.0,"add":1}',
                    '{"drop":0,"add":0,"x":1}', '{"drop":0,"drop":1,"add":1}',
                    '```json\n{"drop":0,"add":0}\n```']:
            with self.assertRaises(ValueError):
                parse_swap(raw, 4)

    def test_preserves_five_and_six_budget_no_padding(self):
        anchors, challengers = list('abcdef'), list('ghi')
        self.assertEqual(apply_swap(anchors, challengers, {"drop":0,"add":0}), anchors)
        result = apply_swap(anchors, challengers, {"drop":2,"add":3})
        self.assertEqual(result, list('aicdef'))
        self.assertEqual(len(set(result) & set(anchors)), 5)
        with self.assertRaises(ValueError):
            apply_swap(anchors, ['a'], {"drop":0,"add":0})

    def test_fixed_sampling_no_labels_or_input_order_dependence(self):
        groups = [{"id": str(i)} for i in range(2556)]
        ids = pilot_ids(groups)
        self.assertEqual(len(set(ids)), 128)
        self.assertEqual(ids, pilot_ids(list(reversed(groups))))
        self.assertEqual(ids, pilot_ids([{**g,"gold":"ignored","question_type":"ignored"} for g in groups]))

    def test_challengers_fixed_top_two_per_need_not_gold(self):
        pool = [str(i) for i in range(100)]
        values = [i/100 for i in range(100)]
        chosen = pick_challengers(pool, [values, values[::-1]], pool[:6], pool[:32])
        self.assertEqual(chosen, ['31','6','30','7'])
        self.assertFalse(set(chosen) & set(pool[:6]))

    def test_prompt_whitelist_and_failed_outputs_not_anchor_fallback(self):
        g = {"id":"q", "question":"q", "needs":["need1", "need2"], "status":"ok",
             "anchors":list('abcdef'), "challengers":['g']}
        lookup = {i:{"text":"untrusted text"} for i in 'abcdefg'}
        extra = copy.deepcopy(g)
        extra['gold'] = "SECRET GOLD"
        self.assertNotIn("SECRET", str(prompt(extra, lookup)))
        row = {"input_sha256":digest(g), "status":"ok", "raw_output":'{"drop":0,"add":0}', "selected_ids":list('abcdef')}
        validate_row(row,g)
        row['status']='invalid'
        with self.assertRaises(ValueError):
            validate_row(row,g)
        row['selected_ids']=[]
        validate_row(row,g)

    def test_complete_pilot_evaluation_failures_null_and_partial_rejection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chunks = [{"id":str(i),"source_id":"doc","text":"factA" if i==0 else "factB" if i==6 else "other",
                       "word_start":0,"word_end":1} for i in range(8)]
            groups = [{"id":str(i),"question":"question","needs":["first need", "second need"],"status":"ok",
                       "anchors":[str(j) for j in range(6)],"challengers":["6","7"]} for i in range(128)]
            groups[2].update(status="invalid",anchors=[],challengers=[])
            write_new(root/'inputs.json', {"version":VERSION,"groups":groups,"chunks":chunks})
            write_new(root/'gold.json', {"protocol":PROTOCOL,"role":"external_test_only", "groups":[
                {"id":g['id'],"question":g['question'],"question_type":"null_query" if i==127 else "inference_query",
                 "gold":[] if i==127 else [{"fact":"factA"},{"fact":"factB"}]} for i,g in enumerate(groups)]})
            out = root/'predictions'
            out.mkdir()
            rows=[]
            for i,g in enumerate(groups):
                raw='{"drop":2,"add":1}' if i==0 else '{"drop":0,"add":0}'
                rows.append({"id":g['id'],"input_sha256":digest(g),"status":"invalid" if i==1 else "upstream_invalid" if i==2 else "ok",
                             "raw_output":raw if i!=2 else "","selected_ids":[] if i in (1,2) else apply_swap(g['anchors'],g['challengers'],parse_swap(raw,2)),
                             "input_tokens":100,"output_tokens":10,"seconds":1})
            path=out/'predictions.jsonl'
            path.write_text(''.join(json.dumps(r)+'\n' for r in rows),encoding='utf-8')
            write_new(out/'complete.json',{"predictions_sha256":sha(path),"questions":128})
            write_new(out/'manifest.json',{"inputs":sha(root/'inputs.json')})
            args=Namespace(inputs=root/'inputs.json',gold=root/'gold.json',predictions=out,output=root/'results.json')
            with redirect_stdout(io.StringIO()):
                evaluate(args)
            report=json.loads(args.output.read_text())
            self.assertEqual(report['summary']['single_swap']['questions'],128)
            self.assertEqual(report['summary']['single_swap']['answerable'],127)
            self.assertEqual(report['summary']['single_swap']['invalid_outputs'],2)
            self.assertEqual(report['summary']['single_swap']['null_questions'],1)
            self.assertEqual(report['win_tie_loss'],[1,125,1])
            self.assertAlmostEqual(report['delta_ic6'],0)
            rows.pop()
            path.write_text(''.join(json.dumps(r)+'\n' for r in rows),encoding='utf-8')
            (out/'complete.json').write_text(json.dumps({"predictions_sha256":sha(path),"questions":128}))
            args.output=root/'bad.json'
            with self.assertRaises(ValueError):
                evaluate(args)
            self.assertFalse(args.output.exists())
