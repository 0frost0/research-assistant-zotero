"""Retrieval lifecycle regressions; synthetic data, no external services."""
import io
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from research_assistant.core.service_context import service_context
from research_assistant.knowledge.service import KnowledgeService, IndexBuilding
from research_assistant.retrieval.embeddings.api_embeddings import QwenAPIEmbeddings
from tests import test_knowledge


class SearchReliabilityTests(unittest.TestCase):
    def test_content_cache_survives_failure_restart_and_respects_model_and_role(self):
        config = {"base_url": "http://127.0.0.1:30001/v1", "model": "test",
                  "api_key": "secret", "dimension": 2}
        response = lambda: io.BytesIO(b'{"data":[{"embedding":[3,4]}]}')
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "vectors.sqlite3"
            first = QwenAPIEmbeddings(config, cache_path=path)
            first.opener = Mock()
            first.opener.open.side_effect = [response(), OSError("connection lost")]
            with self.assertRaises(RuntimeError):
                first.embed_texts(["结构化数据采用固定字段", "第二段"])
            resumed = QwenAPIEmbeddings(config, cache_path=path)
            resumed.opener = Mock()
            resumed.opener.open.side_effect = lambda *a, **k: response()
            self.assertEqual(resumed.embed_texts(["结构化数据采用固定字段", "第二段"]), [[.6,.8],[.6,.8]])
            self.assertEqual(resumed.opener.open.call_count, 1)
            resumed.embed_query("结构化数据采用固定字段")
            self.assertEqual(resumed.opener.open.call_count, 2)  # query instruction differs
            changed = QwenAPIEmbeddings({**config, "revision": "2"}, cache_path=path)
            changed.opener = resumed.opener
            changed.embed_texts(["结构化数据采用固定字段"])
            self.assertEqual(resumed.opener.open.call_count, 3)

    def test_background_build_returns_promptly_single_writer_and_keeps_context(self):
        fixture = test_knowledge.KnowledgeTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.sync(fixture.a, [fixture.doc])
        service = KnowledgeService(fixture.store, "multimodal")
        started, release, finished = threading.Event(), threading.Event(), threading.Event()
        seen = []
        index = Mock(unified_error=None)
        def build(*args):
            seen.append(service_context.get()["connection"])
            started.set()
            release.wait(5)
            finished.set()
            return index
        token = service_context.set({"connection": "fixture", "config": {}})
        self.addCleanup(service_context.reset, token)
        with patch.object(service, "_build_index", side_effect=build) as mocked:
            begin = time.monotonic()
            with self.assertRaises(IndexBuilding):
                service.get_index({"one.pdf"}, background=True)
            self.assertLess(time.monotonic()-begin, .5)
            self.assertTrue(started.wait(1))
            with self.assertRaises(IndexBuilding):
                service.get_index({"another.pdf"}, background=True)
            self.assertEqual(mocked.call_count, 1)
            release.set()
            self.assertTrue(finished.wait(1))
            deadline = time.monotonic()+2
            while service._building and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertIs(service.get_index({"one.pdf"}, background=True), index)
            self.assertEqual(seen, ["fixture"])

    def test_switching_scopes_reuses_ready_index(self):
        fixture = test_knowledge.KnowledgeTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        fixture.sync(fixture.a, [fixture.doc])
        service = fixture.service
        with patch.object(service, "_build_index", side_effect=lambda *a: Mock(unified_error=None)) as build:
            original = service.get_index({"original.pdf"})
            service.get_index({"notes.md"})
            self.assertIs(service.get_index({"original.pdf"}), original)
            self.assertEqual(build.call_count, 2)


if __name__ == "__main__":
    unittest.main()
