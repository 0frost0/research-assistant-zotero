"""Deterministic accounting tests: never contact a model service."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from research_assistant.translation.ledger import TranslationLedger, BudgetStopped, metered_create


def response(reason="stop", usage=None):
    return SimpleNamespace(usage=SimpleNamespace(model_dump=lambda: usage or {
        "prompt_tokens": 70, "completion_tokens": 30, "total_tokens": 100}),
        choices=[SimpleNamespace(finish_reason=reason, message=SimpleNamespace(content="模拟中文"))])


class BudgetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ledger = TranslationLedger(Path(self.tmp.name) / "reading.sqlite3")
        self.ledger.account("job", 10000, 3)
        self.ledger.start("job", 1)

    def call(self, create=None):
        return metered_create(create or Mock(return_value=response()), self.ledger, "job", 1)

    def test_actual_boundary_caps_output_and_records_usage(self):
        create = Mock(return_value=response())
        self.call(create)(model="fixture", messages=[{"role": "user", "content": "private fixture text"}], max_tokens=99999)
        self.assertEqual(create.call_args.kwargs["max_tokens"], 2048)
        s = self.ledger.summary("job")
        self.assertEqual((s["requests"], s["reported_tokens"], s["unknown_reserved_tokens"]), (1, 100, 0))
        with self.ledger.transaction() as con:
            self.assertNotIn("private fixture", str([tuple(r) for r in con.execute("SELECT * FROM translation_requests")]))

    def test_concurrent_reservations_do_not_exceed_request_cap(self):
        def reserve(_):
            try:
                return self.ledger.reserve("job", 1, {"text": "fixture"})
            except BudgetStopped:
                return None
        with ThreadPoolExecutor(max_workers=8) as pool:
            ids = list(pool.map(reserve, range(8)))
        self.assertEqual(sum(x is not None for x in ids), 3)
        self.assertLessEqual(self.ledger.summary("job")["accounted_tokens"], 10000)

    def test_large_input_blocked_before_provider(self):
        create = Mock(return_value=response())
        with self.assertRaises(BudgetStopped):
            self.call(create)(messages=[{"content": "x" * 15000}])
        create.assert_not_called()

    def test_waits_for_inflight_reservation_to_settle(self):
        import time
        self.ledger.account("small", 3000, 3)
        self.ledger.start("small", 1)
        first = self.ledger.reserve("small", 1, {})
        with ThreadPoolExecutor(max_workers=1) as pool:
            waiting = pool.submit(self.ledger.reserve, "small", 1, {})
            time.sleep(.1)
            self.assertFalse(waiting.done())
            self.ledger.finish(first, usage={"total_tokens": 100})
            self.assertTrue(waiting.result(timeout=2))
        self.assertIsNone(self.ledger.summary("small")["blocked"])

    def test_failed_unknown_usage_retained_on_restart_and_retry(self):
        with self.assertRaises(ConnectionError):
            self.call(Mock(side_effect=ConnectionError("SECRET")))(messages=[])
        before = self.ledger.summary("job")["accounted_tokens"]
        self.ledger.recover()
        rebuilt = TranslationLedger(self.ledger.path)
        rebuilt.start("job", 2)
        metered_create(Mock(return_value=response()), rebuilt, "job", 2)(messages=[])
        s = rebuilt.summary("job")
        self.assertEqual(s["accounted_tokens"], before + 100)
        self.assertEqual(len(s["attempts"]), 2)
        self.assertEqual(s["unknown_reserved_tokens"], before)

    def test_crash_pending_is_interrupted_not_free(self):
        ident = self.ledger.reserve("job", 1, {})
        before = self.ledger.summary("job")["accounted_tokens"]
        self.ledger.recover()
        self.assertEqual(self.ledger.summary("job")["accounted_tokens"], before)
        with self.assertRaises(BudgetStopped):
            self.ledger.finish(ident, usage={"total_tokens": 1})

    def test_truncation_records_usage_then_stops_fallback(self):
        create = Mock(return_value=response("length"))
        call = self.call(create)
        for _ in range(2):
            with self.assertRaises(BudgetStopped):
                call(messages=[])
        self.assertEqual(create.call_count, 1)
        self.assertEqual(self.ledger.summary("job")["reported_tokens"], 100)
        self.assertEqual(self.ledger.summary("job")["blocked"], "output_truncated")

    def test_response_diagnostics_separate_failure_reasons_without_content(self):
        for index, (reason, content, expected) in enumerate((
            ("length", "partial secret", "output_truncated"),
            ("stop", " \n ", "empty_response"),
            ("content_filter", None, "content_filtered"),
            ("private provider value", None, "invalid_response"),
            ("stop", "valid synthetic translation", None),
        )):
            with self.subTest(reason=reason):
                job = "diagnostics-" + str(index)
                self.ledger.account(job)
                self.ledger.start(job, 1)
                result = response(reason, {
                    "total_tokens": 100, "completion_tokens": 80,
                    "completion_tokens_details": {"reasoning_tokens": 60, "secret": "private"},
                })
                result.choices[0].message.content = content
                result.choices[0].message.reasoning_content = "private reasoning"
                call = metered_create(Mock(return_value=result), self.ledger, job, 1)
                if expected:
                    with self.assertRaisesRegex(BudgetStopped, expected):
                        call(messages=[])
                else:
                    call(messages=[])
                with self.ledger.transaction() as con:
                    row = con.execute(
                        "SELECT d.metadata FROM translation_response_diagnostics d JOIN translation_requests r "
                        "ON r.id=d.request_id WHERE r.job_id=?", (job,),
                    ).fetchone()
                metadata = json.loads(row[0])
                self.assertEqual(metadata["reasoning_tokens"], 60)
                self.assertEqual(metadata["content_chars"], len(content or ""))
                self.assertNotIn("private", row[0])
                self.assertNotIn("synthetic", row[0])
                self.assertEqual(self.ledger.summary(job)["reported_tokens"], 100)
                self.assertEqual(self.ledger.summary(job)["blocked"], expected)

    def test_missing_choices_recorded_and_blocked(self):
        result = response()
        result.choices = []
        with self.assertRaisesRegex(BudgetStopped, "invalid_response"):
            self.call(Mock(return_value=result))(messages=[])
        self.assertEqual(self.ledger.summary("job")["unknown_requests"], 0)

    def test_first_fatal_reason_preserved_as_inflight_requests_settle(self):
        first = self.ledger.reserve("job", 1, {"n": 1})
        second = self.ledger.reserve("job", 1, {"n": 2})
        self.ledger.finish(first, error="output_truncated", halt=True)
        self.assertEqual(self.ledger.activity("job"), ("output_truncated", 1))
        self.ledger.finish(second, error="empty_response", halt=True)
        self.assertEqual(self.ledger.activity("job"), ("output_truncated", 0))

    def test_diagnostic_storage_whitelists_fields_and_types(self):
        ident = self.ledger.reserve("job", 1, {})
        self.ledger.finish(ident, diagnostics={
            "finish_reason": "private response", "content_chars": -1,
            "reasoning_chars": True, "reasoning_tokens": "private tokens",
            "content": "private translation",
        })
        with self.ledger.transaction() as con:
            metadata = json.loads(con.execute("SELECT metadata FROM translation_response_diagnostics").fetchone()[0])
        self.assertEqual(metadata, {"finish_reason": "unknown"})

    def test_normal_full_translation_exceeds_old_daily_and_job_limits(self):
        for job in ("unlimited", "another"):
            self.ledger.account(job)
            self.ledger.start(job, 1)
            create = Mock(return_value=response(usage={"prompt_tokens": 800, "completion_tokens": 300, "total_tokens": 1100}))
            call = metered_create(create, self.ledger, job, 1)
            for paragraph in range(60):
                call(messages=[{"content": str(paragraph)}])
            summary = self.ledger.summary(job)
            self.assertEqual((summary["requests"], summary["reported_tokens"]), (60, 66000))
            self.assertIsNone(summary["token_limit"])
            self.assertIsNone(summary["blocked"])

    def test_identical_requests_bounded_atomically_without_total_limit(self):
        self.ledger.account("unlimited")
        self.ledger.start("unlimited", 1)
        def reserve(_):
            try:
                return self.ledger.reserve("unlimited", 1, {"text": "same request"})
            except BudgetStopped:
                return None
        with ThreadPoolExecutor(max_workers=10) as pool:
            ids = list(pool.map(reserve, range(12)))
        self.assertEqual(sum(i is not None for i in ids), 6)
        self.assertEqual(self.ledger.summary("unlimited")["blocked"], "repeated_request")

    def test_three_identical_failures_stop_next_provider_call(self):
        self.ledger.account("unlimited")
        self.ledger.start("unlimited", 1)
        create = Mock(side_effect=ConnectionError("mock offline"))
        call = metered_create(create, self.ledger, "unlimited", 1)
        for _ in range(3):
            with self.assertRaises(ConnectionError): call(messages=[])
        with self.assertRaisesRegex(BudgetStopped, "repeated_request"): call(messages=[])
        self.assertEqual(create.call_count, 3)

    def test_distinct_continuous_failures_open_circuit(self):
        self.ledger.account("unlimited")
        self.ledger.start("unlimited", 1)
        create = Mock(side_effect=ConnectionError("mock offline"))
        call = metered_create(create, self.ledger, "unlimited", 1)
        for i in range(8):
            with self.assertRaises(ConnectionError): call(messages=[{"content": str(i)}])
        with self.assertRaisesRegex(BudgetStopped, "consecutive_failures"):
            call(messages=[{"content": "next"}])
        self.assertEqual(create.call_count, 8)

    def test_usage_per_attempt_separates_unknown_from_reported(self):
        self.call()(messages=[])
        self.ledger.recover("job", "failed")
        self.ledger.start("job", 2)
        call = metered_create(Mock(return_value=response()), self.ledger, "job", 2)
        call(messages=[])
        ident = self.ledger.reserve("job", 2, {"different": True})
        self.ledger.recover("job")
        summary = self.ledger.summary("job")
        self.assertEqual(summary["reported_tokens"], 200)
        self.assertEqual(summary["unknown_requests"], 1)
        self.assertEqual(summary["attempts"][1]["reported_tokens"], 100)
        self.assertEqual(summary["attempts"][1]["unknown_requests"], 1)

    def test_legacy_limit_migrates_without_losing_usage(self):
        import sqlite3
        path = Path(self.tmp.name) / "legacy.sqlite3"
        with sqlite3.connect(path) as con:
            con.execute("CREATE TABLE translation_budget_schema(version INTEGER PRIMARY KEY)")
            con.execute("INSERT INTO translation_budget_schema VALUES(1)")
            con.execute("CREATE TABLE translation_budgets(job_id TEXT PRIMARY KEY,tokens INTEGER NOT NULL,requests INTEGER NOT NULL,blocked TEXT)")
            con.execute("INSERT INTO translation_budgets VALUES('old',20000,20,'budget_exhausted')")
        con.close()
        ledger = TranslationLedger(path)
        ledger.start("old", 2)
        call = metered_create(Mock(return_value=response()), ledger, "old", 2)
        for i in range(25): call(messages=[{"content": str(i)}])
        rebuilt = TranslationLedger(path)
        self.assertEqual(rebuilt.summary("old")["reported_tokens"], 2500)
        self.assertIsNone(rebuilt.summary("old")["token_limit"])

    def test_storage_failure_prevents_model_request(self):
        create = Mock(return_value=response())
        with patch.object(self.ledger, "reserve", side_effect=OSError("disk failure")):
            with self.assertRaises(OSError):
                self.call(create)(messages=[])
        create.assert_not_called()

    def test_duplicate_account_does_not_reset_spending_or_budget(self):
        self.call()(messages=[])
        self.ledger.account("job", 50000, 50)
        s = self.ledger.summary("job")
        self.assertEqual((s["token_limit"], s["request_limit"], s["reported_tokens"]), (10000, 3, 100))

    def test_legacy_usage_is_unknown(self):
        self.assertIsNone(self.ledger.summary("old"))


if __name__ == "__main__":
    unittest.main()
