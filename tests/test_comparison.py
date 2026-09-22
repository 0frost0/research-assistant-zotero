"""Synthetic evidence and injected async agents: no user documents or paid calls."""

import asyncio
import copy
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from starlette.applications import Starlette
from starlette.testclient import TestClient

from research_assistant.web.comparison_api import ComparisonStore, Conflict, make_comparison_routes
from research_assistant.agents.comparison_workflow import ComparisonWorkflow, collect_evidence, call_model
from research_assistant.retrieval.knowledge_base import SearchResult


def documents(count=2):
    return [{"source": f"paper{i}.txt", "error": None, "evidence": [
        {"ref": f"D{i}E1", "source": f"paper{i}.txt", "page": i,
         "quote": "Synthetic study: evaluation settings differ.", "backend": "fixture",
         "evidence_id": f"fixture-{i}", "modality": "text", "quality_warnings": [],
         "context_evidence_ids": [], "context_span": None, "truncated": False}
    ]} for i in range(1, count + 1)]


async def fixture_caller(role, payload):
    if role == "analyst":
        ref = payload["document"]["evidence"][0]["ref"]
        return {"findings": [{"aspect": "评价设置", "statement": "本篇使用特定评价设置，跨研究比较需要统一条件。", "evidence_ids": [ref]}], "gaps": ["当前片段未覆盖样本规模。"]}
    if role == "synthesizer":
        return {"comparisons": [{"aspect": "评价条件", "relation": "incomparable", "statement": "两篇的评价设置不同，不能直接根据当前片段对效果排名。", "evidence_ids": ["D1E1", "D2E1"]}]}
    return {"verdicts": [{"claim_index": 0, "verdict": "supported", "reason": "模拟证据支持设置存在差异，结论未进行效果排名。"}]}


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_parallel_isolation_order_and_maximum_budget(self):
        arrived, roles, payloads = set(), [], []
        gate = asyncio.Event()
        async def caller(role, payload):
            roles.append(role)
            payloads.append((role, copy.deepcopy(payload)))
            if role == "analyst":
                arrived.add(payload["document"]["source"])
                if len(arrived) == 4:
                    gate.set()
                await asyncio.wait_for(gate.wait(), 1)
            return await fixture_caller(role, payload)
        result = await ComparisonWorkflow(caller).run("compare", documents(4))
        self.assertEqual(result["calls"], 6)
        self.assertEqual(roles.count("synthesizer"), 1)
        self.assertEqual(roles.count("reviewer"), 1)
        self.assertEqual(result["review_rounds"], 1)
        self.assertEqual(len(result["comparisons"]), 1)
        self.assertEqual([a["source"] for a in result["analyses"]], [d["source"] for d in documents(4)])
        for role, payload in payloads:
            if role == "analyst":
                self.assertEqual(set(payload), {"question", "document"})
                self.assertEqual(len({e["source"] for e in payload["document"]["evidence"]}), 1)

    async def test_missing_or_image_only_evidence_makes_no_calls(self):
        async def caller(*args):
            self.fail("No model calls without text evidence")
        docs = documents()
        docs[0]["evidence"] = []
        docs[1]["evidence"][0].update(quote="", modality="image")
        result = await ComparisonWorkflow(caller).run("compare", docs)
        self.assertEqual(result["calls"], 0)
        self.assertEqual(result["status"], "degraded")

    async def test_one_failure_preserves_other_papers_and_partial_comparison(self):
        async def caller(role, payload):
            if role == "analyst" and payload["document"]["source"] == "paper3.txt":
                raise ConnectionError("synthetic")
            return await fixture_caller(role, payload)
        result = await ComparisonWorkflow(caller).run("compare", documents(3))
        self.assertEqual(result["calls"], 5)
        self.assertEqual(result["analyses"][2]["status"], "failed")
        self.assertEqual(len(result["comparisons"]), 1)
        self.assertEqual(result["status"], "degraded")

    async def test_analyst_cross_paper_citation_removed(self):
        async def caller(role, payload):
            draft = await fixture_caller(role, payload)
            if role == "analyst":
                draft["findings"][0]["evidence_ids"] = ["D1E1", "D2E1"]
            return draft
        result = await ComparisonWorkflow(caller).run("compare", documents())
        self.assertTrue(all(not a["findings"] for a in result["analyses"]))
        self.assertEqual(result["calls"], 2)

    async def test_synthesis_single_paper_and_unknown_citations_removed(self):
        for refs in (["D1E1"], ["D1E1", "invented"]):
            async def caller(role, payload):
                draft = await fixture_caller(role, payload)
                if role == "synthesizer":
                    draft["comparisons"][0]["evidence_ids"] = refs
                return draft
            result = await ComparisonWorkflow(caller).run("compare", documents())
            self.assertEqual(result["comparisons"], [])
            self.assertEqual(result["calls"], 3)

    async def test_review_missing_duplicate_rejection_and_timeout_fail_closed(self):
        for mode in ("missing", "duplicate", "unsupported", "timeout", "malformed"):
            async def caller(role, payload):
                draft = await fixture_caller(role, payload)
                if role == "reviewer":
                    if mode == "timeout":
                        await asyncio.sleep(2)
                    elif mode == "missing":
                        draft["verdicts"] = []
                    elif mode == "duplicate":
                        draft["verdicts"] *= 2
                    elif mode == "unsupported":
                        draft["verdicts"][0]["verdict"] = "unsupported"
                    else:
                        return {"unexpected": True}
                return draft
            result = await ComparisonWorkflow(caller, call_timeout=.08).run("compare", documents())
            self.assertEqual(result["comparisons"], [], mode)
            self.assertEqual(len(result["withheld"]), 1, mode)
            self.assertEqual(result["calls"], 4, mode)

    async def test_analyst_timeout_and_invalid_output_do_not_retry(self):
        for slow in (True, False):
            async def caller(role, payload):
                if slow:
                    await asyncio.sleep(2)
                return {"malformed": True}
            result = await ComparisonWorkflow(caller, call_timeout=.03).run("compare", documents())
            self.assertEqual(result["calls"], 2)
            self.assertTrue(all(a["status"] == "failed" for a in result["analyses"]))

    async def test_cancellation_reaches_async_agents(self):
        started = asyncio.Event()
        cancelled = []
        async def caller(role, payload):
            started.set()
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled.append(role)
                raise
        task = asyncio.create_task(ComparisonWorkflow(caller).run("compare", documents()))
        await started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(cancelled)

    async def test_real_adapter_uses_structured_async_bounded_output(self):
        from unittest.mock import AsyncMock
        model = Mock()
        model.with_structured_output.return_value.ainvoke = AsyncMock(return_value={
            "parsed": {"findings": [], "gaps": []}, "raw": Mock(usage_metadata=None), "parsing_error": None})
        with patch('research_assistant.agents.comparison_workflow.build_chat_model', return_value=model):
            await call_model("analyst", {"question": "synthetic"})
        self.assertEqual(model.max_tokens, 2600)
        model.with_structured_output.return_value.ainvoke.assert_awaited_once()


class RetrievalTests(unittest.TestCase):
    def test_source_filter_budget_visual_and_raw_text_boundary(self):
        index = Mock()
        def search(question, **opts):
            source = opts["sources"][0]
            base = dict(source=source, page=1, score=.8, backend="multimodal", chunk_id=0)
            return [SearchResult(**base, quote="not for visual inference", modality="image", retrieved_text="PRIVATE RAW", image_path="PRIVATE PATH"),
                    *[SearchResult(**base, quote="x" * 3000) for _ in range(7)],
                    SearchResult(**{**base, "source": "other.txt"}, quote="WRONG SOURCE")]
        index.search_results.side_effect = search
        result = collect_evidence(lambda _: index, "compare", {"backend": "multimodal", "sources": ["a.txt", "b.txt"], "k": 8, "file_types": None, "min_score": .1})
        self.assertEqual(index.search_results.call_count, 2)
        encoded = json.dumps(result)
        self.assertNotIn("PRIVATE", encoded)
        self.assertNotIn("WRONG SOURCE", encoded)
        for doc in result:
            self.assertEqual(doc["evidence"][0]["quote"], "")
            self.assertLessEqual(sum(len(e["quote"]) for e in doc["evidence"]), 6000)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / "library").mkdir()
        for d in documents():
            (self.root / "library" / d["source"]).write_text("synthetic", encoding="utf-8")
        self.payload = {"question": "compare", "sources": [d["source"] for d in documents()], "k": 5, "allow_external": True, "request_id": "a" * 32}
        self.calls = []

    def app(self, caller=None, collector=None, **kwargs):
        async def counting(role, payload):
            self.calls.append(role)
            return await fixture_caller(role, payload)
        def options(data):
            return {"sources": data["sources"], "backend": "tfidf", "k": data["k"], "min_score": .1, "file_types": None}
        return Starlette(routes=make_comparison_routes(self.root, Mock(), options, caller=caller or counting, collector=collector or (lambda q, o: documents()), **kwargs))

    def finished(self, client, run_id):
        for _ in range(150):
            row = client.get("/api/comparisons/" + run_id).json()
            if row["phase"] in {"completed", "failed", "cancelled", "stale", "interrupted"}:
                return row
            time.sleep(.01)
        self.fail("Comparison did not finish")

    def test_permission_validation_and_source_checks(self):
        with TestClient(self.app()) as client:
            self.assertEqual(client.post("/api/comparisons", json={**self.payload, "allow_external": False}).status_code, 403)
            self.assertEqual(client.post("/api/comparisons", json=self.payload, headers={"Origin": "https://untrusted.example"}).status_code, 403)
            for bad in ({"sources": ["paper1.txt"]}, {"sources": ["paper1.txt"] * 2}, {"sources": ["../x", "paper1.txt"]}, {"sources": ["missing.txt", "paper1.txt"]}, {"k": 9}, {"k": 2.5}, {"question": " "}, {"request_id": "bad"}):
                self.assertEqual(client.post("/api/comparisons", json={**self.payload, **bad}).status_code, 400, bad)
            self.assertEqual(client.post("/api/comparisons", json=[]).status_code, 400)
            self.assertEqual(client.post("/api/comparisons", content="x" * 70000, headers={"Content-Type": "application/json"}).status_code, 400)
            self.assertEqual(self.calls, [])

    def test_completed_idempotency_history_and_restart(self):
        with TestClient(self.app()) as client:
            first = client.post("/api/comparisons", json=self.payload)
            self.assertEqual(first.status_code, 202)
            run = self.finished(client, first.json()["id"])
            self.assertEqual(run["phase"], "completed")
            self.assertEqual(run["telemetry"]["status"], "completed")
            self.assertEqual(run["telemetry"]["summary"]["model_calls"], 4)
            self.assertEqual(len(run["result"]["comparisons"]), 1)
            self.assertNotIn("source_versions", run)
            client.post("/api/comparisons", json=self.payload)
            self.assertEqual(len(self.calls), 4)
            self.assertEqual(client.post("/api/comparisons", json={**self.payload, "question": "changed"}).status_code, 409)
            self.assertEqual(len(client.get("/api/comparisons").json()["runs"]), 1)
        with TestClient(self.app()) as client:
            self.assertEqual(client.get("/api/comparisons/" + run["id"]).json()["result"], run["result"])
        self.assertEqual(len(self.calls), 4)

    def test_single_active_run_cancel_and_no_terminal_overwrite(self):
        async def slow(role, payload):
            self.calls.append(role)
            await asyncio.sleep(5)
        with TestClient(self.app(caller=slow)) as client:
            run = client.post("/api/comparisons", json=self.payload).json()
            duplicate = client.post("/api/comparisons", json={**self.payload, "request_id": "b" * 32})
            self.assertEqual(duplicate.json()["id"], run["id"])
            self.assertEqual(client.post("/api/comparisons", json={**self.payload, "request_id": "c" * 32, "question": "other"}).status_code, 409)
            response = client.post("/api/comparisons/" + run["id"], json={"action": "cancel"})
            self.assertEqual(response.json()["phase"], "cancelled")
            time.sleep(.05)
            self.assertEqual(self.finished(client, run["id"])["phase"], "cancelled")
            trace = client.get("/api/comparisons/" + run["id"]).json()["telemetry"]
            if trace:
                self.assertEqual(trace["status"], "cancelled")
                self.assertTrue(all(s["status"] != "running" for s in trace["spans"]))
            self.assertNotIn("synthesizer", self.calls)

    def test_total_timeout_retains_evidence(self):
        async def slow(role, payload):
            await asyncio.sleep(5)
        with TestClient(self.app(caller=slow, total_timeout=.15)) as client:
            run = client.post("/api/comparisons", json=self.payload).json()
            result = self.finished(client, run["id"])
            self.assertEqual(result["phase"], "failed")
            self.assertEqual(len(result["documents"]), 2)
            self.assertEqual(result["telemetry"]["status"], "failed")

    def test_retrieval_timeout_never_starts_model_and_blocks_thread_accumulation(self):
        def slow(q, o):
            time.sleep(.5)
            return documents()
        with TestClient(self.app(collector=slow, retrieval_timeout=.04)) as client:
            run = client.post("/api/comparisons", json=self.payload).json()
            self.assertEqual(self.finished(client, run["id"])["phase"], "failed")
            self.assertEqual(client.post("/api/comparisons", json={**self.payload, "request_id": "b" * 32}).status_code, 409)
            time.sleep(.55)
            self.assertEqual(self.calls, [])

    def test_file_change_after_retrieval_stops_before_model(self):
        def changed(q, o):
            (self.root / "library/paper1.txt").write_text("changed synthetic")
            return documents()
        with TestClient(self.app(collector=changed)) as client:
            run = client.post("/api/comparisons", json=self.payload).json()
            self.assertEqual(self.finished(client, run["id"])["phase"], "stale")
            self.assertEqual(self.calls, [])

    def test_unfinished_record_restart_is_interrupted_without_execution(self):
        path = self.root / ".data/comparisons.sqlite3"
        store = ComparisonStore(path)
        run, _ = store.reserve("d" * 32, "compare", {"sources": ["paper1.txt", "paper2.txt"]}, {})
        store.update(run["id"], calls=1, analyses=[{"source": "paper1.txt"}])
        restarted = ComparisonStore(path)
        self.assertEqual(restarted.get(run["id"])["phase"], "interrupted")
        self.assertEqual(restarted.get(run["id"])["calls"], 1)
        restarted.update(run["id"], phase="completed")
        self.assertEqual(restarted.get(run["id"])["phase"], "interrupted")


if __name__ == "__main__":
    unittest.main()
