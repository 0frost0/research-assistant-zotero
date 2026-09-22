"""不依赖外部服务的核心回归测试。"""

import tempfile
import json
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from langchain_core.documents import Document
from langchain_core.messages import AIMessage
from PIL import Image, ImageDraw, ImageFont
from pypdf import PdfWriter
from starlette.testclient import TestClient

from research_assistant.retrieval.knowledge_base import LiteratureIndex
from research_assistant.core.models import ResearchContext, ResearchResponse
from research_assistant.ingestion.mineru_extractor import MinerUExtractor
from research_assistant.ingestion.pdf_extractor import PDFExtractor, normalize_pdf_text
from qdrant_client import QdrantClient
from research_assistant.retrieval.qdrant_index import PersistentTextIndex, qdrant_service_status
from research_assistant.retrieval.unified_index import UnifiedMultimodalIndex
from research_assistant.retrieval.embeddings.remote_embeddings import RemoteQwenEmbeddings, query_instruction, QUERY_INSTRUCTION, IMAGE_QUERY_INSTRUCTION
from research_assistant.agents.research_agent import ResearchAgentBundle, run_agent
from research_assistant.web.web_app import RequestValidationError, app, parse_search_options


class LiteratureIndexTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        (root / "Guideline for Heart Failure.txt").write_text(
            "Heart failure definition. HF is a complex clinical syndrome. " * 20,
            encoding="utf-8",
        )
        (root / "unrelated.txt").write_text(
            "This file discusses an unrelated software experiment. " * 20,
            encoding="utf-8",
        )
        self.index = LiteratureIndex(root)

    def tearDown(self) -> None:
        self.temp_dir.cleanup()

    def test_alias_expansion_finds_english_heart_failure_text(self) -> None:
        results = self.index.search_results("什么是心衰", min_score=0.02)
        self.assertTrue(results)
        self.assertEqual(results[0].source, "Guideline for Heart Failure.txt")

    def test_source_filter_limits_results(self) -> None:
        results = self.index.search_results(
            "experiment",
            sources=["unrelated.txt"],
            min_score=0.02,
        )
        self.assertTrue(results)
        self.assertEqual({item.source for item in results}, {"unrelated.txt"})

    def test_high_threshold_returns_no_evidence(self) -> None:
        results = self.index.search_results("量子香蕉", min_score=0.95)
        self.assertEqual(results, [])
        self.assertTrue(self.index.search("量子香蕉", min_score=0.95).startswith("NO_RELIABLE_EVIDENCE"))

    def test_failed_unified_sync_never_publishes_partial_index(self) -> None:
        with patch('research_assistant.retrieval.embeddings.remote_embeddings.RemoteQwenEmbeddings'), patch(
            'research_assistant.retrieval.unified_index.UnifiedMultimodalIndex'
        ) as constructor:
            constructor.return_value.sync.side_effect = RuntimeError("simulated write failure")
            index = LiteratureIndex(Path(self.temp_dir.name), backend="multimodal")
        self.assertIsNone(index.unified_index)
        self.assertIn("simulated write failure", index.unified_error)
        self.assertIsNone(index.embeddings)
        results = index.search_results("heart failure", min_score=0.02)
        self.assertEqual(results[0].backend, "multimodal-fallback-tfidf")

    def test_hybrid_backend_combines_lexical_and_semantic_scores(self) -> None:
        class FakeEmbeddings:
            def embed_documents(self, texts):
                return [
                    [1.0, 0.0] if "Heart failure" in text else [0.0, 1.0]
                    for text in texts
                ]

            def embed_query(self, _text):
                return [1.0, 0.0]

        fake_module = types.ModuleType('research_assistant.retrieval.embeddings.local_embeddings')
        fake_module.LocalBGEEmbeddings = FakeEmbeddings
        with patch.dict("sys.modules", {'research_assistant.retrieval.embeddings.local_embeddings': fake_module}):
            hybrid = LiteratureIndex(Path(self.temp_dir.name), backend="hybrid")
        results = hybrid.search_results("什么是心衰", min_score=0.1)
        self.assertTrue(results)
        self.assertEqual(results[0].source, "Guideline for Heart Failure.txt")

    def test_bge_rejects_query_vector_from_a_different_dimension(self) -> None:
        class MismatchedEmbeddings:
            def embed_documents(self, texts):
                return [[1.0, 0.0] for _text in texts]

            def embed_query(self, _text):
                return [1.0, 0.0, 0.0]

        fake_module = types.ModuleType('research_assistant.retrieval.embeddings.local_embeddings')
        fake_module.LocalBGEEmbeddings = MismatchedEmbeddings
        with patch.dict("sys.modules", {'research_assistant.retrieval.embeddings.local_embeddings': fake_module}):
            index = LiteratureIndex(Path(self.temp_dir.name), backend="bge")

        with self.assertRaisesRegex(ValueError, "问题向量与资料向量维度不一致"):
            index.search_results("heart failure", min_score=0.0)

    def test_index_prefers_existing_mineru_cache_for_pdf(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pdf_path = root / "paper.pdf"
            pdf_path.write_bytes(b"not-read-because-cache-is-mocked")
            cached_document = types.SimpleNamespace(
                page_content="MinerU table evidence " * 20,
                metadata={"source": "paper.pdf", "file_type": ".pdf", "page": 2},
            )
            fake_mineru = types.SimpleNamespace(
                has_cache=lambda _path: True,
                load_cached=lambda _path, source: (
                    [cached_document],
                    {
                        "source": source,
                        "parser": "mineru",
                        "total_pages": 2,
                        "native_pages": 0,
                        "ocr_pages": 0,
                        "low_text_pages": 0,
                        "empty_pages": 0,
                    },
                ),
            )
            with patch('research_assistant.retrieval.knowledge_base.MinerUExtractor', return_value=fake_mineru):
                index = LiteratureIndex(root)

        self.assertEqual(index.pdf_reports["paper.pdf"]["parser"], "mineru")
        self.assertEqual(index.chunks[0].metadata["page"], 2)


class PersistentTextIndexTests(unittest.TestCase):
    class FakeEmbeddings:
        @staticmethod
        def _vector(text: str) -> list[float]:
            lowered = text.lower()
            if "alpha" in lowered:
                return [1.0, 0.0, 0.0]
            if "beta" in lowered:
                return [0.0, 1.0, 0.0]
            return [0.0, 0.0, 1.0]

        def embed_documents(self, texts):
            return [self._vector(text) for text in texts]

        def embed_query(self, text):
            return self._vector(text)

    def test_qdrant_sync_is_incremental_and_replaces_active_version(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            index = PersistentTextIndex(
                embeddings=self.FakeEmbeddings(),
                model_name="fake/test-model",
                dimension=3,
                evidence_path=Path(temp_dir) / "evidence.sqlite3",
                collection_base="test_evidence",
                client=QdrantClient(":memory:"),
            )
            first_chunks = [
                Document(
                    page_content="alpha evidence",
                    metadata={
                        "source": "paper.txt",
                        "file_type": ".txt",
                        "page": None,
                        "start_index": 0,
                    },
                )
            ]

            first = index.sync(first_chunks)
            second = index.sync(first_chunks)
            self.assertEqual(first["indexed"], 1)
            self.assertEqual(second["skipped"], 1)
            self.assertEqual(
                index.store.stats(),
                {"documents": 1, "evidence": 1, "visual_assets": 0},
            )
            self.assertEqual(index.search("alpha", k=3, min_score=0.5)[0].source, "paper.txt")

            replacement = [
                Document(
                    page_content="beta replacement evidence",
                    metadata={
                        "source": "paper.txt",
                        "file_type": ".txt",
                        "page": None,
                        "start_index": 0,
                    },
                )
            ]
            index.sync(replacement)

            self.assertEqual(index.search("alpha", k=3, min_score=0.5), [])
            self.assertEqual(index.search("beta", k=3, min_score=0.5)[0].text, "beta replacement evidence")
            self.assertEqual(
                index.store.stats(),
                {"documents": 1, "evidence": 1, "visual_assets": 0},
            )

    def test_unified_index_stores_text_and_images_in_one_model_space(self) -> None:
        class FakeUnifiedEmbeddings:
            model_name = "fake/unified-model"
            dimension = 2

            def __init__(self):
                self.text_calls = 0
                self.image_calls = 0
                self.query_calls = 0

            def embed_texts(self, texts):
                self.text_calls += len(texts)
                return [[1.0, 0.0] for _text in texts]

            def embed_images(self, paths, captions=None):
                self.captions = captions
                self.image_calls += len(paths)
                return [[1.0, 0.0] for _path in paths]

            def embed_query(self, _text):
                self.query_calls += 1
                return [1.0, 0.0]

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            image_path = root / "figure.jpg"
            Image.new("RGB", (32, 32), "blue").save(image_path)
            embeddings = FakeUnifiedEmbeddings()
            index = UnifiedMultimodalIndex(
                embeddings=embeddings,
                evidence_path=root / "evidence.sqlite3",
                collection_base="test_unified",
                client=QdrantClient(":memory:"),
            )
            chunks = [
                Document(
                    page_content="统一空间中的文字证据",
                    metadata={
                        "source": "paper.pdf",
                        "file_type": ".pdf",
                        "page": 1,
                        "start_index": 0,
                        "content_type": "image",
                        "image_path": str(image_path),
                    },
                )
            ]

            first = index.sync(chunks)
            second = index.sync(chunks)
            with patch.object(index.client, "query_points", wraps=index.client.query_points) as search:
                hits = index.search("同一问题", k=5, min_score=0.5)
                self.assertEqual(search.call_count, 1)

            self.assertEqual(first["text_indexed"], 1)
            self.assertEqual(first["image_indexed"], 1)
            self.assertEqual(second["text_skipped"], 1)
            self.assertEqual(second["image_skipped"], 1)
            self.assertEqual(embeddings.text_calls, 1)
            self.assertEqual(embeddings.image_calls, 1)
            self.assertEqual(embeddings.query_calls, 1)
            self.assertEqual({hit.modality for hit in hits}, {"text", "image"})
            self.assertEqual(index.client.count(index.collection_name, exact=True).count, 2)
            self.assertEqual(embeddings.captions, [chunks[0].page_content])

            # Simulate an empty restored database with a NEW memory-only client.
            index.client.close()
            index.client = QdrantClient(":memory:")
            index._ensure_collection()
            repaired = index.sync(chunks)
            self.assertEqual(repaired["text_indexed"], 1)
            self.assertEqual(repaired["image_indexed"], 1)
            self.assertEqual(index.client.count(index.collection_name, exact=True).count, 2)
            self.assertEqual(index.search("hidden", k=5, min_score=0, sources=[]), [])

            duplicate = Document(page_content=chunks[0].page_content,
                                 metadata={**chunks[0].metadata, "source": "copy.pdf"})
            index.sync(chunks + [duplicate])
            self.assertEqual(len(index.store.active_image_candidates()), 2)
            copied_hits = index.search("copy", k=5, min_score=0, sources=["copy.pdf"])
            self.assertEqual({hit.modality for hit in copied_hits}, {"text", "image"})
            old_image_ids = [hit.point_id for hit in copied_hits if hit.modality == "image"]
            document_id, version, records = index._text_records("copy.pdf", [(0, Document(
                page_content="replacement", metadata={"source": "copy.pdf", "file_type": ".pdf"}))])
            index.store.stage(records)
            index.store.activate(document_id, version)
            self.assertEqual(index.store.active_visual_ids(old_image_ids), set())
            self.assertIsNone(index.store.visual_path(old_image_ids[0]))
            index.client.close()


class RemoteEmbeddingTests(unittest.TestCase):
    def test_query_task_hint_does_not_change_the_embedding_model(self):
        self.assertEqual(query_instruction("心衰是什么"), QUERY_INSTRUCTION)
        self.assertEqual(query_instruction("心衰诊断流程图"), IMAGE_QUERY_INSTRUCTION)
        self.assertEqual(query_instruction("medical flowchart figure"), IMAGE_QUERY_INSTRUCTION)

    def test_nonlocal_endpoint_is_rejected_before_network(self):
        with self.assertRaisesRegex(ValueError, "localhost"):
            RemoteQwenEmbeddings("https://example.com")

    def test_response_model_and_vector_validation(self):
        import io
        from unittest.mock import Mock
        model = RemoteQwenEmbeddings.__new__(RemoteQwenEmbeddings)
        model.url = "http://127.0.0.1:30001"
        model.dimension = 2
        model.profile = {"model_id": "test", "fingerprint": "expected", "dimension": 2}
        model.opener = Mock()
        for result, message in [
            ({**model.profile, "fingerprint": "changed", "vectors": [[1, 0]]}, "model changed"),
            ({**model.profile, "vectors": []}, "count mismatch"),
            ({**model.profile, "vectors": [[1, 0, 0]]}, "Invalid embedding"),
            ({**model.profile, "vectors": [[0, 0]]}, "not normalized"),
            ({**model.profile, "vectors": [[float("nan"), 0]]}, "Invalid embedding"),
        ]:
            with self.subTest(message=message):
                model.opener.open.return_value = io.BytesIO(json.dumps(result).encode())
                with self.assertRaisesRegex(ValueError, message):
                    model.embed_query("test")
        model.opener.open.return_value = io.BytesIO(json.dumps({**model.profile, "vectors": [[1, 0]]}).encode())
        self.assertEqual(model.embed_query("test"), [1, 0])

class PDFExtractionTests(unittest.TestCase):
    def test_normalize_pdf_text_repairs_line_break_hyphenation(self) -> None:
        text = normalize_pdf_text("heart fail-\nure\n\n\nnext paragraph")
        self.assertEqual(text, "heart failure\n\nnext paragraph")

    def test_blank_pdf_is_reported_without_creating_a_document(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "blank.pdf"
            writer = PdfWriter()
            writer.add_blank_page(width=300, height=300)
            with path.open("wb") as output:
                writer.write(output)

            documents, report = PDFExtractor(enable_ocr=False).extract(path)

        self.assertEqual(documents, [])
        self.assertEqual(report.total_pages, 1)
        self.assertEqual(report.empty_pages, 1)
        self.assertEqual(report.pages[0].status, "ocr_unavailable")

    def test_image_only_pdf_uses_ocr(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "scan.pdf"
            image = Image.new("RGB", (1400, 360), "white")
            draw = ImageDraw.Draw(image)
            font_path = next((p for p in (Path("C:/Windows/Fonts/arial.ttf"),Path("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf")) if p.exists()),Path("missing-font"))
            font = (
                ImageFont.truetype(str(font_path), 64)
                if font_path.exists()
                else ImageFont.load_default(size=64)
            )
            draw.text((70, 120), "SCANNED TEST ALPHA", fill="black", font=font)
            image.save(path, "PDF", resolution=150)

            documents, report = PDFExtractor(
                enable_ocr=True,
                ocr_language="eng",
                min_native_chars=20,
            ).extract(path)

        self.assertEqual(report.ocr_pages, 1)
        self.assertEqual(report.pages[0].method, "ocr")
        self.assertIn("SCANNED", documents[0].page_content.upper())


class MinerUCacheTests(unittest.TestCase):
    def test_unreadable_cache_is_treated_as_a_cache_miss(self) -> None:
        extractor = MinerUExtractor()
        with patch.object(extractor, "cache_path", side_effect=PermissionError("denied")):
            self.assertFalse(extractor.has_cache(Path("paper.pdf")))

    @patch('research_assistant.ingestion.mineru_extractor.urllib.request.urlopen')
    def test_server_status_reports_unavailable_without_starting_parse(self, urlopen) -> None:
        urlopen.side_effect = OSError("connection refused")
        extractor = MinerUExtractor()

        healthy, detail = extractor.server_status(timeout_seconds=0.1)

        self.assertFalse(healthy)
        self.assertIn("connection refused", detail)

    def test_cached_content_list_becomes_page_aware_documents(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            pdf_path = root / "paper.pdf"
            pdf_path.write_bytes(b"synthetic-pdf-for-cache-test")
            extractor = MinerUExtractor(
                cache_dir=root / "cache",
                python_executable=root / "python.exe",
            )
            cache_path = extractor.cache_path(pdf_path)
            parse_path = cache_path / "paper" / "vlm"
            parse_path.mkdir(parents=True)
            (cache_path / "manifest.json").write_text(
                json.dumps({"parsed_at": "2026-09-04T10:00:00"}),
                encoding="utf-8",
            )
            (parse_path / "paper_content_list.json").write_text(
                json.dumps(
                    [
                        {"type": "text", "text": "正文证据", "page_idx": 0},
                        {
                            "type": "table",
                            "table_caption": ["表 1"],
                            "table_body": "<table><tr><td>42</td></tr></table>",
                            "page_idx": 2,
                            "bbox": [10, 20, 900, 800],
                        },
                    ],
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            documents, report = extractor.load_cached(pdf_path, source="paper.pdf")

        self.assertEqual([item.metadata["page"] for item in documents], [1, 3])
        self.assertEqual(documents[1].metadata["content_type"], "table")
        self.assertIn("42", documents[1].page_content)
        self.assertEqual(report["parser"], "mineru")
        self.assertEqual(report["block_counts"]["table"], 1)


class FakeResponder:
    def __init__(self, response=None):
        self.response = response
        self.calls = 0

    def invoke(self, *_args, **_kwargs):
        self.calls += 1
        return self.response


class FakePlainModel:
    def invoke(self, *_args, **_kwargs):
        return AIMessage(content="降级文本结果")


class AgentRecoveryTests(unittest.TestCase):
    def test_missing_structured_output_falls_back_to_plain_text(self) -> None:
        bundle = ResearchAgentBundle(
            retrieve=lambda _query: "来源：测试资料，第 1 页。证据内容。",
            responder=FakeResponder(),
            model=FakePlainModel(),
            search_history=[],
        )
        response = run_agent(
            bundle,
            "测试问题",
            ResearchContext(research_topic="测试", weekly_hours=1),
        )
        self.assertEqual(response.status, "degraded")
        self.assertEqual(response.summary, "降级文本结果")
        self.assertIn("结构化输出解析失败", response.warning)

    def test_retrieval_and_structured_generation_each_run_once(self) -> None:
        retrieval_calls = []

        def retrieve(query):
            retrieval_calls.append(query)
            return "来源：测试资料，第 1 页。证据内容。"

        expected = ResearchResponse(
            mode="answer",
            summary="结构化答案",
            citations=[],
            tasks=[],
        )
        responder = FakeResponder(expected)
        bundle = ResearchAgentBundle(
            retrieve=retrieve,
            responder=responder,
            model=FakePlainModel(),
            search_history=[],
        )
        response = run_agent(
            bundle,
            "测试问题",
            ResearchContext(research_topic="测试", weekly_hours=1),
        )
        self.assertEqual(response.summary, "结构化答案")
        self.assertEqual(retrieval_calls, ["测试问题"])
        self.assertEqual(responder.calls, 1)


class ApiValidationTests(unittest.TestCase):
    def test_status_preserves_public_evidence_store_field(self) -> None:
        # API 字段名属于前端契约，不能随着 Python 模块搬迁而更名。
        with tempfile.TemporaryDirectory() as directory, patch(
            "research_assistant.retrieval.qdrant_index.QdrantClient"
        ) as client:
            client.return_value.get_collections.return_value.collections = []
            status = qdrant_service_status(Path(directory) / "evidence.sqlite3")
        self.assertTrue(status["available"])
        self.assertEqual(status["evidence_store"], {"documents": 0, "evidence": 0, "visual_assets": 0})

    def test_local_health_does_not_probe_unavailable_dependencies(self) -> None:
        # 隧道掉线不应导致启动脚本误杀仍可访问的本地网页。
        with patch("research_assistant.retrieval.embeddings.remote_embeddings.embedding_service_status",
                   side_effect=AssertionError("Health must not call Qwen")), patch(
            "research_assistant.retrieval.qdrant_index.qdrant_service_status",
            side_effect=AssertionError("Health must not call Qdrant")), patch(
            "research_assistant.web.web_app.MinerUExtractor",
            side_effect=AssertionError("Health must not call MinerU")):
            response = TestClient(app).get("/api/health")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"ok": True, "service": "research_assistant"})

    def test_invalid_backend_is_client_error(self) -> None:
        with self.assertRaises(RequestValidationError):
            parse_search_options({"backend": "invalid"})

    def test_empty_source_selection_is_rejected(self) -> None:
        with self.assertRaises(RequestValidationError):
            parse_search_options({"sources": []})

    def test_mineru_unavailable_is_reported_before_job_creation(self) -> None:
        fake_mineru = types.SimpleNamespace(
            configured=True,
            server_status=lambda: (False, "connection refused"),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            pdf_path = Path(temp_dir) / "paper.pdf"
            pdf_path.write_bytes(b"test")
            with patch('research_assistant.web.web_app.safe_library_path', return_value=pdf_path), patch(
                'research_assistant.web.web_app.MinerUExtractor', return_value=fake_mineru
            ):
                response = TestClient(app).post(
                    "/api/pdf-parse", json={"source": "paper.pdf"}
                )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["error_code"], "mineru_unavailable")
        self.assertIn("start_mineru_tunnel.ps1", response.json()["error"])

    @patch(
        'research_assistant.web.web_app.execute_research_request',
        side_effect=RuntimeError("Error code: 402 - Insufficient Balance"),
    )
    def test_deepseek_balance_error_is_classified(self, _execute) -> None:
        response = TestClient(app).post(
            "/api/run",
            json={"question": "离线测试", "allow_external": True},
        )
        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["error_code"], "external_model_balance")
        self.assertIn("余额不足", response.json()["error"])


if __name__ == "__main__":
    unittest.main()
