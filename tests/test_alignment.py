"""Deterministic service tests: cache/version safety, errors and read-only memory."""
import json
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock,patch
from research_assistant.memory.store import ReadingStore
from research_assistant.translation.alignment import AlignmentService
from tests.test_reading_memory import pdf


class AlignmentTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.root=Path(self.temp.name)
        self.file=self.root/'paper.pdf';pdf(self.file)
        self.store=ReadingStore(self.root)
        p=self.store.register(self.file,'paper.pdf')
        self.paper=p['id'];self.original=self.store.artifact(p['original_id'])
        self.translated=self.store.add_translation(self.paper,p['original_id'],self.file,{}, {'ok':True})
        self.anchor={'artifact_id':self.translated['id'],'artifact_hash':self.translated['hash'],'page':1,
                     'coordinate_system':'pdf_user_space','view_box':[0.,0.,600.,800.],
                     'rects':[[60,680,180,705]],'excerpt':'证据'}
        output=dict(self.anchor,artifact_id=self.original['id'],artifact_hash=self.original['hash'],excerpt='evidence')
        self.runner=Mock(return_value={'status':'aligned','alignment_unit':'sentence','anchors':[output],'message':'local model'})
        self.service=AlignmentService(self.store,runner=self.runner)
        self.ready=patch.object(self.service,'status',return_value={'available':True});self.ready.start()
        self.data={'paper_id':self.paper,'anchor':self.anchor}

    def tearDown(self):
        self.ready.stop();self.temp.cleanup()

    def test_cache_and_no_notes(self):
        self.assertFalse(self.service.align(self.data)['cached'])
        self.assertTrue(self.service.align(self.data)['cached'])
        self.assertEqual(self.runner.call_count,1)
        self.assertEqual(self.store.notes(self.paper),[])
        self.assertEqual(self.store.annotations(self.paper),[])

    def test_cache_survives_restart(self):
        self.service.align(self.data)
        service=AlignmentService(self.store,runner=Mock(side_effect=AssertionError('no inference')))
        self.assertTrue(service.align(self.data)['cached'])

    def test_hash_checked_on_hit(self):
        self.service.align(self.data)
        self.store.artifact_path(self.original['id']).write_bytes(b'corrupt')
        with self.assertRaises(ValueError):self.service.align(self.data)

    def test_translation_version_isolated(self):
        self.service.align(self.data)
        other=self.store.add_translation(self.paper,self.original['id'],self.file,{'new':True},{'ok':True})
        data={'paper_id':self.paper,'anchor':dict(self.anchor,artifact_id=other['id'])}
        self.assertFalse(self.service.align(data)['cached'])
        self.assertEqual(self.runner.call_count,2)

    def test_reject_wrong_original(self):
        self.runner.return_value['anchors'][0]['artifact_id']=self.translated['id']
        with self.assertRaises(ValueError):self.service.align(self.data)

    def test_no_queue(self):
        self.service.lock.acquire()
        try:self.assertEqual(self.service.align(self.data)['status'],'busy')
        finally:self.service.lock.release()
        self.runner.assert_not_called()

    def test_timeout_no_automatic_retry(self):
        self.runner.side_effect=subprocess.TimeoutExpired('alignment',75)
        self.assertEqual(self.service.align(self.data)['status'],'timeout')
        self.assertEqual(self.runner.call_count,1)
        self.assertFalse(self.service.lock.locked())

    def test_cache_corruption_rebuilt(self):
        self.service.align(self.data)
        with self.store.transaction() as con:con.execute("UPDATE alignment_results SET payload='broken'")
        self.assertFalse(self.service.align(self.data)['cached'])
        self.assertEqual(self.runner.call_count,2)

    def test_unmatched_does_not_create_anchors(self):
        self.runner.return_value={'status':'unmatched','message':'ambiguous'}
        self.assertNotIn('anchors',self.service.align(self.data))
        self.assertTrue(self.service.align(self.data)['cached'])

    def test_unavailable_does_not_affect_notes(self):
        self.ready.stop()
        self.assertEqual(self.service.align(self.data)['status'],'unavailable')
        self.assertEqual(self.store.notes(self.paper),[])


if __name__=='__main__':unittest.main()
