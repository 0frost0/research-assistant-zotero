import tempfile
import sqlite3
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from contextlib import nullcontext

from scripts.import_zotero_library import attachment_path, inventory, run
from research_assistant.knowledge.store import KnowledgeStore, digest, encoded
from scripts.verify_zotero_library_index import verify


class LibraryImportTests(unittest.TestCase):
    def test_storage_and_relative_paths(self):
        root = Path(tempfile.gettempdir()).resolve()
        self.assertEqual(attachment_path('storage:paper.pdf', 'ABC', root), root/'storage/ABC/paper.pdf')
        self.assertEqual(attachment_path('attachments:paper.pdf', 'ABC', root, root), root/'paper.pdf')

    def test_rejects_path_escape_and_unknown_relative(self):
        root = Path(tempfile.gettempdir()).resolve()
        for raw in ('storage:../../escape.pdf', 'attachments:paper.pdf', 'paper.pdf'):
            with self.assertRaises(ValueError):
                attachment_path(raw, 'ABC', root)

    def test_refuses_live_database(self):
        root = Path(tempfile.gettempdir())
        with self.assertRaises(ValueError):
            inventory(root/'zotero.sqlite', root)

    def test_inventory_excludes_deleted_parent_and_classifies_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db = root/'snapshot.sqlite'
            con = sqlite3.connect(db)
            con.executescript('''
                CREATE TABLE items(itemID INTEGER, libraryID INTEGER, key TEXT);
                CREATE TABLE libraries(libraryID INTEGER, type TEXT);
                CREATE TABLE itemAttachments(itemID INTEGER, parentItemID INTEGER, path TEXT, contentType TEXT);
                CREATE TABLE itemData(itemID INTEGER, fieldID INTEGER, valueID INTEGER);
                CREATE TABLE itemDataValues(valueID INTEGER, value TEXT);
                CREATE TABLE fields(fieldID INTEGER, fieldName TEXT);
                CREATE TABLE deletedItems(itemID INTEGER);
                INSERT INTO libraries VALUES(1,'user');
                INSERT INTO items VALUES(10,1,'P'),(20,1,'DELETED_PARENT');
                INSERT INTO deletedItems VALUES(20);
                INSERT INTO fields VALUES(1,'title');
                INSERT INTO itemData VALUES(10,1,1);
                INSERT INTO itemDataValues VALUES(1,'Title');
            ''')
            for i, (key, body) in enumerate([('OK', b'%PDF-ok'), ('MISSING', None),
                    ('BIG', b'x'), ('INVALID', b'not a PDF'), ('DELETED', b'%PDF-ok')], 1):
                con.execute('INSERT INTO items VALUES(?,1,?)', (i, key))
                con.execute('INSERT INTO itemAttachments VALUES(?,?,?,?)',
                    (i, 20 if key == 'DELETED' else 10, 'storage:paper.pdf', 'application/pdf'))
                if body is not None:
                    path = root/'storage'/key/'paper.pdf'
                    path.parent.mkdir(parents=True)
                    path.write_bytes(body)
                    if key == 'BIG':
                        with path.open('r+b') as f:
                            f.truncate(25*1024*1024+1)
            con.commit(); con.close()
            entries, paths = inventory(db, root)
            states = {e['key']: e['status'] for e in entries}
            self.assertEqual(states, {'OK':'ready','MISSING':'missing','BIG':'oversized','INVALID':'unreadable'})
            self.assertEqual(paths['OK'][1]['title'], 'Title')
            self.assertEqual(paths['OK'][1]['parent_key'], 'P')

    def test_additive_import_preserves_original_base_and_entity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            store = KnowledgeStore(root)
            content = b'%PDF-test'; sha = digest(content)
            path = store.objects/(sha+'.pdf'); path.write_bytes(content)
            with store.transaction() as con:
                con.execute('INSERT INTO objects VALUES(?,?,?)', (sha, encoded([{'page':1,'text':'text'}]),
                    encoded({'total_pages':1,'native_pages':1,'empty_pages':0,'low_text_pages':0})))
            base = store.create({'connection':'c','library':'user:0','collection':'original','name':'Original'})
            entity = {'key':'ABC','kind':'document','parent_key':'P','title':'Keep title','hash':sha}
            store.sync(base['id'], {'name':'Original','include_children':True,'expected_revision':0,
                                   'complete':True,'entities':[entity]})
            before = store.snapshot([base['id']])
            entries = [{'key':'ABC','status':'ready','hash':sha}]
            args = SimpleNamespace(root=root, snapshot=path, data_dir=root, attachment_root=None,
                connection='c', report=root/'report/import.json', apply=True)
            with patch('scripts.import_zotero_library.inventory', return_value=(entries, {
                    'ABC':(path,{**entity,'title':'Changed'})})):
                report = run(args)
            self.assertEqual(report['unique_pdfs'], 1)
            self.assertEqual(store.snapshot([base['id']]), before)
            self.assertEqual(store.snapshot([report['base_id']])['entities'][0]['data']['title'], 'Keep title')
            self.assertTrue((root/'report/before-import-backup.zip').is_file())

    def test_index_acceptance_checks_counts_pages_and_omits_quotes(self):
        from unittest.mock import MagicMock
        store = MagicMock(spec=KnowledgeStore)
        store.snapshot.return_value = {'bases':[{'revision':1}], 'entities':[
            {'data':{'kind':'document','hash':'abc'}}]}
        con = MagicMock()
        con.execute.return_value = [('abc', encoded([{'page':1,'text':'source text'}]),
            encoded({'total_pages':1,'native_pages':1,'empty_pages':0,'low_text_pages':0}))]
        store.read.return_value.__enter__.return_value = con
        service = MagicMock()
        unified = MagicMock()
        unified.collection_name = 'test_collection'
        unified.client.count.return_value.count = 1
        service.get_index.return_value = SimpleNamespace(unified_error=None, unified_index=unified,
            chunks=[SimpleNamespace(metadata={'source':'abc.pdf'})])
        hit = {'source_id':'abc.pdf','page':1,'quote':'source text',
               'retrieval_evidence_id':'evidence','url':'zotero://open-pdf/library/items/ABC?page=1'}
        service.search.return_value = {'backend':'multimodal','warning':None,'results':[hit]}
        with patch('scripts.verify_zotero_library_index.KnowledgeStore', return_value=store), \
             patch('scripts.verify_zotero_library_index.KnowledgeService', return_value=service), \
             patch('scripts.verify_zotero_library_index.stored_service_context', return_value=nullcontext()):
            report = verify(Path('.'), 'c', 'base')
        self.assertTrue(report['valid'])
        self.assertEqual(len(report['probes']),12)
        self.assertNotIn('source text',encoded(report))
        self.assertTrue(report['probes'][0]['hits'][0]['source_page_verified'])

    def test_index_acceptance_refuses_lexical_fallback(self):
        from unittest.mock import MagicMock
        store, service = MagicMock(spec=KnowledgeStore), MagicMock()
        store.snapshot.return_value = {'entities':[]}
        store.read.return_value.__enter__.return_value.execute.return_value = []
        service.get_index.return_value = SimpleNamespace(unified_error='unavailable')
        with patch('scripts.verify_zotero_library_index.KnowledgeStore', return_value=store), \
             patch('scripts.verify_zotero_library_index.KnowledgeService', return_value=service), \
             patch('scripts.verify_zotero_library_index.stored_service_context', return_value=nullcontext()):
            with self.assertRaises(RuntimeError):
                verify(Path('.'), 'c', 'base')


if __name__ == '__main__':
    unittest.main()
