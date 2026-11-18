"""Deterministic algorithm/safety regressions; not a scientific quality benchmark."""
from dataclasses import dataclass
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from research_assistant.retrieval.hybrid_retrieval import BM25Index, reciprocal_rank_fusion, tokens
from research_assistant.knowledge.qa import (algorithm_settings, rerank_candidates, select_candidates)
from research_assistant.retrieval.knowledge_base import LiteratureIndex
from research_assistant.retrieval.reranking import RerankerClient, MODEL, REVISION
from deployment.embedding.remote_reranker_service import validate
from tests.test_knowledge_qa import hit


def record(ident, text="heart failure definition", **kw):
    return {"point_id": ident, "text": text, "source": ident + ".pdf", "page": 1,
            "file_type": ".pdf", "content_type": "text", "modality": "text", "score": .8, **kw}


class HybridTests(unittest.TestCase):
    def test_tokenizer_preserves_cjk_and_identifiers(self):
        self.assertIn("心衰", tokens("什么是心衰"))
        self.assertIn("mimic-iii", tokens("MIMIC-III outcomes"))
        self.assertIn("outcome", tokens("outcomes"))

    def test_bm25_filters_before_selection_and_omits_noise(self):
        index = BM25Index([record("a"), record("b", content_type="header"),
                           record("c", modality="image"), record("d", file_type=".md")])
        self.assertEqual([h["point_id"] for h in index.search("heart failure", sources=["a.pdf"])], ["a"])
        self.assertEqual(index.search("heart", sources=[]), [])
        self.assertEqual(index.search("heart", file_types=[]), [])
        self.assertEqual(index.search("unmatched terminology"), [])
        self.assertEqual([h["point_id"] for h in index.search("heart", file_types=["md"])], ["d"])

    def test_bm25_dataclass_and_tie_order(self):
        @dataclass
        class Row:
            point_id: str = "x"
            source: str = "x.pdf"
            file_type: str = ".pdf"
            text: str = "heart failure"
        self.assertEqual(BM25Index([Row()]).search("heart")[0]["point_id"], "x")
        self.assertEqual([h["point_id"] for h in BM25Index([record("z"), record("a")]).search("heart")], ["a", "z"])

    def test_rrf_dedup_preserves_scores_and_does_not_modify_inputs(self):
        dense, lexical = [record("a"), record("a"), record("b")], [record("b", score=15), record("c")]
        before = json.dumps([dense, lexical])
        result = reciprocal_rank_fusion(dense, lexical)
        self.assertEqual(result[0]["point_id"], "b")
        self.assertEqual(result[0]["ranking"]["bm25_score"], 15)
        self.assertEqual(result[0]["ranking"]["dense_score"], .8)
        self.assertEqual(len(result), 3)
        self.assertEqual(json.dumps([dense, lexical]), before)

    def test_complementarity_retains_conflicting_findings_and_exact_quotes(self):
        pool = [hit("The treatment improved survival in cohort A.", source_id="a.pdf"),
                hit("The treatment improved survival in cohort A.", source_id="a.pdf"),
                hit("The treatment did not improve survival in cohort B.", source_id="b.pdf")]
        result = select_candidates(pool, question="treatment survival", selection_mode="complementary")
        self.assertEqual(len(result), 2)
        self.assertIn(pool[2]["quote"], [r["quote"] for r in result])
        self.assertTrue(all(r["quote"] in [p["quote"] for p in pool] for r in result))

    def test_complementarity_respects_budgets_and_skips_notes_images(self):
        pool = [hit("x" * 200), hit("Only a note", kind="user_note"), hit("Only pixels", modality="image"),
                hit("A negated fact is not established.", source_id="short.pdf")]
        result = select_candidates(pool, char_budget=50, selection_mode="complementary")
        self.assertEqual([r["source_id"] for r in result], ["short.pdf"])

    def test_legacy_baseline_and_default_switch(self):
        self.assertEqual(algorithm_settings({}, "fixed"), ("legacy", "legacy"))
        self.assertEqual(algorithm_settings({}, "adaptive"), ("hybrid_rerank", "hybrid"))
        self.assertEqual(algorithm_settings({"algorithm": "legacy"}, "adaptive"), ("legacy", "legacy"))
        with self.assertRaises(ValueError):
            algorithm_settings({"algorithm": "other"}, "adaptive")

    def test_real_index_lexical_route_preserves_page_and_scope(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "a.txt").write_text("Heart failure is a synthetic test syndrome, not a treatment endpoint.")
            (root / "b.txt").write_text("Unrelated material.")
            index = LiteratureIndex(root)
            results = index.search_results("什么是心衰", k=20, text_only=True, retrieval_mode="hybrid", sources=["a.txt"])
            self.assertTrue(results)
            self.assertTrue(all(r.source == "a.txt" and r.evidence_id for r in results))
            self.assertEqual(results[0].ranking["score_kind"], "normalized_rrf_not_probability")
            self.assertIn("not a treatment endpoint", results[0].quote)
            self.assertEqual(index.search_results("heart", text_only=True, retrieval_mode="hybrid", sources=[]), [])

    def test_active_canonical_ids_exclude_old_versions(self):
        from langchain_core.documents import Document
        from qdrant_client import QdrantClient
        from research_assistant.retrieval.unified_index import UnifiedMultimodalIndex
        class Embeddings:
            model_name, dimension = "test-only", 2
            def embed_texts(self, docs): return [[1., 0.] for d in docs]
        with tempfile.TemporaryDirectory() as folder:
            client = QdrantClient(":memory:")
            self.addCleanup(client.close)
            index = UnifiedMultimodalIndex(embeddings=Embeddings(), evidence_path=Path(folder) / "e.sqlite3", client=client)
            index.sync([Document(page_content="OLD heart failure definition", metadata={"source": "a.pdf", "file_type": ".pdf"})])
            old = {r["point_id"] for r in index.store.active_text_records()}
            index.sync([Document(page_content="NEW heart failure definition", metadata={"source": "a.pdf", "file_type": ".pdf"})])
            current = index.store.active_text_records()
            self.assertTrue(current)
            self.assertFalse(old & {r["point_id"] for r in current})
            self.assertTrue(all(r["text"].startswith("NEW") for r in current))

    def test_bm25_can_rescue_evidence_absent_from_dense_list(self):
        from unittest.mock import Mock
        index = LiteratureIndex.__new__(LiteratureIndex)
        index.lexical_index = BM25Index([record("lexical", "A rare exact identifier XYZ789.")])
        index.unified_index = Mock()
        index.unified_index.search.return_value = [record("dense", "A semantically related passage.")]
        index.materialize_unified_results = lambda hits, **kw: hits
        found = index._hybrid_search_results("XYZ789", k=20, sources=None, file_types=None)
        self.assertEqual({h["point_id"] for h in found}, {"lexical", "dense"})
        self.assertTrue(all(h["ranking"]["dense_status"] == "succeeded" for h in found))

    def test_dense_outage_preserves_lexical_candidates_with_explicit_status(self):
        from unittest.mock import Mock
        index = LiteratureIndex.__new__(LiteratureIndex)
        index.lexical_index = BM25Index([record("lexical")])
        index.unified_index = Mock()
        index.unified_index.search.side_effect = RuntimeError("PRIVATE TRANSPORT ERROR")
        index.materialize_unified_results = lambda hits, **kw: hits
        found = index._hybrid_search_results("heart", k=20, sources=["lexical.pdf"], file_types=None)
        self.assertEqual(found[0]["point_id"], "lexical")
        self.assertEqual(found[0]["ranking"]["dense_status"], "fallback")
        self.assertNotIn("PRIVATE", json.dumps(found))

    def test_client_rejects_external_redirectable_endpoint(self):
        for endpoint in ("https://example.com", "http://127.0.0.1:30002/rerank?key=x", "http://u:p@localhost/rerank"):
            with self.assertRaises(ValueError):
                RerankerClient(endpoint, api_key="test")

    def test_client_checks_revision_count_range_without_leaking_body(self):
        from unittest.mock import MagicMock
        client = RerankerClient("http://127.0.0.1:30002/rerank", api_key="test")
        response = MagicMock()
        client.opener = MagicMock()
        client.opener.open.return_value.__enter__.return_value = response
        for payload in ({"model": MODEL, "revision": "wrong", "scores": [.5]},
                        {"model": MODEL, "revision": REVISION, "scores": []},
                        {"model": MODEL, "revision": REVISION, "scores": [float("nan")]},
                        {"model": MODEL, "revision": REVISION, "scores": [True]}):
            response.read.return_value = json.dumps(payload).encode()
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    client.scores("query", ["document"])

    def test_server_input_bounds(self):
        good = {"model": MODEL, "query": "heart failure", "documents": ["definition"], "instruction": "retrieve"}
        self.assertEqual(validate(good)[1], ["definition"])
        for bad in ({**good, "documents": ["x"] * 41}, {**good, "query": 1},
                    {**good, "documents": ["x" * 2001]}, {**good, "model": "other"}):
            with self.assertRaises(ValueError):
                validate(bad)


class RerankingTests(unittest.IsolatedAsyncioTestCase):
    async def test_success_reorders_without_mutation_or_truncation(self):
        class Client:
            def __init__(self, **kw): pass
            def scores(self, query, docs):
                self.assertion = len(docs) <= 40
                return [i / 40 for i in range(len(docs))]
        pool = [hit("complete passage " + str(i), source_id=str(i)) for i in range(45)]
        ranked, trace = await rerank_candidates(pool, "q", enabled=True, client_factory=Client)
        self.assertEqual(ranked[0]["source_id"], "39")
        self.assertEqual(len(ranked), 45)
        self.assertEqual(trace["status"], "succeeded")
        self.assertNotIn("ranking", pool[0])

    async def test_failure_preserves_pool_and_omits_sensitive_error(self):
        class Client:
            def __init__(self, **kw): raise RuntimeError("PRIVATE KEY OR DOCUMENT")
        pool = [hit()]
        ranked, trace = await rerank_candidates(pool, "q", enabled=True, client_factory=Client)
        self.assertIs(ranked, pool)
        self.assertEqual(trace["status"], "fallback")
        self.assertNotIn("PRIVATE", json.dumps(trace))

    async def test_absent_service_is_not_reported_as_success(self):
        with patch.dict("os.environ", {"RESEARCH_RERANKER_URL": ""}):
            _, trace = await rerank_candidates([hit()], "q", enabled=True)
        self.assertEqual(trace["status"], "not_configured")

    async def test_cancellation_does_not_start_generation_or_hide_stop(self):
        import asyncio
        class Client:
            def __init__(self, **kw): raise asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await rerank_candidates([hit()], "q", enabled=True, client_factory=Client)


if __name__ == "__main__":
    unittest.main()
