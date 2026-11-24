"""Measurement validity, not retrieval quality or synthetic gold labels."""
import json
import unittest
from unittest.mock import Mock, MagicMock, patch

from benchmarks.benchmark_zotero_search_latency import DEFAULT_QUERIES, local_opener, request_search


class SearchLatencyTests(unittest.TestCase):
    def test_chinese_roundtrip_and_scope_are_required(self):
        payload = {"query": DEFAULT_QUERIES["heart_failure_original"], "k": 20}
        good = {"query": payload["query"], "revisions": {"base": 1}, "backend": "multimodal-hybrid",
                "results": [{"kind": "original", "modality": "text", "source_id": "a.pdf",
                             "entity_id": "entity", "revision": 2}]}
        for alteration, success in [({}, True), ({"query": "?????"}, False),
                                    ({"revisions": {"base": 2}}, False),
                                    ({"results": [{**good["results"][0], "source_id": "outside.pdf"}]}, False)]:
            with self.subTest(alteration=alteration):
                response = Mock(status=200)
                response.read.return_value = json.dumps({**good, **alteration}).encode()
                opener = Mock()
                opener.open.return_value.__enter__ = Mock(return_value=response)
                opener.open.return_value.__exit__ = Mock(return_value=False)
                row = request_search("http://127.0.0.1:8790/api/knowledge/search", {}, payload,
                                     opener=opener, revisions={"base": 1}, sources={("a.pdf", "entity", 2)})
                self.assertEqual(row["status"] == "completed", success)
                sent = json.loads(opener.open.call_args.args[0].data)
                self.assertEqual(sent["query"], "什么是心衰")
                self.assertNotIn("a.pdf", json.dumps(row))

    def test_errors_are_counted_without_provider_text(self):
        opener = Mock()
        opener.open.side_effect = TimeoutError("PRIVATE")
        row = request_search("http://127.0.0.1/api/knowledge/search", {}, {"query": "q"},
                             opener=opener, revisions={}, sources=set())
        self.assertEqual(row["status"], "request_error")
        self.assertEqual(row["error_type"], "TimeoutError")
        self.assertNotIn("PRIVATE", json.dumps(row))

    def test_indexing_is_polled_and_degraded_backend_is_explicit(self):
        opener = Mock()
        waiting = MagicMock(status=202)
        waiting.read.return_value = b'{"status":"indexing","results":[]}'
        ready = MagicMock(status=200)
        ready.read.return_value = b'{"query":"q","revisions":{},"results":[],"backend":"bm25-lexical","warning":"fallback"}'
        for response in (waiting, ready):
            response.__enter__.return_value = response
        opener.open.side_effect = [waiting, ready]
        with patch("benchmarks.benchmark_zotero_search_latency.time.sleep"):
            row = request_search("http://127.0.0.1/api/knowledge/search", {}, {"query": "q", "k": 20},
                                 opener=opener, revisions={}, sources=set())
        self.assertEqual(row["polls"], 2)
        self.assertEqual(row["status"], "completed")
        self.assertFalse(row["hybrid_succeeded"])
        self.assertTrue(row["warning"])

    def test_credentials_only_go_to_loopback_without_redirects(self):
        for endpoint in ("https://example.com", "http://127.0.0.1@other.invalid", "http://127.0.0.1/path"):
            with self.assertRaises(ValueError):
                local_opener(endpoint)
        opener = local_opener("http://127.0.0.1:8790")
        redirect = next(h for h in opener.handlers if hasattr(h, "redirect_request"))
        self.assertIsNone(redirect.redirect_request(None, None, None, None, None, "https://other.invalid"))


if __name__ == "__main__":
    unittest.main()
