"""本地科研文献库：加载文档、切分、向量化和检索。"""

import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

import numpy as np
from langchain_core.documents import Document
from sklearn.feature_extraction.text import TfidfVectorizer

from research_assistant.ingestion.pdf_extractor import PDFExtractor
from research_assistant.ingestion.mineru_extractor import MinerUExtractionError, MinerUExtractor
from research_assistant.retrieval.retrieval_policy import CANDIDATE_K, EvidenceContext, select_evidence


SUPPORTED_SUFFIXES = {".pdf", ".md", ".txt"}
SUPPORTED_BACKENDS = {"tfidf", "bge", "hybrid", "qdrant", "multimodal"}
DEFAULT_THRESHOLDS = {
    "tfidf": 0.10,
    "bge": 0.35,
    "hybrid": 0.18,
    "qdrant": 0.35,
    "multimodal": 0.35,
}
QUERY_ALIASES = {
    "心衰": "heart failure hf",
    "心力衰竭": "heart failure hf",
    "痴呆": "dementia",
    "阿尔茨海默": "alzheimer dementia",
}


@dataclass(frozen=True)
class SearchResult:
    """一条可直接展示、评测或交给 Agent 的检索证据。"""

    source: str
    page: int | None
    quote: str
    score: float
    backend: str
    chunk_id: int
    evidence_id: str | None = None
    content_type: str = "text"
    bbox: object | None = None
    image_path: str | None = None
    modality: str = "text"
    text_score: float | None = None
    visual_score: float | None = None
    retrieved_text: str | None = None
    context_span: dict | None = None
    quality_warnings: list[str] = field(default_factory=list)
    retrieval_policy: str | None = None
    context_evidence_ids: list[str] = field(default_factory=list)

    def model_dump(self) -> dict:
        return asdict(self)


def split_documents(
    documents: list[Document],
    chunk_size: int = 450,
    chunk_overlap: int = 80,
) -> list[Document]:
    """按固定窗口切分并保留重叠，避免上下文在块边界完全断开。"""
    chunks: list[Document] = []
    step = chunk_size - chunk_overlap
    for document in documents:
        text = document.page_content
        for start in range(0, len(text), step):
            content = text[start : start + chunk_size]
            if not content.strip():
                continue
            metadata = dict(document.metadata)
            metadata["start_index"] = start
            chunks.append(Document(page_content=content, metadata=metadata))
    return chunks


class LiteratureIndex:
    """把文献处理细节封装起来，Agent 只需要调用 search。"""

    def __init__(self, library_dir: Path, backend: str = "tfidf", *, collection_base=None):
        self.library_dir = library_dir.resolve()
        self.backend = backend
        if backend not in SUPPORTED_BACKENDS:
            raise ValueError(f"不支持的检索后端：{backend}")

        self.pdf_reports: dict[str, dict] = {}
        self.mineru = MinerUExtractor()
        documents = self._load_documents()
        if not documents:
            raise ValueError(
                f"文献目录中没有 PDF、Markdown 或 TXT 文件：{self.library_dir}"
            )

        self.chunks = split_documents(documents)
        self.evidence_context = EvidenceContext(documents, self.chunks)
        self.vectorizer = None
        self.chunk_matrix = None
        self.embeddings = None
        self.embedding_matrix = None
        self.persistent_index = None
        self.persistent_error: str | None = None
        self.unified_index = None
        self.unified_error: str | None = None

        if backend in {"tfidf", "hybrid"}:
            # 字符 n-gram 对中英文和技术缩写都适用，不依赖在线模型。
            self.vectorizer = TfidfVectorizer(
                analyzer="char",
                ngram_range=(2, 4),
                sublinear_tf=True,
            )
            self.chunk_matrix = self.vectorizer.fit_transform(
                [chunk.page_content for chunk in self.chunks]
            )
        if backend in {"bge", "hybrid", "qdrant"}:
            # BGE 依赖较重，因此只在用户明确选择时才加载。
            from research_assistant.retrieval.embeddings.local_embeddings import LocalBGEEmbeddings

            self.embeddings = LocalBGEEmbeddings()
            if backend == "qdrant":
                try:
                    from research_assistant.retrieval.qdrant_index import PersistentTextIndex

                    persistent_index = PersistentTextIndex(
                        embeddings=self.embeddings,
                        model_name=self.embeddings.model_name,
                        dimension=self.embeddings.dimension,
                        evidence_path=self.library_dir.parent
                        / ".data"
                        / "evidence.sqlite3",
                    )
                    persistent_index.sync(self.chunks)
                    self.persistent_index = persistent_index
                except Exception as exc:
                    # 数据库不可用时仍可回答，网页状态会明确显示已经降级。
                    self.persistent_error = f"{type(exc).__name__}: {exc}"

            if backend != "qdrant" or self.persistent_index is None:
                vectors = self.embeddings.embed_documents(
                    [chunk.page_content for chunk in self.chunks]
                )
                self.embedding_matrix = np.asarray(vectors, dtype=np.float32)
                if (
                    self.embedding_matrix.ndim != 2
                    or not self.embedding_matrix.shape[1]
                ):
                    raise ValueError("BGE 没有生成有效的二维文档向量矩阵")

        if backend == "multimodal":
            try:
                from research_assistant.retrieval.embeddings.remote_embeddings import RemoteQwenEmbeddings
                from research_assistant.retrieval.unified_index import UnifiedMultimodalIndex

                unified_embeddings = RemoteQwenEmbeddings()
                unified_index = UnifiedMultimodalIndex(
                    embeddings=unified_embeddings,
                    collection_base=collection_base,
                    evidence_path=self.library_dir.parent
                    / ".data"
                    / "evidence.sqlite3",
                )
                unified_index.sync(self.chunks)
                self.unified_index = unified_index
            except Exception as exc:
                self.unified_error = f"{type(exc).__name__}: {exc}"
                self.vectorizer = TfidfVectorizer(
                    analyzer="char",
                    ngram_range=(2, 4),
                    sublinear_tf=True,
                )
                self.chunk_matrix = self.vectorizer.fit_transform(
                    [chunk.page_content for chunk in self.chunks]
                )

    def _load_documents(self) -> list[Document]:
        documents: list[Document] = []
        pdf_extractor = PDFExtractor()
        for path in sorted(self.library_dir.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in SUPPORTED_SUFFIXES:
                continue

            source = str(path.relative_to(self.library_dir))
            metadata = {"source": source, "file_type": path.suffix.lower()}
            if path.suffix.lower() == ".pdf":
                use_mineru_cache = os.getenv(
                    "RESEARCH_ASSISTANT_USE_MINERU_CACHE", "true"
                ).lower() in {"1", "true", "yes"}
                if use_mineru_cache and self.mineru.has_cache(path):
                    try:
                        pdf_documents, report = self.mineru.load_cached(
                            path, source=source
                        )
                    except MinerUExtractionError as exc:
                        pdf_documents, native_report = pdf_extractor.extract(
                            path, source=source
                        )
                        report = native_report.model_dump()
                        report["warning"] = f"MinerU 缓存读取失败，已回退：{exc}"
                else:
                    pdf_documents, native_report = pdf_extractor.extract(
                        path, source=source
                    )
                    report = native_report.model_dump()
                documents.extend(pdf_documents)
                self.pdf_reports[source] = report
            else:
                text = path.read_text(encoding="utf-8", errors="replace")
                if text.strip():
                    documents.append(
                        Document(
                            page_content=text,
                            metadata={**metadata, "page": None},
                        )
                    )
        return documents

    def _allowed_indexes(
        self,
        sources: list[str] | None,
        file_types: list[str] | None,
    ) -> list[int]:
        source_filter = set(sources) if sources is not None else None
        type_filter = (
            {
                item.lower() if item.startswith(".") else f".{item.lower()}"
                for item in file_types
            }
            if file_types
            else None
        )
        return [
            index
            for index, chunk in enumerate(self.chunks)
            if (source_filter is None or chunk.metadata["source"] in source_filter)
            and (type_filter is None or chunk.metadata["file_type"] in type_filter)
        ]

    def _expanded_query(self, query: str) -> str:
        expanded_query = query
        alias_matched = False
        for term, aliases in QUERY_ALIASES.items():
            if term in query.lower():
                expanded_query += f" {aliases}"
                alias_matched = True
        definition_domain = alias_matched or "heart failure" in query.lower()
        if definition_domain and any(
            word in query.lower() for word in ("什么", "定义", "what is", "definition")
        ):
            expanded_query += " definition"
        return expanded_query

    def _base_scores(self, query: str) -> np.ndarray:
        expanded_query = self._expanded_query(query)

        tfidf_scores = None
        bge_scores = None
        if self.vectorizer is not None:
            query_vector = self.vectorizer.transform([expanded_query])
            tfidf_scores = (self.chunk_matrix @ query_vector.T).toarray().ravel()

        if self.embeddings is not None:
            query_vector = np.asarray(
                self.embeddings.embed_query(expanded_query),
                dtype=np.float32,
            )
            if query_vector.ndim != 1:
                raise ValueError("BGE 问题向量必须是一维向量")
            if query_vector.shape[0] != self.embedding_matrix.shape[1]:
                raise ValueError(
                    "BGE 问题向量与资料向量维度不一致："
                    f"{query_vector.shape[0]} != {self.embedding_matrix.shape[1]}"
                )
            bge_scores = self.embedding_matrix @ query_vector

        if self.backend == "tfidf":
            return tfidf_scores
        if self.backend in {"bge", "qdrant"}:
            return np.clip(bge_scores, 0.0, 1.0)
        if self.backend == "multimodal":
            return tfidf_scores

        # 混合检索兼顾字面和语义；词面权重略高，减少泛化过度。
        return 0.6 * tfidf_scores + 0.4 * np.clip(bge_scores, 0.0, 1.0)

    @staticmethod
    def _query_terms(query: str) -> list[str]:
        terms = re.findall(r"[a-zA-Z0-9_+.-]+|[\u4e00-\u9fff]+", query.lower())
        ignored = {"什么", "怎么", "如何", "哪些", "是否", "为什么", "请问", "介绍"}
        expanded: list[str] = []
        for term in terms:
            if term in ignored:
                continue
            if re.fullmatch(r"[\u4e00-\u9fff]+", term) and len(term) > 2:
                expanded.extend(
                    term[index : index + 2] for index in range(len(term) - 1)
                )
            else:
                expanded.append(term)
        return list(dict.fromkeys(expanded))

    def _rerank_score(
        self,
        query: str,
        document: Document,
        base_score: float,
    ) -> float:
        text = document.page_content.lower()
        terms = self._query_terms(query)
        coverage = sum(term in text for term in terms) / len(terms) if terms else 0.0

        source = document.metadata["source"].lower()
        source_coverage = (
            sum(term in source for term in terms) / len(terms) if terms else 0.0
        )
        definition_query = any(
            word in query.lower()
            for word in ("什么", "定义", "definition", "标准", "指南")
        )
        authoritative_source = any(
            word in source
            for word in ("guideline", "指南", "definition", "共识", "标准")
        )
        authority_bonus = 0.06 if definition_query and authoritative_source else 0.0
        return float(
            np.clip(
                base_score + 0.08 * coverage + 0.12 * source_coverage + authority_bonus,
                0.0,
                1.0,
            )
        )

    def search_results(
        self,
        query: str,
        k: int = 5,
        min_score: float | None = None,
        sources: list[str] | None = None,
        file_types: list[str] | None = None,
    ) -> list[SearchResult]:
        """检索、过滤并重排序，返回达到最低可信分数的证据。"""
        query = query.strip()
        if not query:
            return []
        if k < 1 or k > 20:
            raise ValueError("k 必须在 1 到 20 之间")
        threshold = DEFAULT_THRESHOLDS[self.backend] if min_score is None else min_score
        if threshold < 0 or threshold > 1:
            raise ValueError("最低相关度必须在 0 到 1 之间")

        if self.backend == "multimodal" and self.unified_index is not None:
            return self._unified_search_results(
                query,
                k=k,
                min_score=threshold,
                sources=sources,
                file_types=file_types,
            )

        if self.backend == "qdrant" and self.persistent_index is not None:
            return self._persistent_search_results(
                query,
                k=k,
                min_score=threshold,
                sources=sources,
                file_types=file_types,
            )

        allowed = self._allowed_indexes(sources, file_types)
        if not allowed:
            return []

        base_scores = self._base_scores(query)
        ranked = sorted(
            (
                (
                    index,
                    self._rerank_score(
                        query,
                        self.chunks[index],
                        float(base_scores[index]),
                    ),
                    float(base_scores[index]),
                )
                for index in allowed
            ),
            key=lambda item: item[1],
            reverse=True,
        )

        results: list[SearchResult] = []
        per_source: dict[str, int] = {}
        for chunk_index, score, base_score in ranked:
            if base_score < threshold:
                continue
            document = self.chunks[chunk_index]
            source = document.metadata["source"]
            if per_source.get(source, 0) >= 3:
                continue
            per_source[source] = per_source.get(source, 0) + 1
            results.append(
                SearchResult(
                    source=source,
                    page=document.metadata.get("page"),
                    quote=" ".join(document.page_content.split()),
                    score=round(score, 4),
                    backend=(
                        "qdrant-fallback-bge"
                        if self.backend == "qdrant" and self.persistent_index is None
                        else (
                            "multimodal-fallback-tfidf"
                            if self.backend == "multimodal"
                            and self.unified_index is None
                            else self.backend
                        )
                    ),
                    chunk_id=chunk_index,
                )
            )
            if len(results) >= k:
                break
        return results

    def _persistent_search_results(
        self,
        query: str,
        *,
        k: int,
        min_score: float,
        sources: list[str] | None,
        file_types: list[str] | None,
        backend_label: str = "qdrant",
    ) -> list[SearchResult]:
        """Search Qdrant, then apply the same lightweight reranking policy."""
        hits = self.persistent_index.search(
            self._expanded_query(query),
            k=k,
            min_score=min_score,
            sources=sources,
            file_types=file_types,
        )
        ranked = sorted(
            (
                (
                    hit,
                    self._rerank_score(
                        query,
                        Document(
                            page_content=hit.text,
                            metadata={"source": hit.source},
                        ),
                        hit.score,
                    ),
                )
                for hit in hits
            ),
            key=lambda item: item[1],
            reverse=True,
        )
        results: list[SearchResult] = []
        per_source: dict[str, int] = {}
        for hit, score in ranked:
            if per_source.get(hit.source, 0) >= 3:
                continue
            per_source[hit.source] = per_source.get(hit.source, 0) + 1
            results.append(
                SearchResult(
                    source=hit.source,
                    page=hit.page,
                    quote=" ".join(hit.text.split()),
                    score=round(score, 4),
                    backend=backend_label,
                    chunk_id=hit.chunk_index,
                    evidence_id=hit.evidence_id,
                    content_type=hit.content_type,
                    bbox=hit.bbox,
                    image_path=hit.image_path,
                    modality="text",
                    text_score=round(hit.score, 4),
                )
            )
            if len(results) >= k:
                break
        return results

    def _unified_search_results(
        self,
        query: str,
        *,
        k: int,
        min_score: float,
        sources: list[str] | None,
        file_types: list[str] | None,
    ) -> list[SearchResult]:
        """Search text and image points once in the unified Qwen embedding space."""
        hits = self.unified_index.search(
            self._expanded_query(query),
            k=CANDIDATE_K,
            min_score=min_score,
            sources=sources,
            file_types=file_types,
        )
        return self.materialize_unified_results(hits, k=k, min_score=min_score)

    def materialize_unified_results(self, hits, *, k, min_score):
        """Materialize existing candidates without a second model or query call."""
        selected = select_evidence(hits, min_score, limit=k, context=self.evidence_context)
        results: list[SearchResult] = []
        for hit in selected:
            results.append(
                SearchResult(
                    source=hit["source"],
                    page=hit["page"],
                    quote=hit["text"],
                    score=round(hit["score"], 4),
                    backend="multimodal-unified",
                    chunk_id=hit["chunk_index"],
                    evidence_id=hit["point_id"],
                    content_type=hit["content_type"],
                    bbox=hit["bbox"],
                    image_path=hit["image_path"],
                    modality=hit["modality"],
                    text_score=round(hit["score"], 4)
                    if hit["modality"] == "text"
                    else None,
                    visual_score=round(hit["score"], 4)
                    if hit["modality"] == "image"
                    else None,
                    retrieved_text=hit["retrieved_text"],
                    context_span=hit["context_span"],
                    quality_warnings=hit["quality_warnings"],
                    retrieval_policy=hit["retrieval_policy"],
                    context_evidence_ids=hit["context_evidence_ids"],
                )
            )
        return results

    def search(
        self,
        query: str,
        k: int = 5,
        min_score: float | None = None,
        sources: list[str] | None = None,
        file_types: list[str] | None = None,
    ) -> str:
        """把结构化结果转换成 Agent 易于引用的证据文本。"""
        results = self.search_results(
            query,
            k=k,
            min_score=min_score,
            sources=sources,
            file_types=file_types,
        )
        return self.format_results(results)

    @staticmethod
    def format_results(results: list[SearchResult]) -> str:
        """把结构化结果转换成 Agent 易于引用的证据文本。"""
        if not results:
            return "NO_RELIABLE_EVIDENCE: 资料库中没有达到最低相关度的证据。"

        formatted = []
        for index, result in enumerate(results, start=1):
            page_text = str(result.page) if result.page is not None else "N/A"
            formatted.append(
                f"[S{index}]\n"
                f"SOURCE: {result.source}\n"
                f"PAGE: {page_text}\n"
                f"SCORE: {result.score:.4f}\n"
                f"MODALITY: {result.modality}\n"
                f"QUALITY: {'; '.join(result.quality_warnings) or '自动检索结果，非人工审核'}\n"
                f"QUOTE: {result.quote}"
            )
        return "\n\n".join(formatted)
