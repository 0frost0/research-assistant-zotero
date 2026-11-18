"""Paired evaluation contracts; deterministic fixtures are not retrieval quality."""
import argparse
from contextlib import closing
from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from benchmarks.evaluate_qwen_stage1 import (clone_snapshot, classify_retrieval_warning,
    metrics, paired_case, summarize, valid_pair, paired_interval, validate_qwen_retrieval)
from benchmarks.build_qwen_stage1_library_set import SPECS, extract_anchor
from benchmarks.run_retrieval_ablation import run as ablation
from research_assistant.knowledge.qa import EvidenceQA
from research_assistant.knowledge.store import KnowledgeStore
from research_assistant.retrieval.embeddings.api_embeddings import QwenAPIEmbeddings
from tests.test_knowledge_qa import Service, ScriptedModel, answer, SUPPORT, hit


def passage(ident, text):
    return {'kind': 'original', 'modality': 'text', 'source_id': ident + '.pdf',
            'page': 1, 'revision': 1, 'quote': text}


def case():
    return {'id': 'fixture', 'query': 'a question', 'no_answer': False,
            'expected': [{'source_id': 'a.pdf', 'page': 1, 'quote': 'anchor'}]}


class Client:
    def __init__(self, **kw):
        pass

    def scores(self, question, documents):
        return [i / 40 for i in range(len(documents))]


class Stage1Tests(unittest.IsolatedAsyncioTestCase):
    async def test_pair_keeps_identical_pool_and_exposes_regression(self):
        pool = [passage('a', 'anchor original'), passage('b', 'distractor')]
        original = deepcopy(pool)
        row = await paired_case(pool, case(), client_factory=Client)
        self.assertEqual(pool, original)
        self.assertTrue(row['same_pool'])
        self.assertEqual(row['before']['anchor_ranks'], [1])
        self.assertEqual(row['after']['anchor_ranks'], [2])
        self.assertTrue(row['all_candidates_scored'])
        summary = summarize([{**row, 'retrieval_mode': 'legacy'}])['legacy']
        self.assertEqual(summary['first_anchor_rank_changes'], {'regressed': 1})
        self.assertNotIn('distractor', json.dumps(row))
        self.assertNotIn('anchor original', json.dumps(row))

    async def test_pair_exposes_skipped_long_passages_and_budget_tail(self):
        pool = [passage(str(i), 'short text ' + str(i)) for i in range(41)]
        pool.append(passage('long', 'x' * 2001))
        row = await paired_case(pool, case(), client_factory=Client)
        self.assertEqual(row['eligible_count'], 41)
        self.assertEqual(row['scored_count'], 40)
        self.assertEqual(row['skipped_length_or_kind'], 1)
        self.assertEqual(row['unscored_budget_tail'], 1)
        self.assertFalse(row['all_candidates_scored'])
        self.assertEqual(row['before']['candidate_anchor_recall'], 0)

    async def test_multi_anchor_metadata_and_all_required_metrics(self):
        c = {**case(), 'paper': 'a', 'paper_name': 'Fixture', 'topic': 'methods',
             'evidence_shape': 'multi_anchor',
             'expected': [*case()['expected'], {'source_id': 'b.pdf', 'page': 1, 'quote': 'second'}]}
        row = await paired_case([passage('a', 'anchor'), passage('c', 'unrelated')],
                                c, client_factory=Client)
        self.assertEqual(row['anchor_count'], 2)
        self.assertEqual(row['before']['candidate_anchor_recall'], .5)
        self.assertEqual(row['before']['all_anchors_in_candidates'], 0)
        self.assertEqual(row['after']['all_anchors_at_6'], 0)
        summary = summarize([{**row, 'retrieval_mode': 'legacy'}])['legacy']
        self.assertEqual(summary['by_group']['topic']['methods']['labelled_cases'], 1)
        self.assertEqual(summary['by_group']['evidence_shape']['multi_anchor']['cases'], 1)
        self.assertEqual(summary['anchor_counts']['before']['total'], 2)
        self.assertEqual(summary['anchor_counts']['before']['in_candidates'], 1)

    async def test_no_answer_remains_unscored_not_an_accuracy_claim(self):
        c = {**case(), 'no_answer': True, 'expected': []}
        row = await paired_case([passage('a', 'related material')], c, client_factory=Client)
        summary = summarize([{**row, 'retrieval_mode': 'legacy'}])['legacy']
        self.assertEqual(summary['no_answer_cases_unscored'], 1)
        self.assertIsNone(summary['after']['recall_at_6'])
        self.assertIsNone(row['after']['mrr_at_6'])

    async def test_service_failure_is_not_counted_as_success(self):
        class Broken(Client):
            def scores(self, question, documents):
                raise RuntimeError('PRIVATE PROVIDER BODY')
        row = await paired_case([passage('a', 'anchor')], case(), client_factory=Broken)
        self.assertEqual(row['rerank']['status'], 'fallback')
        self.assertEqual(row['scored_count'], 0)
        self.assertEqual(row['before'], row['after'])
        self.assertNotIn('PRIVATE', json.dumps(row))

    async def test_changed_pool_is_rejected(self):
        pool = [passage('a', 'anchor')]
        with patch('benchmarks.evaluate_qwen_stage1.rerank_candidates',
                   AsyncMock(return_value=([], {'status': 'succeeded'}))):
            with self.assertRaisesRegex(ValueError, 'candidate set'):
                await paired_case(pool, case())

    async def test_empty_retrieval_is_valid_but_not_successful_reranking(self):
        row = await paired_case([], {**case(), 'no_answer': True, 'expected': []}, client_factory=Client)
        self.assertTrue(valid_pair(row))
        self.assertEqual(row['rerank']['status'], 'no_eligible_passages')
        self.assertFalse(row['all_candidates_scored'])
        self.assertEqual(row['scored_count'], 0)

    async def test_ablation_explicitly_disables_gate_without_model(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            dataset = root / 'dataset.json'
            dataset.write_text(json.dumps({'split': 'synthetic'}))
            args = argparse.Namespace(root=root, dataset=dataset, output=root / 'reports',
                                      profile_root=root, connection=None, max_cases=12)
            with patch('benchmarks.run_retrieval_ablation.evaluate', AsyncMock()) as evaluate:
                await ablation(args)
            self.assertEqual(evaluate.await_count, 4)
            for call in evaluate.await_args_list:
                options = call.args[0]
                self.assertEqual(options.sufficiency_gates, ['off'])
                self.assertFalse(options.with_model)

    async def test_default_qa_passes_reranked_evidence_to_answer(self):
        # Scripted answer/review, actual production rerank/selection functions.
        with tempfile.TemporaryDirectory() as folder:
            store = KnowledgeStore(Path(folder))
            base = store.create({'connection': 'fixture', 'library': 'user:0',
                                 'collection': 'A', 'name': 'A'})
            first = hit('A distractor with no survival result.', source_id='b.pdf')
            target = hit('The method did not improve survival in the small cohort.', source_id='a.pdf')
            service = Service(store, [[first, target]])
            model = ScriptedModel([answer, SUPPORT])
            with patch.dict('os.environ', {'RESEARCH_RERANKER_URL': 'http://127.0.0.1:30002/rerank'}), patch(
                    'research_assistant.retrieval.reranking.RerankerClient', Client):
                result = await EvidenceQA(service, model, configured=lambda: True).ask({
                    'kb_ids': [base['id']], 'query': 'Does the method improve survival?'})
            self.assertEqual(result['status'], 'answered')
            self.assertEqual(service.calls[0]['retrieval_mode'], 'hybrid')
            self.assertEqual(model.payloads[0]['evidence'][0]['quote'], target['quote'])
            self.assertEqual(result['evidence'][0]['source_id'], 'a.pdf')
            self.assertIn('rerank_score', result['evidence'][0]['ranking'])
            self.assertEqual(model.calls, ['answer', 'review'])
            self.assertEqual(result['trace']['algorithm'], 'hybrid_rerank')


class MetricTests(unittest.TestCase):
    def test_readonly_embedding_cache_reuses_hit_and_never_persists_miss(self):
        class Reply:
            def __enter__(self):
                from io import StringIO
                return StringIO(json.dumps({'data': [{'embedding': [1., 0.]}]}))
            def __exit__(self, *args):
                pass
        from unittest.mock import Mock
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'cache.sqlite3'
            config = {'base_url': 'http://127.0.0.1:30001/v1', 'model': 'fixture',
                      'api_key': 'fixture', 'dimension': 2}
            writer = QwenAPIEmbeddings(config, cache_path=path)
            writer.opener = Mock()
            writer.opener.open.return_value = Reply()
            writer.embed_texts(['cached'])
            original = path.read_bytes()
            reader = QwenAPIEmbeddings(config, cache_path=path, cache_read_only=True)
            reader.opener = Mock()
            reader.opener.open.return_value = Reply()
            self.assertEqual(reader.embed_texts(['cached']), [[1., 0.]])
            reader.opener.open.assert_not_called()
            self.assertEqual(reader.embed_query('uncached'), [1., 0.])
            self.assertEqual(reader.opener.open.call_count, 1)
            self.assertEqual(path.read_bytes(), original)

    def test_readonly_embedding_cache_missing_path_is_not_created(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'absent' / 'cache.sqlite3'
            with self.assertRaisesRegex(ValueError, 'does not exist'):
                QwenAPIEmbeddings({'base_url': 'http://127.0.0.1:30001/v1', 'model': 'fixture',
                                   'api_key': 'fixture', 'dimension': 2},
                                  cache_path=path, cache_read_only=True)
            self.assertFalse(path.parent.exists())

    def test_coverage_warning_is_allowed_but_recorded(self):
        found = {'backend': 'multimodal',
                 'warning': '部分 PDF 页面缺少足够文字；此版本未执行 OCR，检索覆盖不完整。'}
        warning = validate_qwen_retrieval(found)
        self.assertEqual(warning['class'], 'coverage')
        self.assertEqual(len(warning['coverage']), 1)
        self.assertEqual(warning['degradation'], [])

    def test_backend_degradation_is_rejected_even_with_coverage_warning(self):
        found = {'backend': 'bm25-lexical-fallback',
                 'warning': '部分 PDF 页面缺少足够文字；此版本未执行 OCR，检索覆盖不完整。 '
                            '本次混合检索仅完成 BM25 词面召回；未计为已完成向量召回。'}
        classified = classify_retrieval_warning(found['warning'])
        self.assertEqual(classified['class'], 'mixed')
        with self.assertRaisesRegex(RuntimeError, 'degraded'):
            validate_qwen_retrieval(found)

    def test_missing_qwen_backend_is_rejected_without_warning(self):
        for backend in ('multimodal-fallback-tfidf', 'bm25-lexical', 'tfidf', None):
            with self.subTest(backend=backend), self.assertRaisesRegex(RuntimeError, 'degraded'):
                validate_qwen_retrieval({'backend': backend, 'warning': None})

    def test_unknown_or_mixed_warning_on_qwen_label_fails_closed(self):
        for warning in ('语义检索不可用，当前使用关键词检索。',
                        '部分 PDF 页面缺少足够文字；此版本未执行 OCR，检索覆盖不完整。 unknown'):
            with self.subTest(warning=warning), self.assertRaisesRegex(RuntimeError, 'degraded'):
                validate_qwen_retrieval({'backend': 'multimodal', 'warning': warning})

    def test_dense_failure_metadata_cannot_hide_behind_qwen_label(self):
        with self.assertRaisesRegex(RuntimeError, 'degraded'):
            validate_qwen_retrieval({'backend': 'multimodal-hybrid', 'warning': None,
                                    'results': [{'ranking': {'dense_status': 'fallback'}}]})

    def test_expanded_specs_cover_six_papers_without_synonym_duplicates(self):
        from collections import Counter
        self.assertEqual(len(SPECS), 36)
        self.assertEqual(set(Counter(s[0] for s in SPECS).values()), {6})
        self.assertEqual(len({s[4] for s in SPECS}), 36)
        self.assertEqual(len({(s[0], s[1], s[5]) for s in SPECS}), 36)
        self.assertEqual({s[3] for s in SPECS}, {'en', 'zh'})
        self.assertGreaterEqual(len({s[2] for s in SPECS}), 6)

    def test_anchor_extraction_preserves_original_whitespace_and_symbol_case(self):
        text = 'An original CO\n measurement, not Co.'
        self.assertEqual(extract_anchor(text, 'CO measurement'), 'CO\n measurement')
        with self.assertRaises(ValueError):
            extract_anchor(text, 'co measurement')

    def test_paired_interval_is_reproducible_and_keeps_regressions(self):
        rows = [{'id': str(i), 'paper': str(i // 2), 'no_answer': False,
                 'before': {'mrr_at_6': 1}, 'after': {'mrr_at_6': .5}} for i in range(6)]
        result = paired_interval(rows)
        self.assertEqual(result, paired_interval(rows))
        self.assertEqual(result['mean_delta'], -.5)
        self.assertEqual(result['high_95'], -.5)
        self.assertEqual(paired_interval(rows, cluster='paper')['units'], 3)
        self.assertIsNone(paired_interval([]))

    def test_group_summary_does_not_score_no_answer_as_failure(self):
        rows = [{'id': 'one', 'paper': 'p', 'category': 'number', 'language': 'zh',
                 'no_answer': False, 'retrieval_mode': 'hybrid', 'all_candidates_scored': True,
                 'rerank': {'status': 'succeeded', 'elapsed_ms': 1},
                 'before': {'anchor_ranks': [2], **metrics([2])},
                 'after': {'anchor_ranks': [1], **metrics([1])}}]
        result = summarize(rows)['hybrid']
        self.assertEqual(result['by_group']['paper']['p']['cases'], 1)
        self.assertEqual(result['paired_mrr_delta_bootstrap']['mean_delta'], .5)
        self.assertNotIn('by_group', result['by_group']['paper']['p'])

    def test_cutoff_and_candidate_recall_are_not_conflated(self):
        value = metrics([7, None, 2])
        self.assertAlmostEqual(value['candidate_anchor_recall'], 2 / 3)
        self.assertAlmostEqual(value['recall_at_6'], 1 / 3)
        self.assertEqual(value['mrr_at_6'], .5)
        self.assertEqual(metrics([7])['mrr_at_6'], 0)

    def test_clone_is_independent_and_does_not_copy_paper_files(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            home = root / 'source/.data/zotero'
            home.mkdir(parents=True)
            path = home / 'knowledge.sqlite3'
            with closing(sqlite3.connect(path)) as con, con:
                con.execute('CREATE TABLE fixture(value TEXT)')
                con.execute("INSERT INTO fixture VALUES('original')")
            source = path.read_bytes()
            clone_snapshot(root / 'source', root / 'target')
            copied = root / 'target/.data/zotero/knowledge.sqlite3'
            with closing(sqlite3.connect(copied)) as con, con:
                con.execute("UPDATE fixture SET value='isolated'")
            self.assertEqual(source, path.read_bytes())
            self.assertFalse((copied.parent / 'objects').exists())


if __name__ == '__main__':
    unittest.main()
