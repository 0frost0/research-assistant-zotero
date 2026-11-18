import base64
import json
from pathlib import Path
import runpy
import sys
import tempfile
import unittest
from unittest.mock import patch

from starlette.testclient import TestClient

from benchmarks.check_visual_readiness import probe
from benchmarks.visual_fixture import (chart_png, taller_color, seed_visual_report, visual_service,
                                       write_visual_pdf, BoundedVisualCaller)
from research_assistant.knowledge.store import KnowledgeStore, digest
from tests.test_reading_memory import pdf


CONFIG = {"llm": {"model": "fixture", "base_url": "https://fixture.invalid/v1",
                  "api_key": "do-not-leak", "supports_images": True}}


class ReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def test_native_live_budget_and_scope_fail_closed(self):
        from unittest.mock import AsyncMock
        with tempfile.TemporaryDirectory() as directory:
            delegate = AsyncMock(return_value=({"claims": []}, {"input_tokens": 12, "output_tokens": 3}))
            path = Path(directory) / "usage.json"
            caller = BoundedVisualCaller(delegate, path)
            pixels = {"multimodal": True, "_visual_inputs": [{"data_url": "secret-pixels"}]}
            for role, payload in (("answer", {}), ("rewrite", pixels)):
                with self.assertRaises(ValueError):
                    await caller(role, payload, 1)
            await caller("answer", pixels, 1)
            await caller("review", pixels, 1)
            with self.assertRaises(ValueError):
                await caller("answer", pixels, 1)
            report = json.loads(path.read_text())
            self.assertEqual(delegate.await_count, 2)
            self.assertEqual(report["input_tokens"], 24)
            self.assertEqual(report["unknown_usage_calls"], 0)
            self.assertNotIn("secret-pixels", path.read_text())

    async def test_dry_run_and_undeclared_never_call(self):
        async def forbidden(*args):
            self.fail("Unexpected provider request")
        result = await probe(CONFIG, caller=forbidden)
        self.assertEqual(result["status"], "not_verified")
        for flag in (False, "true", 1):
            result = await probe({"llm": {**CONFIG["llm"], "supports_images": flag}}, live=True, caller=forbidden)
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["model_calls"], 0)

    async def test_contrastive_pixels_and_positive_negative_review_six_calls(self):
        roles = []
        async def caller(role, payload, timeout):
            roles.append(role)
            color = taller_color(payload["_visual_inputs"][0]["data_url"])
            if role == "answer":
                return {"claims": [{"text": color, "citations": [{"evidence_id": "Vfixture",
                    "citation_type": "visual", "observation": color}]}]}, {}
            return {"question_relevant": True, "verdicts": [{"claim_index": 0,
                "supported": payload["claims"][0]["text"] == color, "reason": "synthetic"}]}, {}
        result = await probe(CONFIG, live=True, caller=caller)
        self.assertEqual(result["status"], "synthetic_probe_passed")
        self.assertEqual(roles, ["answer", "review", "review"] * 2)
        self.assertEqual(result["model_calls"], 6)
        self.assertEqual(result["unknown_usage_calls"], 6)
        self.assertNotIn("data:image", json.dumps(result))
        self.assertNotIn("do-not-leak", json.dumps(result))

    async def test_failure_is_bounded_and_sanitized(self):
        async def failing(*args):
            raise RuntimeError("do-not-leak provider response")
        result = await probe(CONFIG, live=True, caller=failing)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["model_calls"], 1)
        self.assertNotIn("do-not-leak", json.dumps(result))

    async def test_unsafe_endpoint_is_blocked_before_call(self):
        async def forbidden(*args):
            self.fail("Unsafe endpoint called")
        for url in ("http://remote.invalid/v1", "https://user:password@fixture.invalid/v1", "https://fixture.invalid/v1?key=secret"):
            result = await probe({"llm": {**CONFIG["llm"], "base_url": url}}, live=True, caller=forbidden)
            self.assertEqual(result["status"], "blocked")
            self.assertEqual(result["model_calls"], 0)

    async def test_always_supporting_review_cannot_pass(self):
        async def caller(role, payload, timeout):
            if role == "answer":
                color = taller_color(payload["_visual_inputs"][0]["data_url"])
                return {"claims": [{"text": color, "citations": [{"evidence_id": "Vfixture",
                    "citation_type": "visual", "observation": color}]}]}, {}
            return {"question_relevant": True, "verdicts": [{"claim_index": 0,
                "supported": True, "reason": "uncritical review"}]}, {}
        result = await probe(CONFIG, live=True, caller=caller)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(result["pixel_discrimination"], "passed")
        self.assertFalse(result["cases"][0]["negative_review"])

    async def test_text_only_or_malformed_structure_is_not_pass(self):
        async def malformed(*args):
            return {"claims": [{"text": "blue", "citations": [{"evidence_id": "Vfixture", "quote": "blue"}]}]}, {}
        result = await probe(CONFIG, live=True, caller=malformed)
        self.assertNotEqual(result["status"], "synthetic_probe_passed")


class FixtureTests(unittest.TestCase):
    def test_visual_pdf_is_reproducible_and_contains_chart(self):
        from pypdf import PdfReader
        with tempfile.TemporaryDirectory() as directory:
            a, b = Path(directory) / "a.pdf", Path(directory) / "b.pdf"
            write_visual_pdf(a)
            write_visual_pdf(b)
            self.assertEqual(a.read_bytes(), b.read_bytes())
            page = PdfReader(a).pages[0]
            self.assertIn(b"35 25 61 126 re f", page.get_contents().get_data())
            self.assertIn("Retrieval needs careful validation", page.extract_text())

    def test_scripted_server_notes_and_visual_never_use_real_model(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.object(sys, "argv", ["serve_knowledge_fixture.py", "--qa-scripted", "--visual-fixture", "--root", directory]), \
                    patch("research_assistant.knowledge.qa.ModelCaller.__call__", side_effect=AssertionError("No real provider allowed")):
                module = runpy.run_path(str(Path(__file__).resolve().parents[1] / "benchmarks/serve_knowledge_fixture.py"))
                api = module["api"]
                base = next(b for b in api.store.bases() if b["collection"] == "A")
                note = api.store.save_note(base["id"], {"title": "Trial memory", "body": "Personal synthetic memory: the trial size is seven cases.", "confirmed": True})
                headers = {"Authorization": "Bearer synthetic-acceptance-token", "X-Research-Connection": "fixture"}
                with TestClient(module["app"]) as client:
                    result = client.post("/api/knowledge/ask", headers=headers, json={"kb_ids": [base["id"]], "scope": "notes",
                        "query": "In my personal synthetic memory, what is the trial size?"}).json()
                    self.assertEqual(result["status"], "answered")
                    self.assertEqual(result["claims"][0]["citations"][0]["entity_id"], note["id"])
                    result = client.post("/api/knowledge/ask", headers=headers, json={"kb_ids": [base["id"]], "scope": "original",
                        "multimodal": True, "query": "Which bar is taller in the synthetic two-bar chart?"}).json()
                    self.assertEqual(result["status"], "answered")
                    self.assertEqual(result["claims"][0]["citations"][0]["observation"], "blue")
                for index, _ in api.service._indexes.values():
                    if index and index.unified_index:
                        index.unified_index.client.close()

    def test_contrast_changes_only_pixels_not_caption(self):
        for flag, color in ((True, "blue"), (False, "orange")):
            self.assertEqual(taller_color("data:image/png;base64," + base64.b64encode(chart_png(flag)).decode()), color)

    def test_real_service_scoped_asset_via_local_vector_index(self):
        with tempfile.TemporaryDirectory() as directory:
            store = KnowledgeStore(Path(directory))
            source = Path(directory) / "synthetic.pdf"
            pdf(source)
            sha = digest(source.read_bytes())
            store.put_object(sha, source.read_bytes())
            seed_visual_report(store, sha)
            base = store.create({"connection": "fixture", "library": "user:0", "collection": "A", "name": "A"})
            store.sync(base["id"], {"expected_revision": 0, "name": "A", "include_children": True, "complete": True,
                "entities": [{"key": "PDF00001", "kind": "document", "hash": sha, "title": "Synthetic"}]})
            service = visual_service(store)
            found = service.search({"query": "synthetic two-bar chart", "kb_ids": [base["id"]],
                                    "scope": "original", "multimodal": True})
            image = next(hit for hit in found["results"] if hit["modality"] == "image")
            encoded = service.visuals.encode([base["id"]], image["visual_asset"]["asset_id"])
            self.assertEqual(taller_color(encoded["data_url"]), "blue")
            service.index.unified_index.client.close()
            store.save_note(base["id"], {"title": "Memory", "body": "seven cases", "confirmed": True})
            notes = service.search({"query": "seven cases", "kb_ids": [base["id"]], "scope": "notes"})
            self.assertTrue(notes["results"])
            self.assertIsNone(service.index.unified_index)
