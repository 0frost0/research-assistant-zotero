"""Selection translation tests use synthetic text and mocked providers only."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from tests.test_reading_memory import pdf
from research_assistant.memory.store import ReadingStore, Conflict
from research_assistant.translation.selection import SelectionTranslator, invoke_model, PROMPT


class SelectionTranslationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        (root / "library").mkdir()
        path = root / "library/synthetic.pdf"
        pdf(path)
        self.store = ReadingStore(root)
        self.paper = self.store.register(path, "synthetic.pdf")
        original = self.store.artifact(self.paper["original_id"])
        self.anchor = {"artifact_id": original["id"], "artifact_hash": original["hash"],
            "page": 1, "coordinate_system": "pdf_user_space", "view_box": [0, 0, 600, 800],
            "rects": [[60, 680, 300, 702]], "excerpt": "Scientific evidence requires careful validation."}
        self.cfg = {"enabled": True, "key": "test-placeholder", "model": "deepseek-v4-flash", "base_url": "https://api.deepseek.com/v1"}
        self.config_patch = patch("research_assistant.translation.selection.configuration", return_value=self.cfg)
        self.config_patch.start()
        self.addCleanup(self.config_patch.stop)
        self.provider = Mock(return_value={"text": "科学证据需要仔细核验。", "finish_reason": "stop",
                                           "usage": {"prompt_tokens": 90, "completion_tokens": 10, "total_tokens": 100}})
        self.service = SelectionTranslator(self.store, self.provider)
        self.data = {"paper_id": self.paper["id"], "anchor": self.anchor, "allow_external": True}

    def test_one_request_then_persistent_offline_cache(self):
        first = self.service.translate(self.data)
        self.assertFalse(first["cached"])
        self.assertEqual(self.provider.call_args.args[1], self.anchor["excerpt"])
        restarted = SelectionTranslator(ReadingStore(self.store.root), Mock(side_effect=AssertionError("no HTTP")))
        self.cfg["key"] = ""
        cached = restarted.translate({**self.data, "allow_external": False})
        self.assertTrue(cached["cached"])
        self.assertEqual(cached["id"], first["id"])
        self.assertEqual(restarted.status()["requests_today"], 1)
        self.assertEqual(restarted.status()["accounted_tokens_today"], 100)

    def test_only_selected_text_and_no_automatic_call(self):
        with self.assertRaisesRegex(ValueError, "本地暂无缓存"):
            self.service.translate({**self.data, "allow_external": False})
        self.provider.assert_not_called()
        self.assertEqual(self.service.status()["requests_today"], 0)
        with self.assertRaises(ValueError):
            self.service.translate({**self.data, "anchor": {**self.anchor, "excerpt": "x" * 2401}})
        self.provider.assert_not_called()

    def test_current_anchor_not_cached_rectangle_is_used(self):
        first = self.service.translate(self.data)
        second_anchor = {**self.anchor, "rects": [[60, 650, 300, 672]]}
        second = self.service.translate({**self.data, "anchor": second_anchor})
        self.assertTrue(second["cached"])
        self.assertEqual(second["anchor"]["rects"], second_anchor["rects"])
        note = self.service.draft(first["id"], {**self.data, "anchor": second_anchor})
        self.assertEqual(note["anchors"][0]["rects"], second_anchor["rects"])

    def test_draft_is_not_user_view_and_confirmation_keeps_provenance(self):
        translated = self.service.translate(self.data)
        note = self.service.draft(translated["id"], self.data)
        self.assertFalse(note["confirmed"])
        self.assertEqual(note["source_kind"], "ai_draft")
        self.assertEqual(self.store.search("科学证据"), [])
        confirmed = self.store.save_note({"confirm": True, "expected_revision": 1}, note["id"])
        self.assertEqual(confirmed["author_source"], "ai_assisted")
        self.assertEqual(confirmed["generation_source"]["id"], translated["id"])
        self.assertEqual(len(self.store.search("科学证据")), 1)
        self.assertEqual(len(self.store.history(note["id"])), 2)

    def test_failure_does_not_retry_or_refund_unknown_usage(self):
        self.provider.side_effect = RuntimeError("SECRET provider payload")
        with self.assertRaisesRegex(ValueError, "未自动重试"):
            self.service.translate(self.data)
        self.assertEqual(self.provider.call_count, 1)
        with self.store.read() as con:
            row = con.execute("SELECT * FROM selection_translations").fetchone()
        self.assertEqual(row["state"], "failed")
        self.assertEqual(row["reserved"], row["accounted"])
        self.assertNotIn("SECRET", row["payload"])

    def test_output_limit_failure_still_records_actual_usage(self):
        self.provider.return_value["finish_reason"] = "length"
        with self.assertRaisesRegex(ValueError, "未完整返回"):
            self.service.translate(self.data)
        self.assertEqual(self.service.status()["accounted_tokens_today"], 100)
        with self.store.read() as con:
            self.assertEqual(con.execute("SELECT state FROM selection_translations").fetchone()[0], "failed")

    def test_daily_limit_blocks_before_http_but_cache_is_available(self):
        with patch("research_assistant.translation.selection.DAILY_REQUESTS", 1):
            self.service.translate(self.data)
            with self.assertRaisesRegex(ValueError, "额度"):
                self.service.translate({**self.data, "anchor": {**self.anchor, "excerpt": "Other scientific text."}})
            self.assertTrue(self.service.translate(self.data)["cached"])
            self.assertEqual(self.provider.call_count, 1)

    def test_parallel_clicks_make_only_one_request(self):
        entered, release = threading.Event(), threading.Event()
        result = self.provider.return_value
        def slow(*args):
            entered.set()
            release.wait(5)
            return result
        self.provider.side_effect = slow
        with ThreadPoolExecutor(2) as pool:
            future = pool.submit(self.service.translate, self.data)
            self.assertTrue(entered.wait(3))
            try:
                with self.assertRaises(Conflict):
                    self.service.translate(self.data)
            finally:
                release.set()
            self.assertEqual(future.result()["state"], "succeeded")
        self.assertEqual(self.provider.call_count, 1)

    def test_interruption_preserves_reservation_without_auto_retry(self):
        self.provider.side_effect = KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.service.translate(self.data)
        restarted = SelectionTranslator(ReadingStore(self.store.root), self.provider)
        restarted.recover()
        with self.store.read() as con:
            row = con.execute("SELECT * FROM selection_translations").fetchone()
        self.assertEqual(row["state"], "interrupted")
        self.assertEqual(row["accounted"], row["reserved"])
        self.assertEqual(self.provider.call_count, 1)

    def test_model_change_invalidates_cache(self):
        self.service.translate(self.data)
        self.cfg["model"] = "another-model"
        self.assertFalse(self.service.translate(self.data)["cached"])
        self.assertEqual(self.provider.call_count, 2)

    def test_direct_sdk_payload_is_short_and_has_no_retry(self):
        response = SimpleNamespace(usage=SimpleNamespace(model_dump=lambda: {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20}),
            choices=[SimpleNamespace(message=SimpleNamespace(content="科研证据"), finish_reason="stop")])
        with patch("openai.OpenAI") as factory:
            client = factory.return_value.__enter__.return_value
            client.chat.completions.create.return_value = response
            result = invoke_model(self.cfg, "Scientific evidence.")
            self.assertEqual(factory.call_args.kwargs["max_retries"], 0)
            payload = client.chat.completions.create.call_args.kwargs
            self.assertEqual(payload["messages"], [{"role": "system", "content": PROMPT}, {"role": "user", "content": "Scientific evidence."}])
            self.assertEqual(payload["extra_body"], {"thinking": {"type": "disabled"}})
            self.assertEqual(result["usage"]["total_tokens"], 20)


if __name__ == "__main__":
    unittest.main()
