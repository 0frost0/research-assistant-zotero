"""Isolated synthetic pixels/contracts, not visual-model quality measurements."""
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch, AsyncMock

from PIL import Image
from starlette.applications import Starlette
from starlette.testclient import TestClient

from research_assistant.core.service_context import service_context
from research_assistant.knowledge.jobs import QAJobStore
from research_assistant.knowledge.qa import (EvidenceQA, ModelCaller, MultimodalDraft,
    grounded_claims, merge_candidates, select_multimodal_candidates)
from research_assistant.knowledge.service import KnowledgeService
from research_assistant.knowledge.store import KnowledgeStore, digest
from research_assistant.memory.store import Conflict
from research_assistant.knowledge.visual_evidence import VisualEvidenceRepository
from research_assistant.retrieval.evidence_groups import attach_evidence_groups
from research_assistant.retrieval.knowledge_base import SearchResult
from research_assistant.web.knowledge_api import KnowledgeAPI
from tests.test_knowledge_qa import Service, ScriptedModel, SUPPORT, hit
from tests.test_reading_memory import pdf


def visual(ident="V1", **extra):
    return hit("", modality="image", retrieval_evidence_id="atomic-" + ident,
               visual_caption="Synthetic chart caption", visual_asset={
                   "asset_id": ident, "source_id": "paper.pdf", "page": 2,
                   "image_hash": "hash", "document_version": "version"}, **extra)


def visual_answer(payload):
    ident = next(e["evidence_id"] for e in payload["evidence"] if e.get("modality") == "image")
    return {"claims": [{"text": "图中蓝柱高于橙柱。", "citations": [
        {"evidence_id": ident, "citation_type": "visual", "observation": "蓝柱较高"}]}]}


class VisualRepositoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = KnowledgeStore(self.root)
        self.repo = VisualEvidenceRepository(self.store)
        self.a = self.store.create({"connection": "test", "library": "user:0", "collection": "A", "name": "A"})
        self.b = self.store.create({"connection": "test", "library": "user:0", "collection": "B", "name": "B"})
        path = self.root / "fixture.pdf"
        pdf(path)
        self.sha = digest(path.read_bytes())
        self.store.put_object(self.sha, path.read_bytes())
        self.store.sync(self.a["id"], {"expected_revision": 0, "name": "A", "include_children": True,
            "complete": True, "entities": [{"key": "PDF00001", "kind": "document", "title": "Synthetic", "hash": self.sha}]})
        self.image = self.store.home / "mineru" / self.sha / "figure.png"
        self.image.parent.mkdir(parents=True)
        Image.new("RGB", (60, 40), "blue").save(self.image)
        with self.store.transaction() as con:
            report = json.loads(con.execute("SELECT report FROM objects WHERE hash=?", (self.sha,)).fetchone()[0])
            report["mineru"] = {"blocks": [{"text": "Synthetic chart caption", "metadata": {
                "page": 1, "bbox": [0, 0, 60, 40], "content_type": "image", "image_path": str(self.image)}}]}
            con.execute("UPDATE objects SET report=? WHERE hash=?", (json.dumps(report), self.sha))
        self.ids = [self.a["id"]]

    def test_scoped_hash_identity_no_path_and_stale_bytes(self):
        entry = self.repo.catalog(self.ids)[0]
        encoded = self.repo.encode(self.ids, entry["asset_id"])
        self.assertTrue(encoded["data_url"].startswith("data:image/png;base64,"))
        self.assertNotIn(str(self.image), json.dumps(encoded))
        with self.assertRaises(KeyError):
            self.repo.encode([self.b["id"]], entry["asset_id"])
        Image.new("RGB", (60, 40), "orange").save(self.image)
        with self.assertRaises(KeyError):
            self.repo.encode(self.ids, entry["asset_id"])
        with self.assertRaises(ValueError):
            self.repo._encode_entry(entry)

    def test_path_allowlist_and_pixel_budget(self):
        outside = self.root / "outside.png"
        Image.new("RGB", (5, 5)).save(outside)
        with self.assertRaises(ValueError):
            self.repo._safe_path(outside)
        with patch("research_assistant.knowledge.visual_evidence.MAX_IMAGE_PIXELS", 10):
            with self.assertRaises(ValueError):
                self.repo.encode(self.ids, self.repo.catalog(self.ids)[0]["asset_id"])
        with self.assertRaises(ValueError):
            self.repo.catalog([])

    def test_explicit_page_fallback_resolves_and_cannot_cross_scope(self):
        try:
            import fitz  # noqa: F401
        except ImportError:
            with self.assertRaises(ValueError):
                self.repo.render_page(self.ids, self.sha + ".pdf", 1)
            return
        page = self.repo.render_page(self.ids, self.sha + ".pdf", 1)
        self.assertEqual(page["asset_type"], "pdf_page")
        self.assertEqual(self.repo.encode(self.ids, page["asset_id"])["image_hash"], page["image_hash"])
        with self.assertRaises(ValueError):
            self.repo.render_page([self.b["id"]], self.sha + ".pdf", 1)
        with self.assertRaises(KeyError):
            self.repo.encode([self.b["id"]], page["asset_id"])
        with self.assertRaises(ValueError):
            self.repo.render_page(self.ids, self.sha + ".pdf", True)

    def test_authenticated_api_read_and_validation(self):
        api = KnowledgeAPI(self.root, token="fixture", backend="tfidf")
        client = TestClient(Starlette(routes=api.routes()))
        asset_id = self.repo.catalog(self.ids)[0]["asset_id"]
        body = {"kb_ids": self.ids, "asset_id": asset_id}
        self.assertEqual(client.post("/api/knowledge/visual", json=body).status_code, 401)
        headers = {"Authorization": "Bearer fixture"}
        self.assertEqual(client.post("/api/knowledge/visual", json=body, headers=headers).status_code, 200)
        self.assertEqual(client.post("/api/knowledge/visual", json={**body, "kb_ids": [self.b["id"]]}, headers=headers).status_code, 404)
        for flag in ("true", 1):
            self.assertEqual(client.post("/api/knowledge/ask-async", json={"query": "chart", "kb_ids": self.ids,
                "multimodal": flag}, headers=headers).status_code, 400)

    def test_search_binds_active_image_hash_not_just_same_page(self):
        asset = self.repo.catalog(self.ids)[0]
        identity = {"source": self.sha + ".pdf", "page": 1, "bbox_json": "[0, 0, 60, 40]",
                    "image_path": str(self.image), "image_hash": asset["image_hash"], "document_version": "index-version"}
        index = Mock(unified_error=None)
        index.unified_index.store.active_visual_identity.return_value = identity
        index.search_results.return_value = [SearchResult(source=self.sha + ".pdf", page=1, quote="", score=.9,
            backend="fixture", chunk_id=1, evidence_id="atomic-image", modality="image", bbox=[0, 0, 60, 40],
            image_path=str(self.image), visual_caption="caption")]
        service = KnowledgeService(self.store)
        with patch.object(service, "get_index", return_value=index):
            data = {"query": "chart", "kb_ids": self.ids, "scope": "original", "multimodal": True}
            found = service.search(data)["results"]
            self.assertEqual(found[0]["visual_asset"]["asset_id"], asset["asset_id"])
            self.assertNotIn(str(self.image), json.dumps(found))
            identity["image_hash"] = "stale"
            self.assertEqual(service.search(data)["results"], [])

    def test_real_memory_vector_index_to_scoped_pixels(self):
        from langchain_core.documents import Document
        from qdrant_client import QdrantClient
        from research_assistant.retrieval.unified_index import UnifiedMultimodalIndex
        from research_assistant.retrieval.knowledge_base import LiteratureIndex, split_documents
        from research_assistant.retrieval.retrieval_policy import EvidenceContext
        class Embeddings:
            model_name, dimension = "synthetic-visual-contract", 2
            def embed_texts(self, texts): return [[1., 0.] for _ in texts]
            def embed_images(self, paths, captions=None): return [[1., 0.] for _ in paths]
            def embed_query(self, query): return [1., 0.]
        client = QdrantClient(":memory:")
        self.addCleanup(client.close)
        unified = UnifiedMultimodalIndex(embeddings=Embeddings(), evidence_path=self.root / "e.sqlite3", client=client)
        documents = [Document(page_content="Synthetic chart caption", metadata={"source": self.sha + ".pdf",
            "file_type": ".pdf", "page": 1, "content_type": "image", "bbox": [0, 0, 60, 40], "image_path": str(self.image)}),
            Document(page_content="Blue bar is higher in this synthetic example.", metadata={
                "source": self.sha + ".pdf", "file_type": ".pdf", "page": 1, "content_type": "text"})]
        index = LiteratureIndex.__new__(LiteratureIndex)
        index.documents, index.chunks = documents, split_documents(documents)
        index.evidence_context = EvidenceContext(documents, index.chunks)
        index.unified_index, index.lexical_index, index.unified_error = unified, None, None
        index.backend = "multimodal"
        unified.sync(index.chunks)
        service = KnowledgeService(self.store, backend="multimodal")
        with patch.object(service, "get_index", return_value=index):
            found = service.search({"query": "chart", "kb_ids": self.ids, "scope": "original",
                "multimodal": True, "retrieval_mode": "hybrid", "k": 6})
        hits = found["results"]
        self.assertEqual({h["modality"] for h in hits}, {"text", "image"})
        self.assertEqual(len({h["evidence_group_id"] for h in hits}), 1)
        evidence = select_multimodal_candidates(hits, "selected")
        image = next(h for h in evidence if h["modality"] == "image")
        self.assertTrue(image["retrieval_evidence_id"])
        self.assertTrue(service.visuals.encode(self.ids, image["evidence_id"])["data_url"])
        self.assertEqual(image["linked_evidence_ids"], [next(h["evidence_id"] for h in evidence if h["modality"] == "text")])


class VisualQATests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = KnowledgeStore(Path(self.temp.name))
        self.base = self.store.create({"connection": "test", "library": "user:0", "collection": "A", "name": "A"})
        self.data = {"query": "比较图中两根柱", "kb_ids": [self.base["id"]], "strategy": "selected", "multimodal": True}
        self.service = Service(self.store, [[hit(), visual()]])
        self.service.visuals = Mock()
        self.service.visuals.encode.return_value = {**visual()["visual_asset"], "data_url": "data:image/png;base64,cGl4ZWxz"}
        self.service.visuals.resolve.return_value = visual()["visual_asset"]
        token = service_context.set({"connection": "test", "config": {"llm": {
            "base_url": "https://example.test/v1", "model": "fixture-vl", "api_key": "test", "supports_images": True}}})
        self.addCleanup(service_context.reset, token)

    async def run_qa(self, answers):
        self.model = ScriptedModel(answers)
        return await EvidenceQA(self.service, self.model, configured=lambda: True).ask(self.data)

    async def test_answer_review_share_pixels_without_persisting_pixels(self):
        result = await self.run_qa([visual_answer, SUPPORT])
        self.assertEqual(result["status"], "answered")
        self.assertEqual(self.model.payloads[0]["_visual_inputs"], self.model.payloads[1]["_visual_inputs"])
        cite = result["claims"][0]["citations"][0]
        self.assertEqual(cite["observation_source"], "model_derived")
        self.assertNotIn("support_quote", cite)
        self.assertNotIn("data:image", json.dumps(result))
        jobs = QAJobStore(Path(self.temp.name) / "jobs.sqlite3")
        job = jobs.create("ask", self.data)
        jobs.finish(job["job_id"], "succeeded", result=result)
        feedback = jobs.record_feedback(job["job_id"], {"rating": "helpful"})
        saved = jobs.list_feedback()[0]["snapshot"]["claims"][0]["citations"][0]
        self.assertEqual(saved["citation_type"], "visual")
        self.assertEqual(saved["visual_asset"]["asset_id"], "V1")
        with self.store.transaction() as con:
            telemetry = con.execute("SELECT metrics FROM qa_runs").fetchone()[0]
        self.assertNotIn("data:image", telemetry)
        self.assertNotIn("蓝柱较高", telemetry)

    async def test_unconfirmed_capability_sends_nothing(self):
        service_context.get()["config"]["llm"]["supports_images"] = False
        result = await self.run_qa([])
        self.assertEqual(result["status"], "model_unavailable")
        self.assertEqual(self.model.calls, [])
        self.service.visuals.encode.assert_not_called()

    async def test_missing_or_changed_asset_cannot_ground_visual_claim(self):
        self.service.visuals.encode.return_value["image_hash"] = "changed"
        claim = {"claims": [{"text": "untrusted", "citations": [{"evidence_id": "V1", "citation_type": "visual", "observation": "untrusted"}]}]}
        result = await self.run_qa([claim])
        self.assertEqual(result["claims"], [])
        self.assertNotIn("data:image", json.dumps(result))
        self.assertEqual(self.service.visuals.encode.call_count, 1)
        self.assertEqual(self.model.calls, ["answer"])
        self.assertEqual(self.model.payloads[0]["_visual_inputs"], [])

    async def test_review_failure_never_returns_visual_claim(self):
        result = await self.run_qa([visual_answer, RuntimeError("provider failure")])
        self.assertEqual(result["claims"], [])
        self.assertEqual(result["status"], "unavailable")

    async def test_scope_rejected_review_hides_visual_claim(self):
        result = await self.run_qa([visual_answer, {**SUPPORT, "question_relevant": False}])
        self.assertEqual(result["claims"], [])
        self.assertEqual(result["status"], "no_evidence")

    async def test_parser_representation_changed_during_review_raises_conflict(self):
        def changed(payload):
            self.store.save_note(self.base["id"], {"title": "synthetic", "body": "fixture", "confirmed": True})
            return SUPPORT
        with self.assertRaises(Conflict):
            await self.run_qa([visual_answer, changed])

    async def test_model_caller_uses_image_blocks_not_json_encoded_pixels(self):
        model = Mock()
        model.with_structured_output.return_value.ainvoke = AsyncMock(return_value={"parsed": {"claims": []}})
        with patch("research_assistant.agents.research_agent.build_chat_model", return_value=model):
            await ModelCaller()("answer", {"multimodal": True, "evidence": [], "_visual_inputs": [
                {"evidence_id": "V1", "data_url": "data:image/png;base64,cGl4ZWxz"}]}, 5)
        messages = model.with_structured_output.return_value.ainvoke.call_args.args[0]
        content = messages[1].content
        self.assertEqual(content[-1]["type"], "image_url")
        self.assertNotIn("data:image", content[0]["text"])
        self.assertIn("V1", content[-2]["text"])


class VisualContractTests(unittest.TestCase):
    def test_distinct_empty_quotes_survive_and_pixels_are_bounded(self):
        pool = merge_candidates([visual("V1"), visual("V2"), visual("V3")], [])
        self.assertEqual(len(pool), 3)
        self.assertEqual(len(select_multimodal_candidates(pool, "selected")), 2)

    def test_forged_visual_ids_and_fake_quotes_fail_local_validation(self):
        evidence = select_multimodal_candidates([visual()], "selected")
        for citation in ({"evidence_id": "V1", "quote": "invented pixel quote"},
                         {"evidence_id": "foreign", "citation_type": "visual", "observation": "blue"}):
            draft = MultimodalDraft.model_validate({"claims": [{"text": "claim", "citations": [citation]}]})
            self.assertEqual(grounded_claims(draft, evidence, ["V1"]), [])

    def test_groups_do_not_cross_source_version_or_unknown_coordinates(self):
        rows = [hit("caption", retrieval_evidence_id="T1", document_version="v1"),
                visual(document_version="v1", bbox=[0, 0, 10, 10]),
                hit("stale", retrieval_evidence_id="T2", document_version="v2")]
        attach_evidence_groups(rows)
        self.assertEqual(rows[0]["evidence_group_id"], rows[1]["evidence_group_id"])
        self.assertNotEqual(rows[0]["evidence_group_id"], rows[2]["evidence_group_id"])
        self.assertEqual(rows[1]["evidence_relations"], [{"type": "same_page", "evidence_id": "T1"}])
