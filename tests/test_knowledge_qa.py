"""Deterministic safety/flow tests. Scripted models do not measure answer quality."""
import asyncio
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch, Mock, AsyncMock

from starlette.applications import Starlette
from starlette.testclient import TestClient

from research_assistant.knowledge.qa import (CombinedQA, EvidenceQA, MemoryQA,
                                              note_candidate_relevant,
                                              select_candidates, select_note_candidates)
from research_assistant.knowledge.store import KnowledgeStore
from research_assistant.knowledge.service import KnowledgeService
from research_assistant.memory.store import Conflict
from research_assistant.web.knowledge_api import KnowledgeAPI


def hit(quote="The method did not improve survival in the small cohort.", **kw):
    return {"kind": "original", "source_id": "paper.pdf", "title": "Synthetic paper", "revision": 1,
            "page": 2, "url": "zotero://open-pdf/library/items/PDF00001?page=2", "quote": quote, **kw}


class Service:
    def __init__(self, store, rounds):
        self.store, self.rounds, self.calls = store, rounds, []

    def search(self, data):
        self.calls.append(data)
        return {"results": self.rounds[min(len(self.calls) - 1, len(self.rounds) - 1)], "warning": None}


class ScriptedModel:
    def __init__(self, answers, callback=None, missing_usage=False):
        self.answers, self.callback, self.missing_usage = iter(answers), callback, missing_usage
        self.calls = []
        self.payloads = []

    async def __call__(self, role, payload, timeout):
        self.calls.append(role)
        self.payloads.append(payload)
        if self.callback:
            self.callback()
        result = next(self.answers)
        if isinstance(result, Exception):
            raise result
        if callable(result):
            result = result(payload)
        return result, None if self.missing_usage else {"input_tokens": 11, "output_tokens": 7}


def answer(payload):
    evidence = payload["evidence"][0]
    return {"claims": [{"text": "该研究未发现小样本队列中的生存获益。", "citations": [
        {"evidence_id": evidence["evidence_id"], "quote": evidence["quote"]}]}]}


SUPPORT = {"question_relevant": True, "verdicts": [{"claim_index": 0, "supported": True, "reason": "原文支持"}]}
REJECT = {"question_relevant": True, "verdicts": [{"claim_index": 0, "supported": False, "reason": "条件不匹配"}], "followup_query": "small cohort survival limitation"}


class EvidenceQATests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = KnowledgeStore(Path(self.temp.name))
        self.base = self.store.create({"connection": "fixture", "library": "user:0", "collection": "A", "name": "A"})
        self.data = {"kb_ids": [self.base["id"]], "query": "这种方法在小样本中有效吗？"}

    def qa(self, answers, rounds=None, **kw):
        self.service = Service(self.store, rounds or [[hit()]])
        self.model = ScriptedModel(answers, **kw)
        return EvidenceQA(self.service, self.model, configured=lambda: True)

    async def test_supported_answer_real_quote_and_usage(self):
        result = await self.qa([answer, SUPPORT]).ask(self.data)
        self.assertEqual(result["status"], "answered")
        self.assertEqual(result["trace"]["model_calls"], 2)
        self.assertEqual(result["trace"]["total_tokens"], 36)
        self.assertEqual([item["role"] for item in result["trace"]["calls"]], ["answer", "review"])
        self.assertTrue(all(item["status"] == "succeeded" for item in result["trace"]["calls"]))
        self.assertTrue(all("elapsed_ms" in item for item in result["trace"]["calls"]))
        self.assertNotIn(self.data["query"], json.dumps(result["trace"]))
        self.assertEqual(result["claims"][0]["citations"][0]["page"], 2)
        self.assertTrue(all(c["scope"] == "original" for c in self.service.calls))
        with self.store.transaction() as con:
            payload = con.execute("SELECT metrics FROM qa_runs").fetchone()[0]
        self.assertNotIn(self.data["query"], payload)
        self.assertNotIn("survival", payload)
        metrics = json.loads(payload)
        self.assertEqual(metrics["qa_observability"]["workflow_version"], "qa-observability-v1")
        self.assertEqual([item["role"] for item in metrics["qa_observability"]["calls"]],
                         ["answer", "review"])
        self.assertTrue(metrics["qa_observability"]["stages"])

    async def test_followup_question_uses_bounded_same_session_context(self):
        service = Service(self.store, [[hit()]])
        model = ScriptedModel([answer, SUPPORT])
        result = await EvidenceQA(service, model, configured=lambda: True).ask({
            **self.data,
            "query": "它在小样本中有效吗？",
            "conversation_id": "session-1",
            "_conversation_history": [{
                "question": "这篇论文的方法是什么？",
                "claims": ["作者提出了一个小样本验证方法。"],
            }],
        })
        self.assertEqual(result["status"], "answered")
        self.assertIn("这篇论文的方法是什么？；它在小样本中有效吗？", service.calls[0]["query"])
        self.assertTrue(result["trace"]["conversation_context_used"])
        self.assertEqual(result["trace"]["conversation_turns"], 1)
        self.assertEqual(model.payloads[0]["conversation"][0]["claims"], ["作者提出了一个小样本验证方法。"])

    async def test_standalone_question_does_not_change_retrieval_query(self):
        service = Service(self.store, [[hit()]])
        model = ScriptedModel([answer, SUPPORT])
        await EvidenceQA(service, model, configured=lambda: True).ask({
            **self.data,
            "query": "小样本中有效吗？",
            "_conversation_history": [{"question": "这篇论文的方法是什么？", "claims": ["历史结论"]}],
        })
        self.assertEqual(service.calls[0]["query"], "小样本中有效吗？")

    async def test_review_rejects_topic_related_evidence_outside_question_scope(self):
        result = await self.qa([answer, {**SUPPORT, "question_relevant": False}]).ask(
            {**self.data, "strategy": "fixed"}
        )
        self.assertEqual(result["status"], "no_evidence")
        self.assertEqual(result["claims"], [])
        self.assertTrue(any("问题范围" in item for item in result["gaps"]))
        self.assertTrue(any("未覆盖问题范围" in item for item in result["warnings"]))
        self.assertFalse(result["trace"]["steps"][-1]["question_relevant"])

    async def test_personal_memory_qa_keeps_note_scope_and_current_revision(self):
        note = {"kind": "user_note", "source_id": "notes/n1-2.md", "entity_id": "n1",
                "revision": 2, "title": "我的理解", "page": None,
                "url": "/knowledge?kb=base&note=n1",
                "quote": "我认为这个方法只适用于小样本验证，仍需长期随访。"}
        service = Service(self.store, [[note]])
        model = ScriptedModel([answer])
        result = await MemoryQA(service, model, configured=lambda: True).ask(self.data)
        self.assertEqual(result["status"], "answered")
        self.assertEqual(result["scope"], "notes")
        self.assertEqual(service.calls[0]["scope"], "notes")
        self.assertEqual(result["claims"][0]["citations"][0]["kind"], "user_note")
        self.assertEqual(result["trace"]["model_calls"], 1)

    async def test_personal_memory_unknown_usage_is_not_reported_as_zero(self):
        note = {"kind": "user_note", "source_id": "notes/n1-1.md", "entity_id": "n1",
                "revision": 1, "title": "我的理解", "page": None,
                "url": "/knowledge?kb=base&note=n1", "quote": "我记录了这种方法在小样本中的限制。"}
        service = Service(self.store, [[note]])
        model = ScriptedModel([answer], missing_usage=True)
        result = await MemoryQA(service, model, configured=lambda: True).ask(self.data)
        self.assertEqual(result["trace"]["unknown_usage_calls"], 1)

    def test_note_candidate_selector_excludes_paper_facts(self):
        self.assertEqual(select_note_candidates([hit()]), [])

    def test_note_candidate_selector_accepts_confirmed_zotero_records(self):
        external_note = hit(kind="external_note", entity_id="fixture-note",
                             source_id="notes/fixture-note-1.md", quote="我记录了方法的适用范围。",
                             url="zotero://select/library/items/NOTE0001")
        external_annotation = hit(kind="external_annotation", entity_id="fixture-annotation",
                                  source_id="notes/fixture-annotation-1.md", quote="图中结果需要谨慎解释。",
                                  url="zotero://open-pdf/library/items/PDF00001?page=2")
        selected = select_note_candidates([external_note, external_annotation])
        self.assertEqual([item["kind"] for item in selected], ["external_note", "external_annotation"])
        self.assertTrue(all(item["evidence_id"].startswith("N") for item in selected))

    async def test_external_zotero_note_enters_memory_qa_and_keeps_source_url(self):
        note = {"kind": "external_note", "source_id": "notes/NOTE0001-1.md", "entity_id": "NOTE0001",
                "revision": 1, "title": "阅读记录", "page": None,
                "url": "zotero://select/library/items/NOTE0001",
                "quote": "我记录了这种方法适合小样本验证。"}
        service = Service(self.store, [[note]])
        model = ScriptedModel([answer])
        result = await MemoryQA(service, model, configured=lambda: True).ask(self.data)
        citation = result["claims"][0]["citations"][0]
        self.assertEqual(result["status"], "answered")
        self.assertEqual(citation["kind"], "external_note")
        self.assertEqual(citation["url"], note["url"])
        self.assertEqual(service.calls[0]["scope"], "notes")

    def test_knowledge_service_returns_external_note_as_zotero_source(self):
        base = self.store.create({"connection": "fixture", "library": "user:0",
                                  "collection": "notes", "name": "Notes"})
        self.store.sync(base["id"], {"name": "Notes", "include_children": True, "complete": True,
                                      "expected_revision": 0,
                                      "entities": [{"key": "NOTE0001", "kind": "note", "title": "阅读记录",
                                                    "body": "我记录了这种方法适合小样本验证。",
                                                    "confirmed": True}]})
        service = KnowledgeService(self.store, backend="tfidf")
        result = service.search({"query": "小样本验证", "kb_ids": [base["id"]], "scope": "notes", "k": 5})
        self.assertTrue(result["results"])
        item = result["results"][0]
        self.assertEqual(item["kind"], "external_note")
        self.assertEqual(item["url"], "zotero://select/library/items/NOTE0001")
        self.assertEqual(item["author_source"], "external_zotero")
        self.assertEqual(item["source_status"], "external_zotero")

    def test_original_selector_still_excludes_external_records(self):
        external = hit(kind="external_note", quote="个人记录，不是论文原文。")
        self.assertEqual(select_candidates([external]), [])

    def test_personal_memory_relevance_gate_rejects_single_shared_word(self):
        note = {"kind": "user_note", "quote": "我修正理解：这个方法适合小样本验证，但长期外推仍需额外证据。", "score": 0.118}
        self.assertTrue(note_candidate_relevant("我之前如何理解这个方法的适用范围？", note))
        self.assertTrue(note_candidate_relevant("小样本验证和长期外推有什么关系？", note))
        self.assertFalse(note_candidate_relevant("作者报告了长期随访吗？", note))
        self.assertFalse(note_candidate_relevant("量子纠缠实验的误差是多少？", note))

    def test_memory_gate_threshold_is_explicit_and_bounded(self):
        from research_assistant.knowledge.qa import memory_gate_coverage
        with patch.dict("os.environ", {"RESEARCH_MEMORY_MIN_QUERY_COVERAGE": "1.7"}):
            self.assertEqual(memory_gate_coverage(), 1.0)
        with patch.dict("os.environ", {"RESEARCH_MEMORY_MIN_QUERY_COVERAGE": "invalid"}):
            self.assertEqual(memory_gate_coverage(), 0.2)

    async def test_personal_memory_bounded_rewrite_recovers_fuzzy_question(self):
        note = {"kind": "user_note", "source_id": "notes/n1-2.md", "entity_id": "n1",
                "revision": 2, "title": "方法外推边界", "page": None,
                "url": "/knowledge?kb=base&note=n1",
                "quote": "我的理解：这个方案在小样本验证中稳定，但长期外推仍需额外证据。"}
        service = Service(self.store, [[note], [note]])
        model = ScriptedModel([
            {"query": "小样本长期外推限制"},
            answer,
        ])
        result = await MemoryQA(service, model, configured=lambda: True).ask(
            {**self.data, "query": "它还能工作多久？"}
        )
        self.assertEqual(result["status"], "answered")
        self.assertEqual(model.calls, ["rewrite", "answer"])
        self.assertEqual(len(service.calls), 2)
        self.assertEqual(service.calls[1]["query"], "小样本长期外推限制")
        self.assertEqual(result["trace"]["model_calls"], 2)
        self.assertEqual(result["trace"]["retrieval_calls"], 2)
        self.assertEqual(result["trace"]["rewrite_rounds"], 1)
        self.assertTrue(any("受限问题改写" in warning for warning in result["warnings"]))

    async def test_combined_qa_keeps_original_and_personal_record_sections_separate(self):
        class Stub:
            def __init__(self, result):
                self.result = result
                self.lock = asyncio.Lock()

            async def ask(self, data):
                return self.result

        original = {"status": "answered", "question": "问题", "claims": [{"text": "论文事实", "citations": []}],
                    "gaps": [], "evidence": [{"kind": "original", "quote": "paper"}], "warnings": [],
                    "trace": {"model_calls": 2, "retrieval_calls": 1, "input_tokens": 10, "output_tokens": 5,
                              "unknown_usage_calls": 0, "elapsed_ms": 20, "steps": [{"stage": "retrieve"}],
                              "model": "model", "review_model": "review"}, "revisions": {"base": 1}}
        notes = {"status": "answered", "question": "问题", "claims": [{"text": "我的理解", "citations": []}],
                 "gaps": [], "evidence": [{"kind": "user_note", "quote": "note"}], "warnings": [],
                 "trace": {"model_calls": 1, "retrieval_calls": 1, "input_tokens": 7, "output_tokens": 3,
                           "unknown_usage_calls": 0, "elapsed_ms": 30, "steps": []}, "revisions": {"base": 1}}
        result = await CombinedQA(Service(self.store, [[hit()]]), Stub(original), Stub(notes)).ask(self.data)
        self.assertEqual(result["scope"], "both")
        self.assertEqual([s["scope"] for s in result["sections"]], ["original", "notes"])
        self.assertEqual(result["sections"][0]["result"]["evidence"][0]["kind"], "original")
        self.assertEqual(result["sections"][1]["result"]["evidence"][0]["kind"], "user_note")
        self.assertEqual([claim["scope"] for claim in result["claims"]], ["original", "notes"])
        self.assertEqual(result["trace"]["model_calls"], 3)
        self.assertEqual(result["trace"]["total_tokens"], 25)
        self.assertEqual(result["trace"]["rewrite_rounds"], 0)
        self.assertEqual(result["trace"]["correction_rounds"], 0)

    async def test_combined_qa_keeps_other_lane_when_one_lane_fails(self):
        class Stub:
            def __init__(self, result):
                self.result, self.lock = result, asyncio.Lock()

            async def ask(self, data):
                if isinstance(self.result, BaseException):
                    raise self.result
                return self.result

        notes = {"status": "answered", "question": "问题", "claims": [], "gaps": [], "evidence": [],
                 "warnings": [], "trace": {"model_calls": 1, "retrieval_calls": 1, "input_tokens": 1,
                           "output_tokens": 1, "unknown_usage_calls": 0, "steps": [], "elapsed_ms": 1}}
        result = await CombinedQA(Service(self.store, [[hit()]]), Stub(RuntimeError("temporary")), Stub(notes)).ask(self.data)
        self.assertEqual(result["status"], "partial")
        self.assertEqual(result["sections"][0]["result"]["status"], "unavailable")
        self.assertEqual(result["sections"][1]["result"]["status"], "answered")

    async def test_combined_qa_rejects_revision_change_at_merge_barrier(self):
        service = Service(self.store, [[hit()]])

        class Stub:
            def __init__(self, callback=None):
                self.lock = asyncio.Lock()
                self.callback = callback

            async def ask(self, data):
                if self.callback:
                    self.callback()
                return {"status": "answered", "question": "问题", "claims": [],
                        "gaps": [], "evidence": [], "warnings": [],
                        "trace": {"model_calls": 0, "retrieval_calls": 1,
                                  "input_tokens": 0, "output_tokens": 0,
                                  "unknown_usage_calls": 0, "elapsed_ms": 0, "steps": []},
                        "revisions": {self.base_id: 1}}

        def change_revision():
            base = self.store.base(self.base["id"])
            self.store.set_archive(base["id"], {"archived": True, "expected_revision": base["revision"]})

        original = Stub(change_revision)
        original.base_id = self.base["id"]
        notes = Stub()
        notes.base_id = self.base["id"]
        with self.assertRaises(Conflict):
            await CombinedQA(service, original, notes).ask(self.data)

    async def test_failed_review_rewrites_once_and_rechecks(self):
        result = await self.qa([answer, REJECT, answer, SUPPORT]).ask(self.data)
        self.assertEqual(result["status"], "answered")
        self.assertEqual(result["trace"]["model_calls"], 4)
        self.assertEqual(result["trace"]["rewrite_rounds"], 1)
        self.assertEqual(len(self.service.calls), 2)
        self.assertEqual(self.service.calls[1]["query"], REJECT["followup_query"])

    async def test_no_infinite_retry_after_two_rejections(self):
        result = await self.qa([answer, REJECT, answer, REJECT]).ask(self.data)
        self.assertEqual(result["claims"], [])
        self.assertEqual(result["status"], "no_evidence")
        self.assertEqual(result["trace"]["model_calls"], 4)
        self.assertEqual(len(self.service.calls), 2)

    async def test_no_hits_can_rewrite_without_claiming_an_answer(self):
        result = await self.qa([{"claims": [], "gaps": ["需要英文术语"], "followup_query": "small cohort"}, answer, SUPPORT],
                               rounds=[[], [hit()]]).ask(self.data)
        self.assertEqual(result["trace"]["model_calls"], 3)
        self.assertTrue(result["claims"])

    async def test_fabricated_quote_removed_before_review(self):
        def wrong(payload):
            value = answer(payload)
            value["claims"][0]["citations"][0]["quote"] = "invented source quotation"
            return value
        result = await self.qa([wrong, {"claims": [], "gaps": ["无法引用"]}]).ask(self.data)
        self.assertFalse(result["claims"])
        self.assertEqual(self.model.calls, ["answer", "answer"])
        self.assertEqual(len(self.service.calls), 1)

    async def test_unknown_citation_cannot_be_returned(self):
        value = {"claims": [{"text": "猜测", "citations": [{"evidence_id": "foreign", "quote": hit()["quote"]}]}]}
        self.assertFalse((await self.qa([value, {"claims": []}]).ask(self.data))["claims"])

    async def test_duplicate_review_verdict_is_not_support(self):
        result = await self.qa([answer, {"question_relevant": True, "verdicts": SUPPORT["verdicts"] * 2}, {"claims": []}]).ask(self.data)
        self.assertFalse(result["claims"])

    async def test_review_only_sees_cited_excerpt_and_feedback_reaches_correction(self):
        reject = {"question_relevant": True, "verdicts": [{"claim_index": 0, "supported": False, "reason": "引文缺少条件"}]}
        result = await self.qa([answer, reject, answer, SUPPORT]).ask(self.data)
        self.assertEqual(result["status"], "answered")
        self.assertNotIn("evidence", self.model.payloads[1])
        self.assertTrue(self.model.payloads[2]["previous_feedback"])
        self.assertEqual(result["trace"]["correction_rounds"], 1)
        self.assertEqual(result["trace"]["retrieval_calls"], 1)

    async def test_unknown_usage_not_fabricated_as_zero(self):
        result = await self.qa([answer, SUPPORT], missing_usage=True).ask(self.data)
        self.assertEqual(result["trace"]["unknown_usage_calls"], 2)

    async def test_outage_preserves_candidates_and_never_retries(self):
        result = await self.qa([ConnectionError("secret must not leak")]).ask(self.data)
        self.assertEqual(result["status"], "unavailable")
        self.assertTrue(result["evidence"])
        self.assertEqual(result["trace"]["model_calls"], 1)
        self.assertEqual(result["trace"]["calls"][0]["status"], "error")
        self.assertEqual(result["trace"]["calls"][0]["error_type"], "ConnectionError")
        self.assertNotIn("secret", json.dumps(result))

    async def test_timeout_preserves_candidates(self):
        result = await self.qa([TimeoutError()]).ask(self.data)
        self.assertEqual(result["status"], "timeout")
        self.assertFalse(result["claims"])
        self.assertEqual(result["trace"]["calls"][0]["status"], "timeout")

    async def test_missing_configuration_does_not_call_model(self):
        qa = self.qa([])
        qa.configured = lambda: False
        result = await qa.ask(self.data)
        self.assertEqual(result["status"], "model_unavailable")
        self.assertEqual(result["trace"]["model_calls"], 0)

    async def test_changed_revision_discards_answer(self):
        def change():
            base = self.store.base(self.base["id"])
            self.store.set_archive(base["id"], {"archived": True, "expected_revision": base["revision"]})
        qa = self.qa([answer, SUPPORT], callback=change)
        with self.assertRaises(Conflict):
            await qa.ask(self.data)

    async def test_fixed_baseline_never_rewrites(self):
        result = await self.qa([answer, REJECT]).ask({**self.data, "strategy": "fixed"})
        self.assertEqual(len(self.service.calls), 1)
        self.assertEqual(result["trace"]["rewrite_rounds"], 0)

    async def test_archived_scope_rejected_before_model(self):
        self.store.set_archive(self.base["id"], {"archived": True, "expected_revision": self.base["revision"]})
        qa = self.qa([])
        with self.assertRaises(Conflict):
            await qa.ask(self.data)
        self.assertEqual(self.model.calls, [])

    async def test_audit_failure_does_not_lose_result(self):
        self.store.record_qa_run = lambda _: (_ for _ in ()).throw(OSError())
        result = await self.qa([answer, SUPPORT]).ask(self.data)
        self.assertTrue(result["claims"])
        self.assertTrue(result["warnings"])

    async def test_concurrent_request_does_not_start_more_calls(self):
        qa = self.qa([])
        async with qa.lock:
            with self.assertRaisesRegex(RuntimeError, "qa_busy"):
                await qa.ask(self.data)
        self.assertEqual(self.model.calls, [])

    async def test_glm53_uses_supported_low_effort_instead_of_disabling_thinking(self):
        from research_assistant.knowledge.qa import ModelCaller
        model = Mock()
        model.with_structured_output.return_value.ainvoke = AsyncMock(return_value={"parsed": {"claims": []}})
        with patch.dict('os.environ', {'OPENAI_MODEL': 'GLM-5.3-FlashX', 'RESEARCH_QA_REVIEW_MODEL': 'GLM-5.3-FlashX'}):
            with patch('research_assistant.agents.research_agent.build_chat_model', return_value=model) as build:
                await ModelCaller()('review', {'claims': []}, 45)
        self.assertFalse(build.call_args.kwargs['disable_thinking'])
        self.assertEqual(build.call_args.kwargs['reasoning_effort'], 'low')

    async def test_review_uses_configured_role_and_recovers_length_usage(self):
        from research_assistant.knowledge.qa import ModelCaller
        class LengthFinishReasonError(Exception):
            completion = Mock(usage=Mock(prompt_tokens=12, completion_tokens=1536))
        model = Mock()
        model.with_structured_output.return_value.ainvoke = AsyncMock(side_effect=LengthFinishReasonError())
        with patch.dict('os.environ', {'OPENAI_MODEL': 'GLM-5.3', 'RESEARCH_QA_REVIEW_MODEL': 'GLM-4.7'}):
            with patch('research_assistant.agents.research_agent.build_chat_model', return_value=model) as build:
                value, usage = await ModelCaller()('review', {'claims': []}, 45)
        self.assertIsNone(value)
        self.assertEqual(usage['output_tokens'], 1536)
        self.assertEqual(usage['finish_reason'], 'length')
        self.assertEqual(build.call_args.kwargs['model_name'], 'GLM-4.7')
        self.assertTrue(build.call_args.kwargs['disable_thinking'])


class SelectionTests(unittest.TestCase):
    def test_scientific_symbol_case_is_not_silently_normalized(self):
        from research_assistant.knowledge.qa import normalized
        self.assertNotEqual(normalized('CO concentration'), normalized('Co concentration'))

    def test_timeout_is_set_before_sdk_client_construction(self):
        from research_assistant.agents.research_agent import build_chat_model
        with patch.dict('os.environ', {'OPENAI_API_KEY': 'fixture', 'OPENAI_BASE_URL': 'https://example.invalid/v1', 'OPENAI_MODEL': 'fixture', 'RESEARCH_ASSISTANT_MODEL_TIMEOUT': '20'}):
            with patch('research_assistant.agents.research_agent.ChatOpenAI') as model:
                build_chat_model(timeout=45, max_tokens=3072)
                self.assertEqual(model.call_args.kwargs['timeout'], 45)
                self.assertEqual(model.call_args.kwargs['max_tokens'], 3072)
                self.assertEqual(model.call_args.kwargs['max_retries'], 0)

    def test_duplicates_do_not_exclude_opposite_finding(self):
        positive = hit("The method improved survival in the small cohort.")
        negative = hit("The method did not improve survival in the small cohort.")
        result = select_candidates([positive] * 6 + [negative])
        self.assertEqual(len(result), 2)
        self.assertEqual(len(select_candidates([positive] * 6 + [negative], "fixed")), 6)

    def test_notes_are_not_original_facts(self):
        self.assertEqual(select_candidates([hit(kind="user_note"), hit(kind="external_annotation")]), [])

    def test_budget_never_truncates_negation(self):
        result = select_candidates([hit("x" * 13000), hit()])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["quote"], hit()["quote"])

    def test_benchmark_checks_evidence_not_just_page(self):
        from benchmarks.evaluate_knowledge_qa import evidence_metrics
        gold = [{"source_id": "paper.pdf", "page": 2, "quote": "did not improve survival"}]
        self.assertEqual(evidence_metrics([hit("irrelevant heading on same page")], gold)["recall_at_6"], 0)
        self.assertEqual(evidence_metrics([hit()], gold)["recall_at_6"], 1)

    def test_qa_benchmark_refuses_wrong_split(self):
        from benchmarks.evaluate_knowledge_qa import check_split
        self.assertEqual(check_split({"split": "test"}, "test"), "test")
        with self.assertRaises(ValueError):
            check_split({"split": "development"}, "test")

    def test_qa_dataset_validator_checks_page_quote_and_split(self):
        from benchmarks.validate_knowledge_qa_dataset import validate_dataset
        from tests.test_reading_memory import pdf
        with tempfile.TemporaryDirectory() as root:
            store = KnowledgeStore(Path(root))
            base = store.create({"connection": "fixture", "library": "user:0", "collection": "A", "name": "A"})
            paper = Path(root) / "paper.pdf"
            pdf(paper)
            content = paper.read_bytes()
            from research_assistant.knowledge.store import digest
            sha = digest(content)
            store.put_object(sha, content)
            store.sync(base["id"], {"name": base["name"], "include_children": False, "complete": True,
                                     "expected_revision": 0, "entities": [{"key": "PDF0001", "kind": "document",
                                     "title": "论文", "hash": sha}]})
            dataset_path = Path(root) / "qa.json"
            dataset_path.write_text(json.dumps({"id": "fixture-qa", "split": "test", "cases": [{
                "id": "Q1", "query": "方法", "kb_ids": [base["id"]], "no_answer": False,
                "expected": [{"source_id": sha + ".pdf", "page": 1, "quote": "Scientific evidence"}],
            }]}), encoding="utf-8")
            report = validate_dataset(Path(root), dataset_path, expected_split="test")
            self.assertTrue(report["valid"], report["errors"])

            bad = json.loads(dataset_path.read_text(encoding="utf-8"))
            bad["cases"][0]["expected"][0]["quote"] = "不存在的原文"
            dataset_path.write_text(json.dumps(bad, ensure_ascii=False), encoding="utf-8")
            report = validate_dataset(Path(root), dataset_path, expected_split="test")
            self.assertFalse(report["valid"])
            self.assertTrue(any("quote is not present" in error for error in report["errors"]))

    def test_memory_benchmark_checks_revision_and_source_link(self):
        from benchmarks.evaluate_personal_memory import memory_metrics
        result = memory_metrics([{"kind": "user_note", "entity_id": "n1", "revision": 2,
                                  "quote": "我记录的方法限制", "url": "/knowledge?kb=b&note=n1"}],
                                [{"entity_id": "n1", "revision": 2, "quote": "方法限制"}])
        self.assertEqual(result["recall_at_6"], 1)
        self.assertEqual(result["revision_accuracy"], 1)
        self.assertEqual(result["source_link_rate"], 1)

    def test_memory_benchmark_counts_external_zotero_records(self):
        from benchmarks.evaluate_personal_memory import memory_metrics
        result = memory_metrics(
            [{"kind": "external_note", "entity_id": "z1", "revision": 1,
              "quote": "Zotero 原生阅读记录", "url": "zotero://select/library/items/NOTE0001"}],
            [{"kind": "external_note", "entity_id": "z1", "revision": 1,
              "quote": "原生阅读记录"}],
        )
        self.assertEqual(result["recall_at_6"], 1)
        self.assertEqual(result["revision_accuracy"], 1)
        self.assertEqual(result["source_link_rate"], 1)

    def test_memory_evaluation_refuses_wrong_declared_split(self):
        from benchmarks.evaluate_personal_memory import check_split
        self.assertEqual(check_split({"split": "test"}, "test"), "test")
        with self.assertRaises(ValueError):
            check_split({"split": "development"}, "test")

    def test_memory_dataset_validator_checks_current_revision_and_quote(self):
        from benchmarks.validate_personal_memory_dataset import validate_dataset
        with tempfile.TemporaryDirectory() as root:
            store = KnowledgeStore(Path(root))
            base = store.create({"connection": "fixture", "library": "user:0", "collection": "A", "name": "A"})
            note = store.save_note(base["id"], {
                "title": "理解", "type": "understanding", "body": "我记录了方法的边界。", "confirmed": True,
            })
            dataset_path = Path(root) / "memory.json"
            dataset_path.write_text(json.dumps({"id": "fixture-memory", "split": "test", "cases": [{
                "id": "M1", "query": "方法边界", "kb_ids": [base["id"]], "no_answer": False,
                "expected": [{"entity_id": note["id"], "revision": note["revision"], "kind": "user_note", "quote": "方法的边界"}],
            }]}), encoding="utf-8")
            report = validate_dataset(store.root, dataset_path)
            self.assertTrue(report["valid"], report["errors"])

            stale = json.loads(dataset_path.read_text(encoding="utf-8"))
            stale["cases"][0]["expected"][0]["quote"] = "不存在的摘录"
            dataset_path.write_text(json.dumps(stale), encoding="utf-8")
            report = validate_dataset(store.root, dataset_path)
            self.assertFalse(report["valid"])
            self.assertTrue(any("quote is not present" in error for error in report["errors"]))

    def test_memory_dataset_validator_accepts_external_zotero_note(self):
        from benchmarks.validate_personal_memory_dataset import validate_dataset
        with tempfile.TemporaryDirectory() as root:
            store = KnowledgeStore(Path(root))
            base = store.create({"connection": "fixture", "library": "user:0", "collection": "A", "name": "A"})
            store.sync(base["id"], {"name": "A", "include_children": True, "complete": True,
                                     "expected_revision": 0, "entities": [{
                                         "key": "NOTE0001", "kind": "note", "title": "Zotero 阅读记录",
                                         "body": "我记录了原生笔记中的适用条件。", "confirmed": True,
                                     }]})
            entity = store.snapshot([base["id"]])["entities"][0]
            dataset_path = Path(root) / "external-memory.json"
            dataset_path.write_text(json.dumps({"id": "external-memory", "split": "test", "cases": [{
                "id": "M1", "query": "适用条件", "kb_ids": [base["id"]], "no_answer": False,
                "expected": [{"entity_id": entity["id"], "revision": entity["revision"],
                              "kind": "external_note", "quote": "原生笔记中的适用条件"}],
            }]}), encoding="utf-8")
            report = validate_dataset(store.root, dataset_path)
            self.assertTrue(report["valid"], report["errors"])

    def test_http_ask_is_authenticated_and_current_snapshot_checked(self):
        with tempfile.TemporaryDirectory() as root:
            api = KnowledgeAPI(root, token="fixture", backend="tfidf")
            base = api.store.create({"connection": "fixture", "library": "user:0", "collection": "A", "name": "A"})
            service = Service(api.store, [[hit()]])
            api._qa = EvidenceQA(service, ScriptedModel([answer, SUPPORT]), configured=lambda: True)
            with TestClient(Starlette(routes=api.routes())) as client:
                payload = {"query": "小样本有效吗", "kb_ids": [base["id"]]}
                self.assertEqual(client.post("/api/knowledge/ask", json=payload).status_code, 401)
                response = client.post("/api/knowledge/ask", json=payload, headers={"Authorization": "Bearer fixture"})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["status"], "answered")

    def test_http_both_returns_separate_source_sections(self):
        class CombinedStub:
            def __init__(self):
                self.lock = asyncio.Lock()

            async def ask(self, data):
                return {"status": "partial", "scope": "both", "sections": [
                    {"scope": "original", "label": "论文原文依据", "result": {"claims": [], "evidence": [], "gaps": []}},
                    {"scope": "notes", "label": "你的个人记录", "result": {"claims": [], "evidence": [], "gaps": []}},
                ], "claims": [], "evidence": [], "gaps": [], "warnings": [],
                        "trace": {"model_calls": 0, "retrieval_calls": 0, "input_tokens": 0,
                                  "output_tokens": 0, "unknown_usage_calls": 0, "steps": [], "elapsed_ms": 0}}

        with tempfile.TemporaryDirectory() as root:
            api = KnowledgeAPI(root, token="fixture", backend="tfidf")
            base = api.store.create({"connection": "fixture", "library": "user:0", "collection": "A", "name": "A"})
            api._combined_qa = CombinedStub()
            with TestClient(Starlette(routes=api.routes())) as client:
                response = client.post("/api/knowledge/ask", json={"query": "问题", "scope": "both", "kb_ids": [base["id"]]},
                                       headers={"Authorization": "Bearer fixture"})
            self.assertEqual(response.status_code, 200)
            self.assertEqual([s["scope"] for s in response.json()["sections"]], ["original", "notes"])

    def test_http_async_ask_persists_and_polls_result(self):
        class AsyncStub:
            def __init__(self):
                self.lock = asyncio.Lock()

            async def ask(self, data):
                await asyncio.sleep(0)
                return {"status": "answered", "claims": [], "evidence": [], "gaps": [],
                        "warnings": [], "trace": {"model_calls": 1}}

        with tempfile.TemporaryDirectory() as root:
            api = KnowledgeAPI(root, token="fixture", backend="tfidf")
            base = api.store.create({"connection": "fixture", "library": "user:0", "collection": "A", "name": "A"})
            api._qa = AsyncStub()
            headers = {"Authorization": "Bearer fixture"}
            with TestClient(Starlette(routes=api.routes())) as client:
                response = client.post("/api/knowledge/ask-async", headers=headers,
                                       json={"query": "问题", "kb_ids": [base["id"]]})
                self.assertEqual(response.status_code, 202)
                job_id = response.json()["job_id"]
                observed = None
                for _ in range(20):
                    observed = client.get("/api/knowledge/jobs/" + job_id, headers=headers).json()
                    if observed["state"] == "succeeded":
                        break
                    time.sleep(0.02)
                self.assertEqual(observed["state"], "succeeded")
                self.assertEqual(observed["result"]["status"], "answered")

    def test_http_async_failed_job_can_be_manually_retried_once(self):
        class AsyncStub:
            def __init__(self):
                self.lock = asyncio.Lock()

            async def ask(self, data):
                return {"status": "answered", "claims": [], "evidence": [], "gaps": [],
                        "warnings": [], "trace": {"model_calls": 1}}

        with tempfile.TemporaryDirectory() as root:
            api = KnowledgeAPI(root, token="fixture", backend="tfidf")
            base = api.store.create({"connection": "fixture", "library": "user:0", "collection": "A", "name": "A"})
            api._qa = AsyncStub()
            failed = api._qa_jobs.create("ask", {"query": "可恢复问题", "kb_ids": [base["id"]]})
            api._qa_jobs.finish(failed["job_id"], "failed", error="模型暂不可用")
            headers = {"Authorization": "Bearer fixture"}
            with TestClient(Starlette(routes=api.routes())) as client:
                response = client.post("/api/knowledge/jobs/" + failed["job_id"], headers=headers,
                                       json={"action": "retry"})
                self.assertEqual(response.status_code, 202)
                retry_id = response.json()["job_id"]
                self.assertEqual(response.json()["retry_count"], 1)
                observed = None
                for _ in range(20):
                    observed = client.get("/api/knowledge/jobs/" + retry_id, headers=headers).json()
                    if observed["state"] == "succeeded":
                        break
                    time.sleep(0.02)
                self.assertEqual(observed["state"], "succeeded")
                rejected = client.post("/api/knowledge/jobs/" + retry_id, headers=headers,
                                       json={"action": "retry"})
                self.assertEqual(rejected.status_code, 400)

    def test_http_queued_job_can_be_cancelled_without_starting_model(self):
        class AsyncStub:
            def __init__(self):
                self.lock = asyncio.Lock()
                self.calls = 0

            async def ask(self, data):
                self.calls += 1
                return {"status": "answered", "claims": [], "evidence": [], "gaps": [],
                        "warnings": [], "trace": {"model_calls": 1}}

        with tempfile.TemporaryDirectory() as root:
            api = KnowledgeAPI(root, token="fixture", backend="tfidf")
            base = api.store.create({"connection": "fixture", "library": "user:0", "collection": "A", "name": "A"})
            stub = AsyncStub()
            api._qa = stub
            job = api._qa_jobs.create("ask", {"query": "排队问题", "kb_ids": [base["id"]]})
            headers = {"Authorization": "Bearer fixture"}
            with TestClient(Starlette(routes=api.routes())) as client:
                response = client.post("/api/knowledge/jobs/" + job["job_id"], headers=headers,
                                       json={"action": "cancel"})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["state"], "cancelled")
                detail = client.get("/api/knowledge/jobs/" + job["job_id"], headers=headers).json()
                self.assertEqual(detail["state"], "cancelled")
            self.assertEqual(stub.calls, 0)

    def test_http_running_job_cancel_stays_cancelled_and_does_not_become_interrupted(self):
        class AsyncStub:
            def __init__(self):
                self.lock = asyncio.Lock()
                self.calls = 0

            async def ask(self, data):
                self.calls += 1
                await asyncio.sleep(60)

        with tempfile.TemporaryDirectory() as root:
            api = KnowledgeAPI(root, token="fixture", backend="tfidf")
            base = api.store.create({"connection": "fixture", "library": "user:0", "collection": "A", "name": "A"})
            stub = AsyncStub()
            api._qa = stub
            headers = {"Authorization": "Bearer fixture"}
            with TestClient(Starlette(routes=api.routes())) as client:
                response = client.post("/api/knowledge/ask-async", headers=headers,
                                       json={"query": "可停止问题", "kb_ids": [base["id"]]})
                self.assertEqual(response.status_code, 202)
                job_id = response.json()["job_id"]
                state = None
                for _ in range(40):
                    state = client.get("/api/knowledge/jobs/" + job_id, headers=headers).json()
                    if state["state"] == "running":
                        break
                    time.sleep(0.02)
                self.assertEqual(state["state"], "running")
                response = client.post("/api/knowledge/jobs/" + job_id, headers=headers,
                                       json={"action": "cancel"})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["state"], "cancelled")
                time.sleep(0.05)
                detail = client.get("/api/knowledge/jobs/" + job_id, headers=headers).json()
                self.assertEqual(detail["state"], "cancelled")
                self.assertIn("主动停止", detail["error"])
                rejected = client.post("/api/knowledge/jobs/" + job_id, headers=headers,
                                       json={"action": "cancel"})
                self.assertEqual(rejected.status_code, 200)
            self.assertEqual(stub.calls, 1)

    def test_http_completed_job_rejects_cancel(self):
        with tempfile.TemporaryDirectory() as root:
            api = KnowledgeAPI(root, token="fixture", backend="tfidf")
            job = api._qa_jobs.create("ask", {"query": "已完成问题", "kb_ids": ["kb"]})
            api._qa_jobs.finish(job["job_id"], "succeeded", result={"status": "answered"})
            headers = {"Authorization": "Bearer fixture"}
            with TestClient(Starlette(routes=api.routes())) as client:
                response = client.post("/api/knowledge/jobs/" + job["job_id"], headers=headers,
                                       json={"action": "cancel"})
                self.assertEqual(response.status_code, 400)
                self.assertEqual(client.get("/api/knowledge/jobs/" + job["job_id"], headers=headers).json()["state"],
                                 "succeeded")

    def test_http_completed_job_feedback_is_persisted_without_promoting_gold(self):
        with tempfile.TemporaryDirectory() as root:
            api = KnowledgeAPI(root, token="fixture", backend="tfidf")
            base = api.store.create({"connection": "fixture", "library": "user:0", "collection": "A", "name": "A"})
            job = api._qa_jobs.create("ask", {"query": "我之前怎么理解？", "kb_ids": [base["id"]], "scope": "notes"})
            api._qa_jobs.finish(job["job_id"], "succeeded", result={
                "status": "answered", "scope": "notes", "revisions": {base["id"]: 1},
                "evidence": [{"evidence_id": "N1", "kind": "user_note", "revision": 1,
                              "quote": "我的阅读记录。"}], "claims": [], "gaps": []})
            headers = {"Authorization": "Bearer fixture"}
            with TestClient(Starlette(routes=api.routes())) as client:
                response = client.post("/api/knowledge/jobs/" + job["job_id"], headers=headers,
                                       json={"action": "feedback", "rating": "partial"})
                self.assertEqual(response.status_code, 201)
                self.assertEqual(response.json()["rating"], "partial")
                detail = client.get("/api/knowledge/jobs/" + job["job_id"], headers=headers).json()
                self.assertEqual(detail["feedback"]["rating"], "partial")
                duplicate = client.post("/api/knowledge/jobs/" + job["job_id"], headers=headers,
                                        json={"action": "feedback", "rating": "helpful"})
                self.assertEqual(duplicate.status_code, 400)


if __name__ == "__main__":
    unittest.main()
