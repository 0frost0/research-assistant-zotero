"""Verify an already-built Zotero scope and bounded retrieval without generation.

No paper excerpts or secrets are saved. This is index/provenance smoke testing,
not a relevance benchmark or answer-quality evaluation.
"""
import argparse
from collections import Counter
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from research_assistant.core.service_context import stored_service_context
from research_assistant.knowledge.service import KnowledgeService
from research_assistant.knowledge.store import KnowledgeStore, compact_text, digest


QUERIES = [
    'BERT masked language modeling next sentence prediction',
    'low dose CT image denoising diffusion model',
    '心衰 电子健康记录 时间事件 生存分析',
    'retrieval augmented generation agent tool use',
    'medical image segmentation U-Net',
    'multimodal clinical notes irregular time series',
]


def verify(root, connection, base_id):
    store = KnowledgeStore(root)
    snapshot = store.snapshot([base_id])
    sources = {e['data']['hash'] + '.pdf' for e in snapshot['entities'] if e['data']['kind'] == 'document'}
    with store.read() as con:
        objects = {sha + '.pdf': (json.loads(pages), json.loads(report)) for sha, pages, report
                   in con.execute('SELECT hash,pages,report FROM objects') if sha + '.pdf' in sources}
    text_sources = {source for source, (pages, _) in objects.items() if any(p['text'].strip() for p in pages)}
    with stored_service_context(root, connection):
        service = KnowledgeService(store, 'multimodal')
        index = service.get_index(sources)
        if index is None or index.unified_error or index.unified_index is None:
            raise RuntimeError('Qwen index unavailable; lexical fallback is not acceptance')
        unified = index.unified_index
        counts = Counter(str(chunk.metadata['source']) for chunk in index.chunks)
        per_source = []
        for source, count in sorted(counts.items()):
            actual = unified.client.count(unified.collection_name,
                count_filter=unified._filter(source, 'text'), exact=True).count
            per_source.append({'source_id': source, 'expected_chunks': count, 'indexed_points': actual})
        probes = []
        for query in QUERIES:
            for mode in ('legacy', 'hybrid'):
                started = time.monotonic()
                result = service.search({'kb_ids': [base_id], 'query': query, 'scope': 'original',
                                         'text_only': True, 'retrieval_mode': mode, 'k': 6})
                hits = []
                for hit in result['results']:
                    pages = objects.get(hit['source_id'], ([], {}))[0]
                    page = next((p for p in pages if p['page'] == hit['page']), None)
                    hits.append({'source_id': hit['source_id'], 'page': hit['page'],
                        'evidence_id': hit['retrieval_evidence_id'], 'url': hit['url'],
                        'quote_sha256': digest(hit['quote'].encode()),
                        'source_page_verified': bool(page and compact_text(hit['quote']) in compact_text(page['text'])),
                        'scope_verified': hit['source_id'] in sources})
                probes.append({'query': query, 'mode': mode, 'backend': result['backend'],
                    'elapsed_ms': round((time.monotonic() - started) * 1000),
                    'has_warning': bool(result['warning']), 'hits': hits})
        store.assert_current(snapshot)
        collection_points = unified.client.count(unified.collection_name, exact=True).count
    valid = (set(counts) == text_sources and all(r['expected_chunks'] == r['indexed_points'] for r in per_source)
             and collection_points == len(index.chunks)
             and all(p['hits'] and p['backend'].startswith('multimodal') and
                     all(h['source_page_verified'] and h['scope_verified'] and h['evidence_id'] for h in p['hits'])
                     for p in probes))
    return {'valid': valid, 'base_id': base_id, 'revision': snapshot['bases'][0]['revision'],
        'scope_unique_pdfs': len(sources), 'text_indexed_pdfs': len(text_sources),
        'pdfs_without_text': sorted(sources - text_sources),
        'total_pages': sum(r['total_pages'] for _, r in objects.values()),
        'pages_with_any_text': sum(len(pages) for pages, _ in objects.values()),
        'native_sufficient_text_pages': sum(r['native_pages'] for _, r in objects.values()),
        'empty_pages': sum(r['empty_pages'] for _, r in objects.values()),
        'low_text_pages': sum(r['low_text_pages'] for _, r in objects.values()),
        'collection': unified.collection_name, 'chunks': len(index.chunks), 'collection_points': collection_points,
        'sources': per_source, 'probes': probes,
        'answer_model_calls': 0, 'reranker_calls': 0,
        'boundary': 'index_integrity_and_provenance_only_not_relevance_or_answer_quality'}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path('.'))
    parser.add_argument('--connection', required=True)
    parser.add_argument('--base-id', required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    report = verify(args.root, args.connection, args.base_id)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: v for k, v in report.items() if k not in {'sources', 'probes'}}, ensure_ascii=False))
    if not report['valid']:
        raise SystemExit(1)
