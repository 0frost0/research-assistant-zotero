"""Finalize copied MuSiQue runs on CPU; no SSH, generation or label creation.

Both splits must be complete and use identical frozen inference settings.
Reports contain IDs/counts only, not questions, documents or generated audits.
"""
import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import statistics
import sys
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from benchmarks.run_public_sufficiency import (
    SOURCE_SHA, VERSION, digest, evaluate, file_sha256, load, parse_output,
    validate_input, write_new,
)


def verify_split(data, split, run):
    frozen = load(data / 'manifest.json')
    if frozen['version'] != VERSION or frozen['source_sha256'] != SOURCE_SHA:
        raise ValueError('Unexpected frozen data version')
    for kind in ('inputs', 'gold'):
        name = f'{split}.{kind}.json'
        if file_sha256(data / name) != frozen['files'][name]:
            raise ValueError('Frozen file changed: ' + name)
    inputs = load(data / f'{split}.inputs.json')
    gold = load(data / f'{split}.gold.json')
    expected_pairs = {'development': 32, 'heldout': 128}[split]
    if len(inputs) != 2 * expected_pairs or len(gold) != len(inputs):
        raise ValueError('Unexpected split size')
    lookup = {}
    for item in inputs:
        validate_input(item)
        if item['id'] in lookup or item['id'] != digest(
                {k: v for k, v in item.items() if k != 'id'}):
            raise ValueError('Duplicate or non-content-addressed input')
        lookup[item['id']] = item
    groups = defaultdict(list)
    if len({g['id'] for g in gold}) != len(gold) or {g['id'] for g in gold} != set(lookup):
        raise ValueError('Gold/input identity mismatch')
    for g in gold:
        if type(g['sufficient']) is not bool:
            raise ValueError('Non-boolean public label')
        groups[g['group']].append(g)
    if len(groups) != expected_pairs:
        raise ValueError('Unexpected question count')
    for pair in groups.values():
        if len(pair) != 2 or {g['sufficient'] for g in pair} != {False, True} or len(
                {lookup[g['id']]['question'] for g in pair}) != 1:
            raise ValueError('Broken positive/negative question pair')
    manifest = load(run / 'manifest.json')
    complete = load(run / 'complete.json')
    if manifest['version'] != VERSION or manifest['modes'] != ['direct', 'chain']:
        raise ValueError('Expected the frozen direct/chain experiment')
    if manifest['inputs_sha256'] != file_sha256(data / f'{split}.inputs.json'):
        raise ValueError('Run used different inputs')
    if not manifest.get('model_files') or not manifest.get('script_sha256'):
        raise ValueError('Missing model/code fingerprints')
    if manifest.get('no_truncation') is not True:
        raise ValueError('Original labels require untruncated contexts')
    records = [json.loads(line) for line in (run / 'predictions.jsonl').read_text(
        encoding='utf-8').splitlines()]
    if (len(records) != len(inputs) * 2 or complete['rows'] != len(records) or
            complete['run_id'] != digest(manifest) or
            complete['predictions_sha256'] != file_sha256(run / 'predictions.jsonl')):
        raise ValueError('Incomplete or changed prediction receipt')
    seen = set()
    for r in records:
        key = (r['id'], r['mode'])
        if key in seen or r['id'] not in lookup or r['mode'] not in manifest['modes']:
            raise ValueError('Duplicate, foreign or unknown-mode prediction')
        seen.add(key)
        if r['run_id'] != digest(manifest) or r['input_sha256'] != digest(lookup[r['id']]):
            raise ValueError('Prediction fingerprint mismatch')
        if r['status'] not in {'ok', 'input_overflow', 'output_limit', 'invalid_output', 'oom'}:
            raise ValueError('Unknown prediction status')
        if r['status'] == 'ok':
            if (type(r['prediction']) is not bool or
                    r['prediction'] != parse_output(r['raw_output'], r['mode']) or
                    r['input_tokens'] > manifest['max_input_tokens']):
                raise ValueError('Invalid successful prediction')
        elif r['prediction'] is not None:
            raise ValueError('Failed output silently repaired')
    return manifest, inputs, gold, records


def diagnostics(gold, records):
    arms = {mode: {r['id']: r for r in records if r['mode'] == mode}
            for mode in ('direct', 'chain')}
    changes = {label: Counter() for label in ('sufficient', 'insufficient')}
    for g in gold:
        before, after = [arms[mode][g['id']] for mode in arms]
        b = before['status'] == 'ok' and before['prediction'] == g['sufficient']
        c = after['status'] == 'ok' and after['prediction'] == g['sufficient']
        changes['sufficient' if g['sufficient'] else 'insufficient'][
            'improved' if c and not b else 'regressed' if b and not c else
            'both_correct' if b else 'both_wrong'] += 1
    return {'class_transitions': {k: dict(v) for k, v in changes.items()},
            'generation': {mode: {
                'status_counts': dict(Counter(r['status'] for r in arm.values())),
                'median_seconds': statistics.median(r['seconds'] for r in arm.values()),
                'total_output_tokens': sum(r.get('output_tokens', 0) for r in arm.values()),
            } for mode, arm in arms.items()},
            'timing_boundary': 'Sequential generation diagnostics, not production end-to-end latency'}


def finalize(data, development, heldout, output):
    verified = {split: verify_split(data, split, run) for split, run in
                [('development', development), ('heldout', heldout)]}
    # Input identity is the only setting allowed to differ across these two runs.
    manifests = [{k: v for k, v in value[0].items() if k != 'inputs_sha256'}
                 for value in verified.values()]
    if manifests[0] != manifests[1]:
        raise ValueError('Model, prompt, code or inference settings differ across splits')
    dev, test = verified.values()
    if ({g['group'] for g in dev[2]} & {g['group'] for g in test[2]} or
            {r['question'] for r in dev[1]} & {r['question'] for r in test[1]}):
        raise ValueError('Development/heldout question overlap')
    output.mkdir(parents=True, exist_ok=False)
    reports = {}
    for split, run in [('development', development), ('heldout', heldout)]:
        target = output / f'{split}-score.json'
        evaluate(SimpleNamespace(inputs=data / f'{split}.inputs.json',
                                 gold=data / f'{split}.gold.json',
                                 runs=[f'qwen3-8b={run}'], baseline='qwen3-8b/direct',
                                 output=target))
        reports[split] = {**load(target), 'diagnostics': diagnostics(
            verified[split][2], verified[split][3])}
    report = {'reports': reports, 'heldout_gain_replicated': reports['heldout'][
        'comparisons']['qwen3-8b/chain']['meaningful_gain_passed'],
        'production_ready': False,
        'boundary': 'Relative gain criterion only; absolute sufficient recall and Zotero transfer still require validation',
        'data_manifest_sha256': file_sha256(data / 'manifest.json'),
        'finalizer_sha256': file_sha256(__file__)}
    write_new(output / 'summary.json', report)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path, required=True)
    parser.add_argument('--development', type=Path, required=True)
    parser.add_argument('--heldout', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    finalize(args.data, args.development, args.heldout, args.output)
