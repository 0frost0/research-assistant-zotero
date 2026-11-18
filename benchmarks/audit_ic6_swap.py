"""Gold-only single-swap opportunity audit; never emits a replacement decision."""
import argparse
import json
from pathlib import Path
from statistics import mean
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmarks.run_ic6_retrieval import load, sha, write_new
from benchmarks.run_ic6_swap import validate
from benchmarks.setr_protocol import PROTOCOL, evidence_sets


def opportunity(matches, n):
    """Enumerate legal zero/one-swap sets, report counts only, no chosen IDs."""
    if n < 0 or len(matches) < 6 or any(not m <= set(range(n)) for m in matches):
        raise ValueError("Invalid fact sets")
    original = set().union(*matches[:6])
    best = len(original)
    preserving_gain = False
    for drop in range(6):
        retained = set().union(*(matches[i] for i in range(6) if i != drop))
        for challenger in matches[6:]:
            covered = retained | challenger
            best = max(best, len(covered))
            preserving_gain |= original < covered
    return {"gold_count": n, "baseline_covered": len(original), "best_covered_diagnostic_only": best,
            "baseline_ic": len(original)/n if n else None, "oracle_single_swap_ic_diagnostic_only": best/n if n else None,
            "improvement_opportunity": bool(n and best > len(original)),
            "gain_without_losing_old_gold_opportunity": bool(n and preserving_gain)}


def audit(a):
    data, gold, report = map(load, (a.inputs, a.gold, a.report))
    validate(data)
    if (gold['protocol'] != PROTOCOL or gold['role'] != 'external_test_only' or
            report['fingerprints']['inputs'] != sha(a.inputs) or report['fingerprints']['gold'] != sha(a.gold)):
        raise ValueError("Identity/protocol mismatch")
    labels = {g['id']:g for g in gold['groups']}
    if len(labels) != len(gold['groups']):
        raise ValueError("Duplicate labels")
    lookup = {c['id']:c for c in data['chunks']}
    previous = {r['id']:r for r in report['rows'] if r['arm']=='coverage_anchors'}
    if len(previous)!=128 or set(previous)!={g['id'] for g in data['groups']}:
        raise ValueError("Partial pilot report")
    rows=[]
    for g in data['groups']:
        label = labels[g['id']]
        if label['question']!=g['question']:
            raise ValueError("Question changed")
        facts, matches = evidence_sets([lookup[i] for i in g['anchors']+g['challengers']], label['gold'])
        if (label['question_type']=='null_query') != (not facts):
            raise ValueError("Null mismatch")
        counts=opportunity(matches,len(facts)) if g['status']=='ok' else {
            'gold_count':len(facts),'baseline_covered':0,'best_covered_diagnostic_only':0,
            'baseline_ic':0 if facts else None,'oracle_single_swap_ic_diagnostic_only':0 if facts else None,
            'improvement_opportunity':False,'gain_without_losing_old_gold_opportunity':False}
        if previous[g['id']]['covered_gold_count']!=counts['baseline_covered']:
            raise ValueError("Old coverage changed")
        rows.append({'id':g['id'],'question_type':label['question_type'],**counts})
    real=[r for r in rows if r['gold_count']]
    summary={'questions':len(rows),'answerable':len(real),'null_questions':len(rows)-len(real),
             'baseline_ic':mean(r['baseline_ic'] for r in real),
             'oracle_single_swap_ic_diagnostic_only':mean(r['oracle_single_swap_ic_diagnostic_only'] for r in real),
             'improvable_questions':sum(r['improvement_opportunity'] for r in real),
             'improvable_without_losing_old_gold_questions':sum(r['gain_without_losing_old_gold_opportunity'] for r in real)}
    output={'version':'ic6-swap-opportunity-audit-v1','role':'diagnostic_only_never_selection_or_training',
            'summary':summary,'rows':rows,'fingerprints':{'inputs':sha(a.inputs),'gold':sha(a.gold),'report':sha(a.report),'script':sha(__file__)},
            'notes':['Oracle is gold-only diagnosis, never a selector or a new achieved score.',
                     'Preserves annotated old facts only, not a semantic guarantee about all useful facts.',
                     'No selected challenger/drop IDs, gold spans, original questions or passage text are emitted.']}
    write_new(a.output,output)
    print(json.dumps(summary,indent=2),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for flag in ('inputs','gold','report','output'):
        p.add_argument('--'+flag,required=True,type=Path)
    audit(p.parse_args())
