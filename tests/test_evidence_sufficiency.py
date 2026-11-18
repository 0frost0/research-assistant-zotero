"""Experimental pre-generation flow/safety contracts, not calibrated quality scores."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock, AsyncMock

from research_assistant.core.service_context import service_context
from research_assistant.knowledge.qa import (EvidenceQA, CombinedQA, ModelCaller, Sufficiency,
                                            validate_sufficiency)
from research_assistant.knowledge.store import KnowledgeStore
from research_assistant.memory.store import Conflict
from research_assistant.web.knowledge_api import KnowledgeAPI
from tests.test_knowledge_qa import Service, ScriptedModel, hit, answer, SUPPORT, REJECT
from tests.test_visual_evidence import visual, visual_answer


def sufficient(payload):
    return {"sufficient": True, "evidence_ids": [e["evidence_id"] for e in payload["evidence"]], "gaps": []}


def insufficient(payload):
    return {"sufficient": False, "evidence_ids": [], "gaps": ["missing condition"],
            "followup_query": "missing condition alternate"}


class SufficiencyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = KnowledgeStore(Path(self.temp.name))
        base = self.store.create({"connection": "fixture", "library": "user:0", "collection": "A", "name": "A"})
        self.data = {"kb_ids": [base["id"]], "query": "Does the method improve survival?", "sufficiency_gate": True}

    async def run_qa(self, replies, rounds=None, **data):
        self.service = Service(self.store, rounds or [[hit()]])
        self.model = ScriptedModel(replies)
        return await EvidenceQA(self.service, self.model, configured=lambda: True).ask({**self.data, **data})

    async def test_sufficient_checks_before_generation(self):
        result = await self.run_qa([sufficient, answer, SUPPORT])
        self.assertEqual(result["status"], "answered")
        self.assertEqual(self.model.calls, ["sufficiency", "answer", "review"])
        self.assertNotIn("claims", self.model.payloads[0])
        self.assertNotIn("conversation", self.model.payloads[0])

    async def test_insufficient_twice_never_generates_or_retrieves_thrice(self):
        result = await self.run_qa([insufficient, insufficient])
        self.assertEqual(result["status"], "no_evidence")
        self.assertEqual(self.model.calls, ["sufficiency"] * 2)
        self.assertEqual(len(self.service.calls), 2)
        self.assertEqual(result["trace"]["supplementary_retrievals"], 1)
        self.assertEqual(result["trace"]["correction_rounds"], 0)

    async def test_no_evidence_spends_zero_model_calls(self):
        result = await self.run_qa([], rounds=[[]])
        self.assertEqual(result["status"], "no_evidence")
        self.assertEqual(self.model.calls, [])

    async def test_invalid_or_missing_gate_never_falls_back_to_generation(self):
        for value in ({"sufficient": True, "evidence_ids": ["forged"], "gaps": []},
                      {"sufficient": True, "evidence_ids": [], "gaps": []},
                      {"sufficient": "true", "evidence_ids": [], "gaps": []},
                      RuntimeError("provider error")):
            result = await self.run_qa([value])
            self.assertEqual(result["status"], "unavailable")
            self.assertEqual(self.model.calls, ["sufficiency"])
            self.assertEqual(result["claims"], [])

    async def test_empty_and_contradictory_decisions_rejected(self):
        for decision in ({"sufficient": False, "evidence_ids": [], "gaps": [" "]},
                         {"sufficient": True, "evidence_ids": ["E1"], "gaps": ["missing"]},
                         {"sufficient": True, "evidence_ids": ["E1", "E1"], "gaps": []}):
            with self.assertRaises(ValueError):
                validate_sufficiency(decision, [{"evidence_id": "E1"}])

    async def test_gate_timeout_is_not_no_evidence(self):
        result = await self.run_qa([TimeoutError()])
        self.assertEqual(result["status"], "timeout")
        self.assertEqual(self.model.calls, ["sufficiency"])

    async def test_review_rejection_does_not_stack_another_correction(self):
        result = await self.run_qa([sufficient, answer, REJECT])
        self.assertEqual(result["claims"], [])
        self.assertEqual(self.model.calls, ["sufficiency", "answer", "review"])
        self.assertEqual(result["trace"]["correction_rounds"], 0)

    async def test_fixed_gate_does_not_supplement(self):
        result = await self.run_qa([insufficient], strategy="fixed")
        self.assertEqual(result["status"], "no_evidence")
        self.assertEqual(len(self.service.calls), 1)

    async def test_pool_rotation_without_duplicate_search(self):
        rows = [hit(f"Unique passage number {i} contains distinct evidence about condition {i}.") for i in range(9)]
        def reject_without_query(payload):
            return {"sufficient": False, "evidence_ids": [], "gaps": ["missing condition"]}
        result = await self.run_qa([reject_without_query, sufficient, answer, SUPPORT], rounds=[rows])
        self.assertEqual(result["status"], "answered")
        self.assertEqual(len(self.service.calls), 1)
        self.assertNotEqual(self.model.payloads[0]["evidence"][0]["evidence_id"],
                            self.model.payloads[1]["evidence"][0]["evidence_id"])
        self.assertEqual(result["trace"]["model_calls"], 4)

    async def test_revision_change_stops_before_answer(self):
        def mutate(payload):
            self.store.save_note(self.data["kb_ids"][0], {"title": "Changed", "body": "changed", "confirmed": True})
            return sufficient(payload)
        with self.assertRaises(Conflict):
            await self.run_qa([mutate])
        self.assertEqual(self.model.calls, ["sufficiency"])

    async def test_pixels_gate_answer_review_share_identity_not_telemetry(self):
        token = service_context.set({"config": {"llm": {"model": "fixture-vl", "supports_images": True}}, "connection": "fixture"})
        self.addCleanup(service_context.reset, token)
        service = Service(self.store, [[hit(), visual()]])
        service.visuals = Mock()
        service.visuals.encode.return_value = {**visual()["visual_asset"], "data_url": "data:image/png;base64,cGl4ZWxz"}
        service.visuals.resolve.return_value = visual()["visual_asset"]
        model = ScriptedModel([sufficient, visual_answer, SUPPORT])
        result = await EvidenceQA(service, model, configured=lambda: True).ask({**self.data, "multimodal": True})
        self.assertEqual(result["status"], "answered")
        self.assertEqual(model.payloads[0]["_visual_inputs"], model.payloads[1]["_visual_inputs"])
        self.assertEqual(model.payloads[1]["_visual_inputs"], model.payloads[2]["_visual_inputs"])
        self.assertEqual(result["trace"]["gate_images_sent"], 1)
        self.assertEqual(result["trace"]["visual_images_sent"], 1)
        self.assertEqual([c["images_sent"] for c in result["trace"]["calls"]], [1, 1, 1])
        self.assertNotIn("data:image", json.dumps(result))
        with self.store.transaction() as con:
            metrics = con.execute("SELECT metrics FROM qa_runs").fetchone()[0]
        self.assertNotIn("data:image", metrics)
        self.assertNotIn("missing condition", metrics)

    async def test_gate_image_changed_without_database_revision_stops_before_answer(self):
        token = service_context.set({"config": {"llm": {"model": "fixture", "supports_images": True}}})
        self.addCleanup(service_context.reset, token)
        service = Service(self.store, [[visual()]])
        service.visuals = Mock()
        service.visuals.encode.return_value = {**visual()["visual_asset"], "data_url": "data:image/png;base64,cGl4ZWxz"}
        service.visuals.resolve.return_value = {**visual()["visual_asset"], "image_hash": "changed"}
        model = ScriptedModel([sufficient])
        with self.assertRaises(Conflict):
            await EvidenceQA(service, model, configured=lambda: True).ask({**self.data, "multimodal": True})
        self.assertEqual(model.calls, ["sufficiency"])

    async def test_invalid_usage_remains_unknown(self):
        for usage in ({"input_tokens": True, "output_tokens": 1},
                      {"input_tokens": -1, "output_tokens": 0}):
            async def caller(role, payload, timeout):
                return insufficient(payload), usage
            result = await EvidenceQA(Service(self.store, [[hit()]]), caller,
                configured=lambda: True).ask({**self.data, "strategy": "fixed"})
            self.assertEqual(result["trace"]["unknown_usage_calls"], 1)
            self.assertEqual(result["trace"]["total_tokens"], 0)

    async def test_combined_does_not_gate_memory_lane(self):
        lane = Mock()
        lane.ask = AsyncMock(return_value={"status": "no_evidence", "claims": [], "trace": {}})
        original = Mock()
        original.ask = AsyncMock(return_value={"status": "no_evidence", "claims": [], "trace": {}})
        await CombinedQA(Service(self.store, [[]]), evidence=original, memory=lane).ask(self.data)
        self.assertTrue(original.ask.call_args.args[0]["sufficiency_gate"])
        self.assertNotIn("sufficiency_gate", lane.ask.call_args.args[0])

    async def test_request_validation_and_model_schema(self):
        for flag in ("true", 1, None):
            with self.assertRaises(ValueError):
                KnowledgeAPI._validate_ask_payload({**self.data, "sufficiency_gate": flag})
        with self.assertRaises(ValueError):
            KnowledgeAPI._validate_ask_payload({**self.data, "scope": "notes"})
        model = Mock()
        model.with_structured_output.return_value.ainvoke = AsyncMock(return_value={"parsed": None})
        with patch("research_assistant.agents.research_agent.build_chat_model", return_value=model):
            await ModelCaller()("sufficiency", {"question": "test", "evidence": []}, 1)
        self.assertIs(model.with_structured_output.call_args.args[0], Sufficiency)
