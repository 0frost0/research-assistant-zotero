"""Observability checks never infer usage, persist prompts, or call real models."""

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from langchain_core.exceptions import OutputParserException

from research_assistant.core.agent_telemetry import ExecutionTrace, ModelReply, interrupted_trace, normalize_usage, summarize, token_usage
from research_assistant.agents.comparison_workflow import ComparisonWorkflow, call_model
from tests.test_comparison import documents, fixture_caller


USAGE = {"input_tokens": 100, "output_tokens": 20, "total_tokens": 120}


class UsageTests(unittest.TestCase):
    def test_missing_bad_or_inconsistent_usage_is_unknown(self):
        for value in (None, {}, {**USAGE, "total_tokens": 0}, {**USAGE, "input_tokens": True}, {**USAGE, "output_tokens": -1}, {**USAGE, "total_tokens": 120.0}):
            self.assertIsNone(normalize_usage(value))

    def test_cached_input_is_not_double_counted(self):
        raw = SimpleNamespace(usage_metadata={**USAGE, "input_token_details": {"cache_read": 60}, "secret": "do not save"})
        usage = token_usage(raw)
        self.assertEqual(usage["total_tokens"], 120)
        self.assertEqual(usage["cache_read_tokens"], 60)
        self.assertNotIn("secret", usage)

    def test_monotonic_timing_queue_and_snapshot_isolation(self):
        clock = Mock(return_value=10)
        trace = ExecutionTrace(clock=clock)
        clock.return_value = 10.5
        span = trace.start("analyst", "model")
        clock.return_value = 11
        trace.requested(span)
        clock.return_value = 13
        trace.end(span)
        trace.finish("completed")
        clock.return_value = 20
        snapshot = trace.snapshot()
        self.assertEqual(snapshot["summary"]["wall_ms"], 3000)
        self.assertEqual(snapshot["spans"][0]["queue_ms"], 500)
        self.assertEqual(snapshot["spans"][0]["request_duration_ms"], 2000)
        snapshot["spans"][0]["name"] = "changed"
        self.assertEqual(span["name"], "analyst")

    def test_adjacent_calls_are_not_concurrent(self):
        spans = [{"kind": "model", "request_start_ms": a, "end_ms": b, "status": "ok"} for a, b in [(0, 10), (10, 20)]]
        self.assertEqual(summarize(spans, 20)["peak_model_concurrency"], 1)

    def test_restart_does_not_invent_finished_duration(self):
        trace = ExecutionTrace()
        trace.start("analyst", "model")
        recovered = interrupted_trace(trace.snapshot())
        self.assertEqual(recovered["status"], "interrupted")
        self.assertIsNone(recovered["spans"][0]["duration_ms"])
        self.assertTrue(recovered["summary"]["timing_incomplete"])

    def test_waiting_for_slot_does_not_count_as_sent_model_call(self):
        trace = ExecutionTrace()
        span = trace.start("analyst", "model")
        trace.end(span, "cancelled")
        self.assertEqual(trace.snapshot()["summary"]["model_calls"], 0)


class ExperimentTests(unittest.TestCase):
    def test_live_requires_separate_permission_and_call_ceiling(self):
        from benchmarks.run_comparison_experiment import authorize_live, synthetic_cases
        cases = synthetic_cases()
        for options in ({"allow_external": False}, {"max_model_calls": 0}, {"dataset": None}):
            args = SimpleNamespace(**{**dict(dataset="snapshot.json", allow_external=True, max_model_calls=60, repeats=3), **options})
            with self.assertRaises(ValueError):
                authorize_live(args, cases)
        self.assertEqual(authorize_live(SimpleNamespace(dataset="snapshot.json", allow_external=True, max_model_calls=60, repeats=3), cases), 60)

    def test_snapshot_must_have_unambiguous_source_and_reference(self):
        from benchmarks.run_comparison_experiment import Case, synthetic_cases
        data = synthetic_cases()[0].model_dump()
        data["documents"][0]["evidence"][0]["source"] = "another.txt"
        with self.assertRaises(ValueError):
            Case.model_validate(data)

    def test_nearest_rank_p95_is_sample_max_for_three_repeats(self):
        from benchmarks.run_comparison_experiment import percentile
        self.assertEqual(percentile([100, 10, 20], .95), 100)


class TraceWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_reported_usage_and_hierarchy_without_content(self):
        async def caller(role, payload):
            return ModelReply(await fixture_caller(role, payload), USAGE)
        result = await ComparisonWorkflow(caller).run("PRIVATE QUESTION", documents())
        trace = result["telemetry"]
        self.assertEqual(trace["summary"]["known_tokens"]["total_tokens"], 480)
        self.assertTrue(trace["summary"]["usage_complete"])
        self.assertEqual(trace["status"], "completed")
        self.assertEqual(len(trace["prompt_sha256"]), 64)
        self.assertNotIn("PRIVATE QUESTION", json.dumps(trace))
        self.assertNotIn("Synthetic study", json.dumps(trace))
        ids = {s["id"] for s in trace["spans"] if s["kind"] == "phase"}
        self.assertTrue(all(s["parent_id"] in ids for s in trace["spans"] if s["kind"] == "model"))

    async def test_simulated_usage_is_not_provider_consumption(self):
        async def caller(role, payload):
            return ModelReply(await fixture_caller(role, payload), USAGE, "simulated")
        stats = (await ComparisonWorkflow(caller).run("compare", documents()))["telemetry"]["summary"]
        self.assertIsNone(stats["known_tokens"])
        self.assertEqual(stats["unknown_usage_calls"], 4)

    async def test_partial_usage_never_looks_complete(self):
        async def caller(role, payload):
            return ModelReply(await fixture_caller(role, payload), None if role == "reviewer" else USAGE)
        stats = (await ComparisonWorkflow(caller).run("compare", documents()))["telemetry"]["summary"]
        self.assertEqual(stats["known_tokens"]["total_tokens"], 360)
        self.assertEqual(stats["unknown_usage_calls"], 1)
        self.assertFalse(stats["usage_complete"])

    async def test_parse_error_keeps_usage_not_response_or_error_text(self):
        async def caller(role, payload):
            return ModelReply(None, USAGE, parsing_error=OutputParserException("PRIVATE RESPONSE / TOKEN"))
        trace = (await ComparisonWorkflow(caller).run("compare", documents()))["telemetry"]
        self.assertEqual(trace["summary"]["known_tokens"]["total_tokens"], 240)
        self.assertEqual(trace["summary"]["failed_calls"], 2)
        self.assertNotIn("PRIVATE", json.dumps(trace))
        self.assertTrue(all(s["error_category"] == "invalid_output" for s in trace["spans"] if s["kind"] == "model"))

    async def test_serial_and_parallel_have_same_calls_but_different_overlap(self):
        async def caller(role, payload):
            await asyncio.sleep(.035)
            return await fixture_caller(role, payload)
        results = [await ComparisonWorkflow(caller, analyst_concurrency=c).run("compare", documents(4)) for c in (1, 4)]
        self.assertEqual([r["calls"] for r in results], [6, 6])
        self.assertEqual([r["telemetry"]["summary"]["peak_model_concurrency"] for r in results], [1, 4])
        self.assertEqual(results[0]["comparisons"], results[1]["comparisons"])
        serial_analysts = [s for s in results[0]["telemetry"]["spans"] if s["name"] == "analyst"]
        self.assertGreater(max(s["queue_ms"] for s in serial_analysts), 20)

    async def test_adapter_reads_usage_and_keeps_parsing_failure_envelope(self):
        model = Mock()
        error = OutputParserException("private")
        model.with_structured_output.return_value.ainvoke = AsyncMock(return_value={"raw": SimpleNamespace(usage_metadata=USAGE), "parsed": None, "parsing_error": error})
        with patch('research_assistant.agents.comparison_workflow.build_chat_model', return_value=model):
            reply = await call_model("analyst", {"question": "test"})
        self.assertEqual(reply.usage, USAGE)
        self.assertIs(reply.parsing_error, error)
        self.assertTrue(model.with_structured_output.call_args.kwargs["include_raw"])


if __name__ == "__main__":
    unittest.main()
