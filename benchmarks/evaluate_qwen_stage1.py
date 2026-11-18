"""Paired Qwen retrieval/reranking evaluation; no generation or production writes.

Copies only the knowledge SQLite snapshot to a temporary directory, uses memory
Qdrant and the production KnowledgeService/search/rerank code. Reports contain
IDs, hashes and ranks, never paper excerpts or credentials. No coverage selector,
query-rewrite model or sufficiency gate runs here.
"""
import argparse
import asyncio
from collections import Counter
import hashlib
import json
from pathlib import Path
import random
import sqlite3
import statistics
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from qdrant_client import QdrantClient
from benchmarks.evaluate_knowledge_qa import check_split
from benchmarks.validate_knowledge_qa_dataset import validate_dataset
from research_assistant.core.service_context import stored_service_context, embedding_config
from research_assistant.knowledge.qa import candidate_key, normalized, rerank_candidates
from research_assistant.knowledge.service import KnowledgeService, search_ready
from research_assistant.knowledge.store import KnowledgeStore
from research_assistant.retrieval.reranking import MODEL, REVISION
from research_assistant.retrieval.embeddings.api_embeddings import QwenAPIEmbeddings


# This warning describes known source coverage, not a retrieval backend
# failure.  Keep the string explicit: a broad "ignore warnings" rule would
# turn lexical fallback into a valid Qwen experiment.
COVERAGE_WARNING = "部分 PDF 页面缺少足够文字；此版本未执行 OCR，检索覆盖不完整。"


def sha(value):
    return hashlib.sha256(value).hexdigest()


def candidate_id(hit):
    return sha(json.dumps(candidate_key(hit), ensure_ascii=False).encode())


def classify_retrieval_warning(warning):
    """Classify search warnings without hiding a mixed degradation.

    ``KnowledgeService`` currently returns warnings as one joined string.
    Coverage may be present alongside a backend warning, so removing only the
    known coverage message is intentionally stricter than checking a prefix.
    """
    if not warning:
        return {'class': 'none', 'coverage': [], 'degradation': []}
    remaining = warning.replace(COVERAGE_WARNING, '').strip()
    if not remaining:
        return {'class': 'coverage', 'coverage': [COVERAGE_WARNING], 'degradation': []}
    return {'class': 'mixed' if COVERAGE_WARNING in warning else 'degradation',
            'coverage': [COVERAGE_WARNING] if COVERAGE_WARNING in warning else [],
            'degradation': [remaining]}


def validate_qwen_retrieval(found):
    """Return warning metadata or reject a non-Qwen retrieval result."""
    warning = classify_retrieval_warning(found.get('warning'))
    backend = found.get('backend', '')
    if backend not in {'multimodal', 'multimodal-unified', 'multimodal-hybrid'} or warning['degradation']:
        raise RuntimeError('Qwen retrieval degraded; not a valid stage-1 evaluation')
    if any(h.get('ranking', {}).get('dense_status') not in (None, 'succeeded')
           for h in found.get('results', [])):
        raise RuntimeError('Qwen retrieval degraded; dense channel did not succeed')
    return warning


def ranks(pool, expected):
    return [next((i for i, hit in enumerate(pool, 1)
                  if hit['source_id'] == gold['source_id'] and hit.get('page') == gold['page']
                  and normalized(gold['quote']) in normalized(hit['quote'])), None)
            for gold in expected]


def metrics(anchor_ranks, k=6):
    if not anchor_ranks:
        return {'candidate_anchor_recall': None, 'recall_at_1': None,
                'recall_at_6': None, 'mrr_at_6': None,
                'all_anchors_in_candidates': None, 'all_anchors_at_6': None}
    first = min((r for r in anchor_ranks if r is not None), default=None)
    return {'candidate_anchor_recall': sum(r is not None for r in anchor_ranks) / len(anchor_ranks),
            'recall_at_1': sum(r == 1 for r in anchor_ranks) / len(anchor_ranks),
            'recall_at_6': sum(r is not None and r <= k for r in anchor_ranks) / len(anchor_ranks),
            'mrr_at_6': 1 / first if first is not None and first <= k else 0,
            'all_anchors_in_candidates': int(all(r is not None for r in anchor_ranks)),
            'all_anchors_at_6': int(all(r is not None and r <= k for r in anchor_ranks))}


async def paired_case(pool, case, *, client_factory=None):
    """Exactly one scoring call on an unchanged candidate set; no selection."""
    before_ids = [candidate_id(h) for h in pool]
    before_ranks = ranks(pool, case['expected'])
    ranked, trace = await rerank_candidates(pool, case['query'], enabled=True,
                                           client_factory=client_factory)
    after_ids = [candidate_id(h) for h in ranked]
    same_pool = Counter(before_ids) == Counter(after_ids)
    if not same_pool:
        raise ValueError('Reranking changed the candidate set')
    after_ranks = ranks(ranked, case['expected'])
    eligible = sum(h.get('kind') == 'original' and h.get('modality', 'text') == 'text'
                   and 0 < len(h.get('quote', '')) <= 2000 for h in pool)
    scored = sum('rerank_score' in h.get('ranking', {}) for h in ranked)
    return {'id': case['id'], 'no_answer': case['no_answer'], 'candidate_count': len(pool),
             **{key: case[key] for key in ('paper', 'paper_name', 'topic', 'category',
                                           'language', 'evidence_shape') if key in case},
             'anchor_count': len(case['expected']),
            'same_pool': same_pool, 'candidate_set_sha256': sha(json.dumps(sorted(before_ids)).encode()),
            'eligible_count': eligible, 'scored_count': scored,
            'skipped_length_or_kind': len(pool) - eligible,
            'unscored_budget_tail': max(0, eligible - 40),
            'all_candidates_scored': bool(pool) and scored == len(pool),
            'rerank': trace,
            'before': {'anchor_ranks': before_ranks, **metrics(before_ranks)},
            'after': {'anchor_ranks': after_ranks, **metrics(after_ranks)},
            'candidates': [{'id': candidate_id(h), 'source_id': h['source_id'],
                            'page': h.get('page'), 'revision': h.get('revision'),
                            'quote_sha256': sha(h['quote'].encode()), 'characters': len(h['quote']),
                            'before_rank': before_ids.index(candidate_id(h)) + 1,
                            'after_rank': i, 'rerank_score': h.get('ranking', {}).get('rerank_score')}
                           for i, h in enumerate(ranked, 1)]}


def paired_interval(rows, metric='mrr_at_6', *, cluster=None, samples=2000):
    """Deterministic paired bootstrap; paper clustering exposes within-paper dependence.

    An uncertainty diagnostic on this development set, not population evidence.
    Cluster mode samples papers and weights each sampled paper equally.
    """
    groups = {}
    for row in rows:
        key = row.get(cluster) if cluster else row['id']
        if key is not None and not row['no_answer']:
            groups.setdefault(key, []).append(row['after'][metric] - row['before'][metric])
    if not groups:
        return None
    deltas = [statistics.mean(values) for values in groups.values()]
    rng = random.Random(20261110)
    draws = sorted(statistics.mean(rng.choices(deltas, k=len(deltas))) for _ in range(samples))
    return {'mean_delta': statistics.mean(deltas), 'low_95': draws[int(.025 * samples)],
            'high_95': draws[min(samples - 1, int(.975 * samples))],
            'resampling_unit': cluster or 'question', 'units': len(deltas), 'samples': samples,
            'seed': 20261110}


def summarize(rows, *, breakdown=True):
    summary = {}
    for mode in sorted({r['retrieval_mode'] for r in rows}):
        group = [r for r in rows if r['retrieval_mode'] == mode]
        labelled = [r for r in group if not r['no_answer']]
        arm = {'cases': len(group), 'labelled_cases': len(labelled),
               'no_answer_cases_unscored': sum(r['no_answer'] for r in group),
               'rerank_statuses': dict(Counter(r['rerank']['status'] for r in group)),
               'fully_scored_cases': sum(r['all_candidates_scored'] for r in group),
               'median_rerank_ms': statistics.median(r['rerank']['elapsed_ms'] for r in group)}
        for side in ('before', 'after'):
            arm[side] = {key: statistics.mean(r[side][key] for r in labelled) if labelled else None
                         for key in metrics([1])}
        arm['anchor_counts'] = {}
        for side in ('before', 'after'):
            all_ranks = [rank for r in labelled for rank in r[side]['anchor_ranks']]
            arm['anchor_counts'][side] = {'total': len(all_ranks),
                'in_candidates': sum(rank is not None for rank in all_ranks),
                'at_1': sum(rank == 1 for rank in all_ranks),
                'at_6': sum(rank is not None and rank <= 6 for rank in all_ranks)}
        # Compare first labelled anchor; absent anchors sort after every candidate.
        changes = Counter()
        for r in labelled:
            a, b = (min((rank for rank in r[side]['anchor_ranks'] if rank is not None),
                         default=float('inf')) for side in ('before', 'after'))
            changes['improved' if b < a else 'regressed' if b > a else 'unchanged'] += 1
        arm['first_anchor_rank_changes'] = dict(changes)
        arm['paired_mrr_delta_bootstrap'] = paired_interval(labelled)
        if any(r.get('paper') for r in labelled):
            arm['paper_macro_mrr_delta_bootstrap'] = paired_interval(labelled, cluster='paper')
        if breakdown:
            arm['by_group'] = {}
            for key in ('paper', 'paper_name', 'topic', 'category', 'language',
                        'evidence_shape', 'anchor_count'):
                values = sorted({r[key] for r in group if key in r})
                if values:
                    arm['by_group'][key] = {value: summarize(
                        [r for r in group if r.get(key) == value], breakdown=False)[mode]
                        for value in values}
        summary[mode] = arm
    return summary


def valid_pair(row):
    # Empty retrieval is a valid observation, not a failed model call. It must
    # remain explicitly unscored rather than being relabelled as success.
    return row['same_pool'] and (row['rerank']['status'] == 'succeeded' or
           row['candidate_count'] == 0 and row['rerank']['status'] == 'no_eligible_passages')


def clone_snapshot(root, target):
    source = (root / '.data/zotero/knowledge.sqlite3').resolve()
    destination = target / '.data/zotero/knowledge.sqlite3'
    destination.parent.mkdir(parents=True, exist_ok=True)
    src = sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)
    dst = sqlite3.connect(destination)
    try:
        src.backup(dst)
        dst.commit()
    finally:
        dst.close()
        src.close()
    return sha(destination.read_bytes())


async def run(args):
    raw = args.dataset.read_bytes()
    dataset = json.loads(raw)
    split = check_split(dataset, args.expected_split)
    cases = dataset['cases']
    if not cases or len(cases) > args.max_cases:
        raise ValueError('Dataset exceeds explicit case budget or is empty')
    if any(c.get('scope', 'original') != 'original' for c in cases):
        raise ValueError('Stage 1 accepts only original-paper cases')
    rows = []
    with tempfile.TemporaryDirectory(prefix='qwen-stage1-') as folder:
        isolated = Path(folder)
        snapshot_hash = clone_snapshot(args.root, isolated)
        if dataset.get('snapshot', {}).get('membership_manifest_sha256'):
            # Reuse the read-only builder's exact source manifest checks. This
            # verifies all competing papers, not just the 12 labelled sources.
            from benchmarks.build_qwen_stage1_zotero_set import construct
            if construct(isolated)['snapshot'] != dataset['snapshot']:
                raise ValueError('Frozen corpus snapshot/manifest changed')
        validation = validate_dataset(isolated, args.dataset, args.expected_split, split == 'synthetic')
        if not validation['valid']:
            raise ValueError('Invalid evidence anchors: ' + '; '.join(validation['errors'][:5]))
        client = QdrantClient(':memory:')
        try:
            cache = getattr(args, 'read_only_embedding_cache', None)
            def embeddings_factory(config, *, cache_path=None):
                return QwenAPIEmbeddings(config, cache_path=cache or cache_path,
                                         cache_read_only=bool(cache))
            with stored_service_context(args.profile_root, args.connection), patch(
                    'research_assistant.retrieval.unified_index.QdrantClient', return_value=client), patch(
                    'research_assistant.retrieval.embeddings.api_embeddings.QwenAPIEmbeddings',
                    side_effect=embeddings_factory):
                config = embedding_config() or {}
                if config.get('model') != 'Qwen/Qwen3-VL-Embedding-2B':
                    raise ValueError('Expected saved Qwen embedding profile')
                models = {'embedding': {k: config.get(k) for k in ('model', 'dimension', 'revision')},
                          'reranker': {'model': MODEL, 'revision': REVISION}}
                store = KnowledgeStore(isolated)
                service = KnowledgeService(store, 'multimodal')
                for case in cases:
                    # A fresh isolated six-paper index may need longer than
                    # the interactive UI's two-minute background-build window.
                    # Build synchronously in this offline CLI, then exercise the
                    # unchanged production search path; no UI timeout changes.
                    snapshot = store.snapshot(case['kb_ids'])
                    sources = {entity['data']['hash'] + '.pdf' for entity in snapshot['entities']
                               if entity['data']['kind'] == 'document'}
                    index = await asyncio.to_thread(service.get_index, sources)
                    if index is None or index.unified_error or index.unified_index is None:
                        raise RuntimeError('Qwen index unavailable; lexical fallback is not acceptance')
                    for mode in args.modes:
                        start = time.monotonic()
                        found = await search_ready(service, {**case, 'scope': 'original', 'k': 20,
                                                             'text_only': True, 'retrieval_mode': mode})
                        warning = validate_qwen_retrieval(found)
                        elapsed = round((time.monotonic() - start) * 1000)
                        row = await paired_case(found['results'], case)
                        row.update(retrieval_mode=mode, backend=found['backend'],
                                   retrieval_ms=elapsed, revisions=found['revisions'],
                                   retrieval_warning=warning)
                        rows.append(row)
                        print(f"{case['id']} {mode}: {row['rerank']['status']} "
                              f"scored={row['scored_count']}/{row['candidate_count']}", flush=True)
        finally:
            client.close()
    warning_counts = Counter(r['retrieval_warning']['class'] for r in rows)
    report = {'kind': 'paired_same_pool_qwen_stage1', 'dataset_id': dataset['id'],
              'dataset_sha256': sha(raw), 'snapshot_sha256': snapshot_hash, 'split': split,
              'provenance': dataset.get('provenance', 'synthetic_not_scientific_gold'),
              'models': models, 'answer_model_calls': 0, 'reranker_calls': sum(
                  r['rerank']['status'] == 'succeeded' for r in rows),
              'production_writes': False,
              'embedding_cache_policy': 'read_only_shared_exact_content_model_key' if cache else 'isolated',
              'retrieval_warning_counts': dict(warning_counts),
              'k': 6, 'query_policy': 'one original query; existing deterministic aliases retained',
              'boundary': 'Anchor ranks only, not answer accuracy, sufficiency or independent human gold. '
                          'Before/after use identical materialized candidates; no coverage selection. '
                          'Warm-cache timings are diagnostic, not controlled latency comparisons.',
              'dataset_validation': {k: v for k, v in validation.items() if k != 'errors'},
              'cases': rows, 'summary': summarize(rows),
              'valid': all(valid_pair(r) for r in rows)}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({mode: {k: v for k, v in arm.items() if k != 'by_group'}
                      for mode, arm in report['summary'].items()}, ensure_ascii=False, indent=2))
    if not report['valid']:
        raise RuntimeError('Report saved, but at least one reranking call did not succeed')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--dataset', type=Path, required=True)
    parser.add_argument('--profile-root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--connection', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--expected-split', choices=['synthetic', 'development', 'test'], required=True)
    parser.add_argument('--modes', nargs='+', choices=['legacy', 'hybrid'], default=['legacy', 'hybrid'])
    parser.add_argument('--max-cases', type=int, default=12)
    parser.add_argument('--read-only-embedding-cache', type=Path,
                        help='Reuse exact model/content cache hits without writing to this cache')
    args = parser.parse_args()
    if args.max_cases < 1 or len(set(args.modes)) != len(args.modes):
        parser.error('Positive case budget and unique modes required')
    asyncio.run(run(args))
