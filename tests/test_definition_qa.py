"""Synthetic regressions for definition recall; no medical-quality claims or APIs."""
import json
from pathlib import Path
import tempfile
import unittest

from research_assistant.core.agent_telemetry import summarize_qa_trace
from research_assistant.knowledge.qa import CombinedQA, EvidenceQA, MemoryQA, select_candidates
from research_assistant.knowledge.store import KnowledgeStore
from research_assistant.retrieval.query_intent import definition_query, definition_rank, definition_subject
from tests.test_knowledge_qa import Service, ScriptedModel, SUPPORT, answer, hit


def distractors(count=20):
    return [hit(f"Trial {i} reports treatment outcomes in a heart failure cohort.",
                source_id=f"trial-{i}.pdf") for i in range(count)]


class DefinitionIntentTests(unittest.TestCase):
    def test_bilingual_subject_preserves_conditions(self):
        for query in ("什么是心衰？", "心衰是什么", "请问心衰的定义是什么？", "What is heart failure?"):
            self.assertIn("heart failure", definition_query(query))
        self.assertIn("心衰的治疗原则", definition_query("什么是心衰的治疗原则"))
        self.assertNotIn("heart failure", definition_query("什么是心衰的治疗原则"))
        self.assertIsNone(definition_subject("什么是心衰，以及如何治疗？"))
        self.assertEqual(definition_query("这种方法在小样本中有效吗？"), "")

    def test_unrelated_definition_does_not_promote_topic_only_passage(self):
        quote = "Heart failure patients were included. Response is defined as a change in score."
        self.assertEqual(definition_rank("什么是心衰", quote), 0)
        self.assertEqual(definition_rank("什么是心衰", "Heart failure (HF) is a synthetic test syndrome."), 1)
        self.assertEqual(definition_rank("什么是心衰", "心力衰竭是一个测试定义，须核对原文。"), 1)

    def test_definition_below_first_six_is_used_without_changing_baseline(self):
        definition = hit("Heart failure is a synthetic test syndrome, not a treatment endpoint.", source_id="definition.pdf")
        pool = distractors() + [definition]
        selected = select_candidates(pool, "adaptive", question="什么是心衰")
        self.assertEqual(selected[0]["source_id"], "definition.pdf")
        self.assertIn("not a treatment endpoint", selected[0]["quote"])
        self.assertNotIn("definition.pdf", [e["source_id"] for e in select_candidates(pool, "fixed")])

    def test_supplementary_queries_are_removed_from_durable_telemetry(self):
        trace = summarize_qa_trace({"steps": [{"stage": "retrieve", "query": "PRIVATE",
            "searches": [{"query": "PRIVATE EXPANSION", "candidates": 20, "elapsed_ms": 3}],
            "new_selected": 6, "rotated": True}]})
        self.assertNotIn("PRIVATE", json.dumps(trace))
        self.assertEqual(trace["stages"][0]["searches"], [{"candidates": 20, "elapsed_ms": 3}])


class DefinitionFlowTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.store = KnowledgeStore(Path(temp.name))
        base = self.store.create({"connection": "fixture", "library": "user:0", "collection": "A", "name": "A"})
        self.data = {"kb_ids": [base["id"]], "query": "什么是心衰"}

    async def run_qa(self, rounds, replies, **data):
        self.service = Service(self.store, rounds)
        self.model = ScriptedModel(replies)
        return await EvidenceQA(self.service, self.model, configured=lambda: True).ask({**self.data, **data})

    async def test_definition_search_recovers_evidence_outside_original_top_twenty(self):
        definition = hit("Heart failure is a synthetic test syndrome.", source_id="definition.pdf", page=4)
        result = await self.run_qa([distractors(), [definition]], [answer, SUPPORT])
        self.assertEqual(result["status"], "answered")
        self.assertEqual(result["claims"][0]["citations"][0]["source_id"], "definition.pdf")
        self.assertEqual(result["trace"]["retrieval_calls"], 2)
        self.assertEqual(result["trace"]["model_calls"], 2)
        self.assertEqual(result["trace"]["steps"][0]["candidates"], 21)
        self.assertEqual(self.service.calls[0]["query"], "什么是心衰")
        self.assertTrue(all(c["text_only"] and c["kb_ids"] == self.data["kb_ids"]
                            and c["scope"] == "original" for c in self.service.calls))

    async def test_empty_draft_without_rewrite_tries_unseen_tail(self):
        pool = distractors(8)
        result = await self.run_qa([pool], [{"claims": [], "gaps": ["首批不足"]}, answer, SUPPORT])
        self.assertEqual(result["status"], "answered")
        first = {e["evidence_id"] for e in self.model.payloads[0]["evidence"]}
        self.assertNotIn(self.model.payloads[1]["evidence"][0]["evidence_id"], first)
        self.assertEqual(result["trace"]["steps"][2]["new_selected"], 2)
        self.assertEqual(result["trace"]["steps"][2]["candidates"], 8)
        self.assertEqual(result["trace"]["retrieval_calls"], 2)  # no duplicate search on correction
        self.assertEqual(result["trace"]["correction_rounds"], 1)

    async def test_scope_rejection_without_rewrite_rotates_candidates(self):
        rejected = {**SUPPORT, "question_relevant": False}
        result = await self.run_qa([distractors(9)], [answer, rejected, answer, SUPPORT])
        self.assertEqual(result["status"], "answered")
        self.assertTrue(result["trace"]["steps"][2]["rotated"])
        self.assertEqual(result["trace"]["steps"][2]["new_selected"], 3)
        self.assertTrue(self.model.payloads[2]["previous_feedback"])

    async def test_unsupported_claims_also_try_new_evidence_but_keep_four_call_cap(self):
        rejected = {"question_relevant": True, "verdicts": [{"claim_index": 0, "supported": False, "reason": "证据不足"}]}
        result = await self.run_qa([distractors()], [answer, rejected, answer, rejected])
        self.assertEqual(result["status"], "no_evidence")
        self.assertEqual(result["claims"], [])
        self.assertEqual(result["trace"]["model_calls"], 4)
        self.assertEqual(result["trace"]["steps"][2]["new_selected"], 6)
        self.assertEqual(result["trace"]["retrieval_calls"], 2)

    async def test_rewritten_query_is_bounded_to_one_extra_search(self):
        rejected = {**SUPPORT, "question_relevant": False, "followup_query": "heart failure definition consensus"}
        result = await self.run_qa([distractors()], [answer, rejected, answer, rejected])
        self.assertEqual(result["trace"]["retrieval_calls"], 3)
        self.assertEqual(result["trace"]["rewrite_rounds"], 1)
        self.assertEqual(result["trace"]["model_calls"], 4)
        self.assertEqual(result["claims"], [])

    async def test_no_evidence_and_no_rewrite_stops_after_one_draft(self):
        result = await self.run_qa([[]], [{"claims": [], "gaps": ["库内没有定义"]}])
        self.assertEqual(result["status"], "no_evidence")
        self.assertEqual(result["trace"]["model_calls"], 1)
        self.assertEqual(result["trace"]["retrieval_calls"], 2)
        self.assertEqual(result["evidence"], [])

    async def test_fixed_strategy_keeps_one_search_and_original_rank(self):
        result = await self.run_qa([distractors()], [answer, SUPPORT], strategy="fixed")
        self.assertEqual(result["trace"]["retrieval_calls"], 1)
        self.assertEqual(result["evidence"][0]["source_id"], "trial-0.pdf")

    async def test_supplementary_failure_keeps_primary_candidates(self):
        service = Service(self.store, [distractors()])
        original = service.search
        def search(data):
            if len(service.calls):
                raise TimeoutError()
            return original(data)
        service.search = search
        result = await EvidenceQA(service, configured=lambda: True).ask(self.data)
        self.assertEqual(result["status"], "timeout")
        self.assertTrue(result["evidence"])
        self.assertEqual(result["trace"]["model_calls"], 0)
        self.assertEqual(result["claims"], [])

    async def test_model_permission_failure_is_explained_without_leaking_provider_body(self):
        error = type('OpenAIPermissionDeniedError', (Exception,), {})('PRIVATE PROVIDER BODY')
        result = await self.run_qa([distractors()], [error])
        self.assertEqual(result['status'], 'unavailable')
        self.assertTrue(any('模型访问权限不足' in warning for warning in result['warnings']))
        self.assertEqual(result['trace']['error_category'], 'permission_denied')
        self.assertEqual(result['trace']['model_calls'], 1)
        self.assertTrue(result['evidence'])
        self.assertNotIn('PRIVATE PROVIDER BODY', json.dumps(result))

    async def test_model_rate_limit_keeps_candidates_and_does_not_retry(self):
        error = type('OpenAIRateLimitError', (Exception,), {})('PRIVATE PROVIDER BODY')
        result = await self.run_qa([distractors()], [error])
        self.assertEqual(result['status'], 'unavailable')
        self.assertTrue(any('HTTP 429' in warning for warning in result['warnings']))
        self.assertEqual(result['trace']['error_category'], 'rate_limit')
        self.assertEqual(result['trace']['model_calls'], 1)
        self.assertTrue(result['evidence'])
        self.assertEqual(result['claims'], [])
        self.assertNotIn('PRIVATE PROVIDER BODY', json.dumps(result))

    async def test_default_zotero_both_scope_uses_definition_retrieval_and_keeps_notes_separate(self):
        definition = hit('Heart failure is a synthetic test syndrome.', source_id='definition.pdf')
        service = Service(self.store, [[definition]])
        def search(data):
            service.calls.append(data)
            results = [] if data['scope'] == 'notes' else (
                [definition] if 'definition' in data['query'] else distractors())
            return {'results': results, 'warning': None}
        service.search = search
        model = ScriptedModel([answer, SUPPORT])
        result = await CombinedQA(service, EvidenceQA(service, model, configured=lambda: True),
                                  MemoryQA(service, model, configured=lambda: True)).ask(self.data)
        paper = result['sections'][0]['result']
        self.assertEqual(paper['status'], 'answered')
        self.assertEqual(paper['trace']['strategy'], 'adaptive')
        self.assertEqual(paper['evidence'][0]['source_id'], 'definition.pdf')
        self.assertEqual(result['sections'][1]['result']['claims'], [])
        self.assertEqual(result['trace']['model_calls'], 2)
        self.assertEqual([c['role'] for c in result['trace']['calls']], ['answer', 'review'])

    async def test_both_scope_cannot_mask_model_failure_as_missing_evidence(self):
        service = Service(self.store, [[]])
        error = type('OpenAIPermissionDeniedError', (Exception,), {})('PRIVATE')
        model = ScriptedModel([error])
        result = await CombinedQA(service, EvidenceQA(service, model, configured=lambda: True),
                                  MemoryQA(service, model, configured=lambda: True)).ask(self.data)
        self.assertEqual(result['status'], 'unavailable')
        self.assertEqual(result['trace']['calls'][0]['status'], 'error')
        self.assertEqual(result['trace']['calls'][0]['scope'], 'original')
        self.assertTrue(any('模型访问权限不足' in w for w in result['warnings']))


if __name__ == "__main__":
    unittest.main()
