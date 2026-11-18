"""Offline question-builder protocol fixtures, not retrieval-quality evidence."""
from collections import Counter, defaultdict
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from benchmarks.build_qwen_stage1_zotero_set import BASE_ID, PAPERS, SPECS, build, construct, render_questions
from benchmarks.validate_knowledge_qa_dataset import validate_dataset
from research_assistant.knowledge.store import KnowledgeStore


def fixture(root):
    store = KnowledgeStore(root)
    pages = defaultdict(lambda: defaultdict(list))
    for prefix, _, _, _, anchors in SPECS:
        for page, locator in anchors:
            pages[prefix][page].append(locator)
    with store.transaction() as con:
        con.execute('INSERT INTO knowledge_bases VALUES(?,?,?,?,?,?,?,0,?,?)',
                    (BASE_ID, 'fixture', 'user:0', 'snapshot', 'Fixture', 0, 1, 'fixture-hash', 'now'))
        for prefix, mapping in pages.items():
            sha = prefix + '0' * 56
            payload = json.dumps({'kind': 'document', 'hash': sha, 'title': PAPERS[prefix][0]})
            object_pages = json.dumps([{'page': page, 'text': '\nSeparate fixture passage.\n'.join(texts)}
                                       for page, texts in sorted(mapping.items())])
            report = json.dumps({'total_pages': len(mapping), 'native_pages': len(mapping),
                                 'low_text_pages': 0, 'empty_pages': 0})
            con.execute('INSERT INTO objects VALUES(?,?,?)', (sha, object_pages, report))
            con.execute('INSERT INTO entities VALUES(?,?,?,?,?,?,?,?)',
                        (prefix, 'fixture', 'user:0', prefix, 'document', payload, 'fingerprint', 1))
            con.execute('INSERT INTO memberships VALUES(?,?)', (BASE_ID, prefix))
    return store


class ZoteroQuestionSetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = fixture(self.root)

    def test_specs_are_balanced_and_not_duplicate_queries_or_anchors(self):
        self.assertEqual(len(SPECS), 48)
        self.assertEqual(len({s[3] for s in SPECS}), 48)
        self.assertEqual(set(Counter(s[0] for s in SPECS).values()), {4})
        self.assertEqual(len(PAPERS), 12)
        self.assertEqual(len({value[1] for value in PAPERS.values()}), 7)
        self.assertEqual(len({(s[0], tuple(s[4])) for s in SPECS}), 48)

    def test_build_is_deterministic_readonly_and_full_scope(self):
        original = self.store.path.read_bytes()
        dataset = construct(self.root)
        self.assertEqual(dataset, construct(self.root))
        self.assertEqual(original, self.store.path.read_bytes())
        self.assertEqual(dataset['split'], 'development')
        self.assertIn('not_independent_human_gold', dataset['provenance'])
        self.assertEqual(dataset['coverage']['anchors'], 57)
        self.assertEqual(dataset['coverage']['evidence_shapes']['multi_anchor'], 9)
        self.assertEqual(dataset['coverage']['nonfirst_page_questions'], 43)
        self.assertTrue(all(c['kb_ids'] == [BASE_ID] for c in dataset['cases']))
        self.assertEqual(len(dataset['snapshot']['manifest']), 12)
        probes = [c for c in dataset['cases'] if c['no_answer']]
        self.assertEqual(len(probes), 4)
        self.assertTrue(all(not c['scored'] and not c['expected'] for c in probes))
        self.assertTrue(all(c['absence_status'] == 'not_exhaustively_verified_across_corpus' for c in probes))

    def test_frozen_file_validates_and_cannot_be_overwritten(self):
        output = self.root / 'frozen.json'
        with redirect_stdout(StringIO()):
            report = build(self.root, output)
        result = validate_dataset(self.root, output, 'development')
        self.assertTrue(result['valid'], result['errors'])
        self.assertEqual(result['checked_evidence'], 57)
        self.assertEqual(result['dataset_sha256'], report['sha256'])
        original = output.read_bytes()
        with self.assertRaises(FileExistsError):
            build(self.root, output)
        self.assertEqual(output.read_bytes(), original)
        self.assertFalse(validate_dataset(self.root, output, 'test')['valid'])

    def test_out_of_scope_source_fails_before_file_creation(self):
        with self.store.transaction() as con:
            con.execute('DELETE FROM memberships WHERE entity_id=?', ('f5acfae2',))
        output = self.root / 'bad.json'
        with self.assertRaisesRegex(ValueError, 'in-scope PDF'):
            build(self.root, output)
        self.assertFalse(output.exists())

    def test_stale_or_archived_base_is_rejected(self):
        for column in ('revision', 'archived'):
            with self.subTest(column=column):
                with self.store.transaction() as con:
                    con.execute('UPDATE knowledge_bases SET revision=1,archived=0')
                    con.execute(f'UPDATE knowledge_bases SET {column}=?', (2 if column == 'revision' else 1,))
                with self.assertRaisesRegex(ValueError, 'revision 1'):
                    construct(self.root)

    def test_missing_anchor_or_physical_page_is_rejected(self):
        for pages in ([{'page': 1, 'text': 'unrelated text'}], []):
            with self.subTest(pages=pages):
                with self.store.transaction() as con:
                    con.execute('UPDATE objects SET pages=? WHERE hash LIKE ?',
                                (json.dumps(pages), 'f5acfae2%'))
                with self.assertRaises(ValueError):
                    construct(self.root)

    def test_membership_manifest_detects_source_text_changes(self):
        before = construct(self.root)
        with self.store.transaction() as con:
            sha, pages = con.execute('SELECT hash,pages FROM objects LIMIT 1').fetchone()
            updated = json.loads(pages)
            updated[0]['text'] += '\nAdditional fixture text.'
            con.execute('UPDATE objects SET pages=? WHERE hash=?', (json.dumps(updated), sha))
        after = construct(self.root)
        self.assertNotEqual(before['snapshot']['membership_manifest_sha256'],
                            after['snapshot']['membership_manifest_sha256'])

    def test_missing_database_is_not_created(self):
        absent = self.root / 'absent'
        with self.assertRaises(sqlite3.OperationalError):
            construct(absent)
        self.assertFalse(absent.exists())

    def test_human_readable_companion_has_all_queries_but_no_quotes(self):
        dataset = construct(self.root)
        text = render_questions(dataset, 'fixture-sha')
        self.assertIn('fixture-sha', text)
        self.assertIn('非独立人工 test', text)
        self.assertTrue(all(c['query'] in text for c in dataset['cases']))
        self.assertTrue(all(g['quote'] not in text for c in dataset['cases'] for g in c['expected']))


if __name__ == '__main__':
    unittest.main()
