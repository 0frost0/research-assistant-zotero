"""Run in .translation_env; all HTTP is mocked, no credentials or model calls."""
import logging
from pathlib import Path
import sys
import unittest
import multiprocessing
import importlib.util
import os
import json
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "deployment/translation"))

AVAILABLE = importlib.util.find_spec("pdf2zh_next") is not None
if AVAILABLE:
    import pdf2zh_next
    import httpx
    import openai
    from pdf2zh_next.config.model import SettingsModel
    from research_assistant.translation.policy import engine_settings
    from worker import SafeDiagnostics, preflight
    from pdf2zh_next.translator.translator_impl.openai import OpenAITranslator


def child_policy(queue):
    queue.put({"bounded": getattr(OpenAITranslator, "bounded_by_reader", False),
               "thinking": engine_settings(TranslationWorkerTests.request)._openai_extra_body})


def child_meter(queue):
    # The context and installed adapter must survive Windows spawn, not just threads.
    from research_assistant.translation.ledger import TranslationLedger
    from unittest.mock import Mock
    cfg = json.loads(os.environ["RESEARCH_FULL_TRANSLATION_CONTEXT"])
    real = openai.OpenAI
    def client(**kwargs):
        return real(api_key="mock", base_url="https://example.invalid/v1", max_retries=0,
                    http_client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={
                        "id": "fixture", "object": "chat.completion", "created": 0, "model": "fixture",
                        "usage": {"prompt_tokens": 70, "completion_tokens": 30, "total_tokens": 100},
                        "choices": [{"index": 0, "message": {"role": "assistant", "content": "模拟中文"}, "finish_reason": "stop"}]}))))
    with patch("openai.OpenAI", side_effect=client):
        translator = OpenAITranslator(SettingsModel(translate_engine_settings=engine_settings(TranslationWorkerTests.request)), Mock())
    try:
        queue.put(translator.do_llm_translate("spawn fixture"))
    finally:
        translator.client.close()


@unittest.skipUnless(AVAILABLE, "requires isolated pinned translation environment")
class TranslationWorkerTests(unittest.TestCase):
    request = {"model": "deepseek-v4-flash", "base_url": "https://api.deepseek.com/v1", "key": "test-placeholder"}

    def translator(self, handler):
        settings = SettingsModel(translate_engine_settings=engine_settings(self.request))
        translator = OpenAITranslator(settings, None)
        translator.client.close()
        translator.client = openai.OpenAI(api_key="test-placeholder", base_url="https://example.invalid/v1",
            http_client=httpx.Client(transport=httpx.MockTransport(handler)), max_retries=0)
        translator.do_llm_translate.retry.sleep = lambda seconds: None
        self.addCleanup(translator.client.close)
        return translator

    def test_thinking_is_disabled_and_endpoint_preserved(self):
        settings = engine_settings(self.request)
        self.assertEqual(settings._openai_extra_body, {"thinking": {"type": "disabled"}})
        self.assertEqual(settings.openai_base_url, self.request["base_url"])
        self.assertEqual(settings.openai_timeout, "90")

    def test_windows_spawn_keeps_retry_shim(self):
        context = multiprocessing.get_context("spawn")
        queue = context.Queue()
        process = context.Process(target=child_policy, args=(queue,))
        process.start()
        try:
            self.assertEqual(queue.get(timeout=20), {"bounded": True, "thinking": {"thinking": {"type": "disabled"}}})
            process.join(timeout=10)
            self.assertEqual(process.exitcode, 0)
        finally:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
            queue.close()

    def test_spawned_sdk_request_is_durably_metered(self):
        from research_assistant.translation.ledger import TranslationLedger
        with tempfile.TemporaryDirectory() as tmp:
            ledger = TranslationLedger(Path(tmp) / "reading.sqlite3")
            ledger.account("fixture", 10000, 10)
            ledger.start("fixture", 1)
            with patch.dict(os.environ, {"RESEARCH_FULL_TRANSLATION_CONTEXT": json.dumps({
                    "database": ledger.path, "job_id": "fixture", "attempt": 1, "endpoint_hash": "fixture"})}):
                context = multiprocessing.get_context("spawn")
                queue = context.Queue()
                process = context.Process(target=child_meter, args=(queue,))
                process.start()
                try:
                    self.assertEqual(queue.get(timeout=30), "模拟中文")
                    process.join(timeout=10)
                    self.assertEqual(process.exitcode, 0)
                finally:
                    if process.is_alive(): process.terminate(); process.join(timeout=5)
                    queue.close()
            summary = ledger.summary("fixture")
            self.assertEqual((summary["requests"], summary["reported_tokens"]), (1, 100))

    def test_cache_survives_translator_recreation_and_attempt_restart(self):
        from research_assistant.translation.ledger import TranslationLedger
        from pdf2zh_next.translator import cache as cache_module
        from unittest.mock import Mock
        test_db = cache_module.init_test_db()
        clients = []
        try:
            with tempfile.TemporaryDirectory() as tmp:
                ledger = TranslationLedger(Path(tmp) / "reading.sqlite3")
                ledger.account("cache", 10000, 10)
                ledger.start("cache", 1)
                cfg = {"database": ledger.path, "job_id": "cache", "attempt": 1, "endpoint_hash": "fixture"}
                real = openai.OpenAI
                def client(**kwargs):
                    result = real(api_key="mock", base_url="https://example.invalid/v1", max_retries=0,
                        http_client=httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={
                            "id": "fixture", "object": "chat.completion", "created": 0, "model": "fixture",
                            "usage": {"prompt_tokens": 70, "completion_tokens": 30, "total_tokens": 100},
                            "choices": [{"index": 0, "message": {"role": "assistant", "content": "缓存模拟译文"}, "finish_reason": "stop"}]}))))
                    clients.append(result)
                    return result
                settings = SettingsModel(translate_engine_settings=engine_settings(self.request))
                with patch("openai.OpenAI", side_effect=client):
                    with patch.dict(os.environ, {"RESEARCH_FULL_TRANSLATION_CONTEXT": json.dumps(cfg)}):
                        first = OpenAITranslator(settings, Mock())
                    self.assertEqual(first.llm_translate("synthetic cache fixture"), "缓存模拟译文")
                    ledger.recover()
                    ledger.start("cache", 2)
                    cfg["attempt"] = 2
                    with patch.dict(os.environ, {"RESEARCH_FULL_TRANSLATION_CONTEXT": json.dumps(cfg)}):
                        second = OpenAITranslator(settings, Mock())
                    self.assertEqual(second.llm_translate("synthetic cache fixture"), "缓存模拟译文")
                    self.assertEqual(ledger.summary("cache")["requests"], 1)
                    # A genuine miss on the resumed attempt is still metered.
                    second.do_translate("another synthetic paragraph")
                    self.assertEqual(ledger.summary("cache")["requests"], 2)
        finally:
            for client in clients: client.close()
            cache_module.clean_test_db(test_db)
            cache_module.db.bind([cache_module._TranslationCache], bind_refs=False, bind_backrefs=False)

    def test_other_provider_does_not_receive_deepseek_options(self):
        settings = engine_settings({**self.request, "base_url": "https://example.invalid/v1"})
        self.assertIsNone(settings._openai_extra_body)

    def test_preflight_rejects_empty_balance_without_model_call(self):
        with patch("httpx.get", return_value=httpx.Response(200, json={"is_available": False},
                   request=httpx.Request("GET", "https://example.invalid"))) as get:
            self.assertEqual(preflight(self.request), "insufficient_balance")
            self.assertEqual(get.call_count, 1)

    def test_preflight_reports_network_timeout(self):
        with patch("httpx.get", side_effect=httpx.ReadTimeout("do not log this")):
            self.assertEqual(preflight(self.request), "request_timeout")

    def test_billing_error_is_not_retried_and_stops_later_requests(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(402, json={"error": {"message": "Insufficient Balance", "type": "billing"}})
        translator = self.translator(handler)
        for _ in range(2):
            with self.assertRaises(openai.APIStatusError):
                translator.do_llm_translate("synthetic input")
        self.assertEqual(len(calls), 1)

    def test_rate_limit_stops_after_three_attempts(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(429, json={"error": {"message": "rate limited"}})
        translator = self.translator(handler)
        with self.assertRaises(openai.RateLimitError):
            translator.do_llm_translate("synthetic input")
        self.assertEqual(len(calls), 3)

    def test_transient_server_error_recovers(self):
        calls = []
        def handler(request):
            calls.append(request)
            if len(calls) == 1:
                return httpx.Response(503, json={"error": {"message": "temporary"}})
            return httpx.Response(200, json={"id": "test", "object": "chat.completion", "created": 0,
                "model": "test", "choices": [{"index": 0, "message": {"role": "assistant", "content": "测试译文"}, "finish_reason": "stop"}]})
        self.assertEqual(self.translator(handler).do_llm_translate("synthetic input"), "测试译文")
        self.assertEqual(len(calls), 2)

    def test_diagnostics_do_not_store_sensitive_messages(self):
        handler = SafeDiagnostics()
        handler.emit(logging.LogRecord("test", logging.ERROR, "", 0,
            "Error translating paragraph. PRIVATE PAPER TEXT Error code: 402", (), None))
        self.assertEqual(handler.failure_code, "insufficient_balance")
        self.assertEqual(dict(handler.counts), {"paragraph_errors": 1})
        self.assertNotIn("PRIVATE", str(vars(handler)))


if __name__ == "__main__":
    unittest.main()
