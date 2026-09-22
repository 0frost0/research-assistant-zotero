"""Deterministic reading lifecycle tests; no model or vector server required."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import sqlite3
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch, Mock
from pypdf import PdfWriter
from pypdf.generic import (
    DictionaryObject,
    NameObject,
    DecodedStreamObject,
    NumberObject,
    RectangleObject,
)
from starlette.applications import Starlette
from starlette.testclient import TestClient
from research_assistant.memory.store import ReadingStore, Conflict, inspect_pdf
from research_assistant.memory.qa import answer, draft, ReadingAnswer
from research_assistant.translation.service import (
    TranslationService,
    profile,
    validate_output,
    protocol_event,
    retained_english_prose,
    safe_worker_metrics,
)
from research_assistant.web.reading_api import ReadingAPI


def pdf(path, *, rotate=0, crop=False):
    writer = PdfWriter()
    page = writer.add_blank_page(width=600, height=800)
    font = DictionaryObject(
        {
            NameObject("/Type"): NameObject("/Font"),
            NameObject("/Subtype"): NameObject("/Type1"),
            NameObject("/BaseFont"): NameObject("/Helvetica"),
        }
    )
    page[NameObject("/Resources")] = DictionaryObject(
        {
            NameObject("/Font"): DictionaryObject(
                {NameObject("/F1"): writer._add_object(font)}
            )
        }
    )
    stream = DecodedStreamObject()
    stream.set_data(
        b"BT /F1 12 Tf 60 700 Td (Scientific evidence from original paper. Retrieval needs careful validation. Personal notes are not research conclusions. n = 200.) Tj ET"
    )
    page[NameObject("/Contents")] = writer._add_object(stream)
    if rotate:
        page.rotate(rotate)
    if crop:
        page.cropbox = RectangleObject([30, 40, 570, 760])
        page[NameObject("/UserUnit")] = NumberObject(2)
    with path.open("wb") as out:
        writer.write(out)


class ReadingMemoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        (self.root / "library").mkdir()
        self.pdf = self.root / "library/paper.pdf"
        pdf(self.pdf)
        self.store = ReadingStore(self.root)
        self.paper = self.store.register(self.pdf, "paper.pdf")
        self.a = self.store.artifact(self.paper["original_id"])
        self.anchor = {
            "artifact_id": self.a["id"],
            "artifact_hash": self.a["hash"],
            "page": 1,
            "coordinate_system": "pdf_user_space",
            "view_box": [0.0, 0.0, 600.0, 800.0],
            "rects": [[60, 680, 210, 705], [60, 660, 230, 678]],
            "excerpt": "Scientific evidence",
        }

    def tearDown(self):
        self.temp.cleanup()

    def note(self, **kw):
        return self.store.save_note(
            {
                "paper_id": self.paper["id"],
                "body": "我的理解：检索质量影响证据可靠性。疑问是小样本能否推广。",
                "anchors": [self.anchor],
                **kw,
            }
        )

    def test_revision_history_restart_and_archive(self):
        n = self.note()
        n2 = self.store.save_note(
            {"body": "修正理解：检索结果必须核对原文", "expected_revision": 1}, n["id"]
        )
        self.assertEqual(n2["previous_revision"], 1)
        self.assertEqual(len(ReadingStore(self.root).history(n["id"])), 2)
        self.assertEqual(self.store.search("核对原文")[0]["revision"], 2)
        self.store.save_note({"expected_revision": 2, "archived": True}, n["id"])
        self.assertEqual(self.store.search("核对原文"), [])
        self.assertEqual(self.store.get(n["id"], 1)["body"], n["body"])

    def test_concurrent_compare_and_swap(self):
        n = self.note()

        def edit(i):
            try:
                return self.store.save_note(
                    {"expected_revision": 1, "body": "并发编辑" + str(i)}, n["id"]
                )["revision"]
            except Conflict:
                return "conflict"

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(edit, [1, 2]))
        self.assertCountEqual(results, [2, "conflict"])
        self.assertEqual(len(self.store.history(n["id"])), 2)

    def test_revision_cannot_be_overwritten(self):
        self.note()
        with self.assertRaises(sqlite3.IntegrityError), self.store.transaction() as con:
            con.execute("UPDATE note_revisions SET payload='{}'")

    def test_transaction_rolls_back_pointer_if_revision_insert_fails(self):
        n = self.note()
        with self.store.transaction() as con:
            con.execute(
                "CREATE TRIGGER fail_revision BEFORE INSERT ON note_revisions BEGIN SELECT RAISE(ABORT,'test'); END"
            )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.save_note({"expected_revision": 1, "body": "新内容"}, n["id"])
        self.assertEqual(self.store.get(n["id"])["revision"], 1)

    def test_ai_draft_requires_explicit_confirmation(self):
        n = self.store.save_note(
            {
                "paper_id": self.paper["id"],
                "body": "检索质量的草稿",
                "anchors": [self.anchor],
            },
            ai_draft=True,
        )
        self.assertEqual(self.store.search("检索质量"), [])
        self.assertEqual(n["source_kind"], "ai_draft")
        n = self.store.save_note(
            {"expected_revision": 1, "body": "修订后的检索质量草稿"}, n["id"]
        )
        self.assertFalse(n["confirmed"])
        n = self.store.save_note({"expected_revision": 2, "confirm": True}, n["id"])
        self.assertEqual(n["author_source"], "ai_assisted")
        self.assertEqual(self.store.search("检索质量")[0]["revision"], 3)

    def test_cache_failure_cannot_lose_or_resurrect_notes(self):
        n = self.note()
        self.store.search("检索质量")
        with patch.object(
            self.store,
            "_invalidate_cache",
            side_effect=RuntimeError("cache unavailable"),
        ):
            self.store.save_note(
                {"expected_revision": 1, "body": "不同内容：公式单位需核实"}, n["id"]
            )
        self.assertEqual(self.store.search("检索质量"), [])
        self.store._cache = object()
        self.assertEqual(self.store.search("公式单位")[0]["revision"], 2)
        self.assertEqual(len(self.store.notes(self.paper["id"])), 1)

    def test_translation_version_preserves_old_annotations(self):
        t1 = self.store.add_translation(
            self.paper["id"],
            self.a["id"],
            self.pdf,
            {"test": "synthetic"},
            {"test": True},
        )
        anchor = {
            **self.anchor,
            "artifact_id": t1["id"],
            "artifact_hash": t1["hash"],
            "mapping_status": "exact",
        }
        line = self.store.create_annotation(
            {"paper_id": self.paper["id"], "anchor": anchor}
        )
        n = self.note(anchors=[anchor])
        t2 = self.store.add_translation(
            self.paper["id"], self.a["id"], self.pdf, {}, {}
        )
        self.assertNotEqual(t1["id"], t2["id"])
        self.assertEqual(self.store.get(n["id"])["anchors"][0]["artifact_id"], t1["id"])
        self.assertEqual(line["anchor"]["mapping_status"], "unaligned")
        self.assertTrue(self.store.artifact_path(t1["id"]).exists())

    def test_manual_translation_link_revision_and_restart(self):
        translated = self.store.add_translation(self.paper["id"], self.a["id"], self.pdf, {}, {})
        ta = {**self.anchor, "artifact_id": translated["id"], "artifact_hash": translated["hash"], "excerpt": "机器翻译摘录"}
        n = self.note(anchors=[ta])
        linked = self.store.save_note({"expected_revision": 1, "anchors": [ta, self.anchor],
            "source_links": [{"translation_index": 0, "original_index": 1, "confirm": True}]}, n["id"])
        self.assertEqual(linked["source_links"][0]["status"], "user_confirmed")
        self.assertEqual(linked["anchors"][0]["mapping_status"], "unaligned")
        self.assertEqual(self.store.history(n["id"])[-1]["source_links"], [])
        timestamp = linked["source_links"][0]["confirmed_at"]
        edited = self.store.save_note({"expected_revision": 2, "body": "更新我的理解"}, n["id"])
        self.assertEqual(edited["source_links"][0]["confirmed_at"], timestamp)
        self.store.add_translation(self.paper["id"], self.a["id"], self.pdf, {}, {})
        reopened = ReadingStore(self.root).get(n["id"])
        self.assertEqual(reopened["anchors"][0]["artifact_id"], translated["id"])
        self.assertEqual(reopened["source_links"], edited["source_links"])
        self.assertEqual(answer(self.store, {"question": "更新我的理解", "scope": "notes"})["note_citations"][0]["source_links"], edited["source_links"])

    def test_manual_link_requires_confirmation_and_real_original_selection(self):
        translated = self.store.add_translation(self.paper["id"], self.a["id"], self.pdf, {}, {})
        ta = {**self.anchor, "artifact_id": translated["id"], "artifact_hash": translated["hash"]}
        link = {"translation_index": 0, "original_index": 1}
        for anchors, links in [([ta, self.anchor], [link]),
                               ([ta, {**self.anchor, "rects": []}], [{**link, "confirm": True}]),
                               ([self.anchor, ta], [{**link, "confirm": True}]),
                               ([ta, {**self.anchor, "artifact_hash": "wrong"}], [{**link, "confirm": True}])]:
            with self.assertRaises(ValueError): self.note(anchors=anchors, source_links=links)
        with self.assertRaises(ValueError):
            self.store.save_note({"paper_id": self.paper["id"], "body": "AI cannot confirm",
                "anchors": [ta, self.anchor], "source_links": [{**link, "confirm": True}]}, ai_draft=True)
        other = self.root / "library/other.pdf"
        pdf(other, rotate=90)
        other_paper = self.store.register(other, "other.pdf")
        other_a = self.store.artifact(other_paper["original_id"])
        with self.assertRaises(ValueError): self.note(anchors=[ta, {**self.anchor, "artifact_id": other_a["id"], "artifact_hash": other_a["hash"]}], source_links=[{**link, "confirm": True}])

    def test_link_conflict_preserves_old_pair_and_relink_needs_confirmation(self):
        translated = self.store.add_translation(self.paper["id"], self.a["id"], self.pdf, {}, {})
        ta = {**self.anchor, "artifact_id": translated["id"], "artifact_hash": translated["hash"]}
        n = self.note(anchors=[ta, self.anchor], source_links=[{"translation_index": 0, "original_index": 1, "confirm": True}])
        new_anchor = {**self.anchor, "excerpt": "different original selection"}
        with self.assertRaises(ValueError): self.store.save_note({"expected_revision": 1, "anchors": [ta, new_anchor]}, n["id"])
        self.store.save_note({"expected_revision": 1, "body": "new body"}, n["id"])
        with self.assertRaises(Conflict): self.store.save_note({"expected_revision": 1, "source_links": []}, n["id"])
        self.assertEqual(self.store.get(n["id"])["source_links"], n["source_links"])

    def test_original_hash_and_geometry_enforced(self):
        for changes in (
            {"artifact_hash": "wrong"},
            {"page": 0},
            {"coordinate_system": "screen"},
            {"rects": [[0, 0, 900, 900]]},
            {"view_box": [0, 0, 1, 1]},
            {"rects": [[0, 0, float("nan"), 20]]},
        ):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.store.validate_anchor({**self.anchor, **changes}, self.paper["id"])
        p = self.root / "rotated.pdf"
        pdf(p, rotate=90, crop=True)
        paper = self.store.register(p, "rotated.pdf")
        a = self.store.artifact(paper["original_id"])
        geometry = a["metadata"]["pages"][0]
        self.assertEqual(geometry["view_box"], [30.0, 40.0, 570.0, 760.0])
        self.assertEqual(geometry["rotation"], 90)
        self.assertEqual(geometry["user_unit"], 2.0)

    def test_highlight_without_note_and_page_only_card(self):
        self.store.create_annotation(
            {"paper_id": self.paper["id"], "anchor": self.anchor, "style": "underline"}
        )
        self.assertEqual(len(ReadingStore(self.root).annotations(self.paper["id"])), 1)
        self.assertEqual(self.store.notes(), [])
        n = self.note(
            anchors=[{**self.anchor, "rects": [], "evidence_id": "external-reference"}]
        )
        self.assertEqual(n["anchors"][0]["mapping_status"], "page_only")

    def test_original_retained_after_library_removal(self):
        self.pdf.unlink()
        self.assertTrue(self.store.artifact_path(self.a["id"]).is_file())

    def test_offline_manual_and_qa(self):
        n = self.note()

        def offline():
            raise ConnectionError("model offline")

        result = answer(
            self.store,
            {
                "question": "检索质量",
                "paper_id": self.paper["id"],
                "allow_external": True,
            },
            model_factory=offline,
        )
        self.assertEqual(result["status"], "retrieval_only")
        self.assertEqual(result["note_citations"][0]["id"], n["id"])
        self.assertEqual(result["original_citations"], [])

    def test_answer_during_update_discards_old_revision(self):
        n = self.note()
        store = self.store

        class Fake:
            def with_structured_output(self, *a, **kw):
                return self

            def invoke(self, *a):
                store.save_note({"expected_revision": 1, "body": "已经修正"}, n["id"])
                return ReadingAnswer(
                    original_answer="", notes_answer="旧内容", note_refs=[0]
                )

        with self.assertRaises(Conflict):
            answer(
                store,
                {"question": "检索质量", "allow_external": True},
                model_factory=Fake,
            )

    def test_paper_facts_do_not_retrieve_notes(self):
        self.note()
        result = answer(
            self.store,
            {"question": "检索质量", "scope": "original"},
            get_index=lambda _: (_ for _ in ()).throw(ConnectionError()),
        )
        self.assertEqual(result["note_citations"], [])
        self.assertEqual(result["original_citations"], [])

    def test_api_conflict_and_ai_spoofing(self):
        api = ReadingAPI(self.root)
        client = TestClient(Starlette(routes=api.routes()))
        n = client.post(
            "/api/reading/notes",
            json={
                "paper_id": self.paper["id"],
                "body": "记录",
                "anchors": [self.anchor],
                "confirmed": False,
                "author_source": "ai_assisted",
            },
        ).json()
        self.assertEqual(n["author_source"], "user")
        r = client.post(
            "/api/reading/notes/" + n["id"],
            json={"body": "未保存文本", "expected_revision": 0},
        )
        self.assertEqual(r.status_code, 409)
        self.assertEqual(
            client.post(
                "/api/reading/settings",
                json={"auto_translate": True},
                headers={"Origin": "https://unrelated.invalid"},
            ).status_code,
            403,
        )

    def test_translation_dedup_retry_and_config_identity(self):
        cfg = {
            "configured": True,
            "model": "test",
            "base_url": "https://example.invalid/v1",
            "key": "never-persist",
        }
        service = TranslationService(self.store)
        with patch(
            "research_assistant.translation.service.configuration", return_value=cfg
        ):
            j, created = service.submit(self.paper["id"])
            j2, created2 = service.submit(self.paper["id"])
            self.assertTrue(created)
            self.assertFalse(created2)
            self.assertEqual(j["id"], j2["id"])
            self.assertNotIn("never-persist", str(j))
            self.store.update_job(j["id"], state="failed")
            retry = service.retry(j["id"])
            self.assertEqual(retry["attempt"], 2)
            newer, _ = service.submit(self.paper["id"], force=True)
            self.assertNotEqual(newer["id"], j["id"])
            self.store.update_job(j["id"], state="failed")
            with patch("research_assistant.translation.service.configuration", return_value={**cfg, "model": "changed"}):
                migrated = service.retry(j["id"])
                self.assertNotEqual(migrated["id"], j["id"])
                self.assertEqual(self.store.job(j["id"])["profile"]["model"], "test")
                self.assertEqual(migrated["profile"]["model"], "changed")
                with self.assertRaisesRegex(ValueError, "仅失败任务"):
                    service.retry(migrated["id"])
        self.assertNotEqual(profile(cfg), profile({**cfg, "model": "changed"}))

    def test_restart_marks_interrupted_jobs_failed(self):
        a, _ = self.store.reserve_job(self.paper["id"], {"test": 1})
        b, _ = self.store.reserve_job(self.paper["id"], {"test": 2})
        self.store.update_job(b["id"], state="running")
        self.store.interrupt_jobs()
        self.assertTrue(all(j["state"] == "failed" for j in self.store.jobs()))

    def test_blank_unchanged_or_partial_output_rejected(self):
        with self.assertRaises(ValueError):
            validate_output(self.pdf, self.pdf)
        writer = PdfWriter()
        writer.add_blank_page(width=600, height=800)
        bad = self.root / "blank.pdf"
        with bad.open("wb") as f:
            writer.write(f)
        with self.assertRaises(ValueError):
            validate_output(self.pdf, bad)

    def test_protocol_ignores_numeric_shutdown_logs(self):
        for line in (b"8", b"null", b"[]", b"log output", b'{"unrelated":true}'):
            self.assertIsNone(protocol_event(line))
        self.assertEqual(
            protocol_event(b'{"type":"finish","path":"a.pdf"}')["path"], "a.pdf"
        )

    def test_translation_metrics_are_numeric_and_allowlisted(self):
        metrics = safe_worker_metrics({"diagnostics": {"paragraph_errors": 2, "secret": "private"},
            "seconds": float("nan"), "token_usage": {"main": {"total": 4, "prompt": "private", "secret": "private"}, "secret": {"total": 3}}})
        self.assertEqual(metrics, {"diagnostics": {"paragraph_errors": 2}, "token_usage": {"main": {"total": 4}}})

    def test_runtime_policy_changes_cache_identity_without_provider_redirect(self):
        from research_assistant.translation.policy import runtime_policy, error_code
        self.assertEqual(runtime_policy("deepseek-v4-flash", "https://api.deepseek.com")["thinking"], "disabled")
        self.assertEqual(runtime_policy("deepseek-v4-flash", "https://example.invalid")["thinking"], "provider_default")
        self.assertEqual(error_code(402), "insufficient_balance")
        self.assertEqual(runtime_policy("test", "https://example.invalid")["request_attempts"], 3)

    def test_partial_english_abstract_is_rejected_even_with_chinese_body(self):
        prose = (
            "We evaluate the methods in this study and compare the evidence of the results. "
            "The experiments are designed to test whether the method is reliable for this task. "
        ) * 4
        meta = {"page_count": 1, "pages": [{"view_box": [0, 0, 600, 800],
                "rotation": 0, "user_unit": 1, "text_chars": 1000, "latin": 500, "cjk": 100}]}
        before, after = Mock(), Mock()
        before.extract_text.return_value = prose
        after.extract_text.return_value = "摘要\n" + prose + "\n这里是已经翻译的中文正文。" * 20
        with patch("research_assistant.translation.service.inspect_pdf", return_value=meta), patch(
            "research_assistant.translation.service.PdfReader",
            side_effect=[Mock(pages=[before]), Mock(pages=[after])],
        ):
            with self.assertRaisesRegex(ValueError, "长段未翻译英文"):
                validate_output(self.pdf, self.pdf)

    def test_short_labels_and_separated_abbreviations_are_not_long_prose(self):
        text = "We use the model and data in this study to test a result."
        self.assertFalse(retained_english_prose(text, "研究结果 " + text))
        self.assertFalse(retained_english_prose(text * 10, (text + "中文说明") * 10))
        self.assertFalse(retained_english_prose("Alpha Beta Gamma " * 40, "Alpha Beta Gamma " * 40))

    def test_wrapped_english_prose_still_detected(self):
        source = ("We evaluate the methods in this study and compare the evidence of the results. " * 8)
        target = "摘要 " + source.replace("evaluate", "eval-\nuate").replace("methods", "meth-\nods")
        self.assertTrue(retained_english_prose(source, target))

    def test_same_filename_replacement_does_not_substitute_original(self):
        self.note()
        pdf(self.pdf, rotate=90)
        with patch("research_assistant.memory.qa.current_notes"):
            getter = Mock(side_effect=AssertionError("must not search replacement PDF"))
            result = answer(
                self.store,
                {
                    "scope": "original",
                    "paper_id": self.paper["id"],
                    "question": "evidence",
                },
                get_index=getter,
            )
            getter.assert_not_called()
        self.assertEqual(result["original_citations"], [])

    def test_notes_require_at_least_one_source(self):
        with self.assertRaises(ValueError):
            self.note(anchors=[])

    def test_auto_upload_switch_and_language_routing(self):
        api = ReadingAPI(self.root)
        with (
            patch.object(api.translator, "status", return_value={"configured": True}),
            patch.object(api.translator, "submit") as submit,
        ):
            asyncio.run(api.uploaded(self.pdf, "paper.pdf"))
            submit.assert_not_called()
            api.store.set_auto(True)
            asyncio.run(api.uploaded(self.pdf, "paper.pdf"))
            submit.assert_called_once()
            submit.reset_mock()
            api.store.set_auto(False)
            asyncio.run(api.uploaded(self.pdf, "paper.pdf"))
            submit.assert_not_called()
        cfg = {"configured": True, "model": "test", "base_url": "test"}
        with patch(
            "research_assistant.translation.service.configuration", return_value=cfg
        ):
            for language, text_layer in (
                ("zh", True),
                ("en", False),
                ("unknown", True),
            ):
                meta = {
                    **self.a["metadata"],
                    "language": language,
                    "text_layer": text_layer,
                    "warning": "low text",
                }
                with patch.object(
                    api.store, "artifact", return_value={**self.a, "metadata": meta}
                ):
                    with self.assertRaises(ValueError):
                        api.translator.submit(self.paper["id"])

    def test_provider_error_does_not_echo_request_or_key(self):
        factory = Mock(side_effect=ValueError("sensitive-test-marker"))
        with self.assertRaises(RuntimeError) as caught:
            draft(
                self.store,
                {
                    "paper_id": self.paper["id"],
                    "anchors": [self.anchor],
                    "allow_external": True,
                },
                model_factory=factory,
            )
        self.assertNotIn("sensitive-test-marker", str(caught.exception))

    def test_timeout_and_worker_failure_are_failed_not_success(self):
        async def scenario(timeout):
            store = self.store
            service = TranslationService(store)
            j, _ = store.reserve_job(self.paper["id"], {"test": str(timeout)})

            async def fail(job):
                if timeout:
                    await asyncio.sleep(2)
                else:
                    raise ValueError("simulated failure")

            with (
                patch.object(service, "execute", side_effect=fail),
                patch.dict("os.environ", {"RESEARCH_TRANSLATION_TIMEOUT": ".02"}),
            ):
                task = asyncio.create_task(service.run())
                for _ in range(100):
                    await asyncio.sleep(0.01)
                    if store.job(j["id"])["state"] == "failed":
                        break
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            self.assertEqual(store.job(j["id"])["state"], "failed")
            self.assertIsNone(store.job(j["id"])["artifact_id"])

        asyncio.run(scenario(True))
        asyncio.run(scenario(False))


if __name__ == "__main__":
    unittest.main()
