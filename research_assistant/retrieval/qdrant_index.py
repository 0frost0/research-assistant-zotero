"""Persistent dense retrieval backed by Qdrant and the SQLite evidence store."""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_core.documents import Document
from research_assistant.core.paths import version_metadata
from qdrant_client import QdrantClient, models

from research_assistant.storage.evidence_store import EvidenceRecord, EvidenceStore


INDEX_SCHEMA_VERSION = "canonical-evidence-v2/chunk-450-80"
DEFAULT_QDRANT_URL = "http://127.0.0.1:6333"
DEFAULT_COLLECTION = "research_assistant_text_v1"
ID_NAMESPACE = uuid.UUID("d29a2189-15c1-42aa-a756-947c369eb8cc")


@dataclass(frozen=True)
class PersistentSearchHit:
    evidence_id: str
    source: str
    file_type: str
    page: int | None
    text: str
    score: float
    chunk_index: int
    content_type: str
    bbox: object | None
    image_path: str | None


class PersistentTextIndex:
    def __init__(
        self,
        *,
        embeddings: Any,
        model_name: str,
        dimension: int,
        evidence_path: Path,
        qdrant_url: str | None = None,
        collection_base: str | None = None,
        client: QdrantClient | None = None,
    ) -> None:
        self.embeddings = embeddings
        self.model_name = model_name
        self.dimension = dimension
        self.store = EvidenceStore(evidence_path)
        self.qdrant_url = qdrant_url or os.getenv(
            "RESEARCH_ASSISTANT_QDRANT_URL", DEFAULT_QDRANT_URL
        )
        base = collection_base or os.getenv(
            "RESEARCH_ASSISTANT_QDRANT_COLLECTION", DEFAULT_COLLECTION
        )
        model_tag = hashlib.sha256(model_name.encode("utf-8")).hexdigest()[:10]
        safe_base = re.sub(r"[^a-zA-Z0-9_-]+", "_", base).strip("_")
        self.collection_name = f"{safe_base}_{dimension}_{model_tag}"
        self.client = client or QdrantClient(url=self.qdrant_url, timeout=10)
        self._ensure_collection()

    def _ensure_collection(self) -> None:
        if not self.client.collection_exists(self.collection_name):
            self.client.create_collection(
                collection_name=self.collection_name,
                vectors_config=models.VectorParams(
                    size=self.dimension,
                    distance=models.Distance.COSINE,
                ),
                metadata={
                    "embedding_model": self.model_name,
                    "embedding_dimension": self.dimension,
                    "index_schema": INDEX_SCHEMA_VERSION,
                },
            )
        info = self.client.get_collection(self.collection_name)
        vector_config = info.config.params.vectors
        size = getattr(vector_config, "size", None)
        if size != self.dimension:
            raise ValueError(
                "Qdrant collection vector dimension mismatch: "
                f"{size} != {self.dimension}"
            )

    @staticmethod
    def _json_value(value: object) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)

    def _records_for_source(
        self,
        source: str,
        indexed_chunks: list[tuple[int, Document]],
    ) -> tuple[str, str, list[EvidenceRecord]]:
        document_id = str(uuid.uuid5(ID_NAMESPACE, source))
        digest = hashlib.sha256()
        digest.update(INDEX_SCHEMA_VERSION.encode("utf-8"))
        for _, chunk in indexed_chunks:
            digest.update(chunk.page_content.encode("utf-8"))
            digest.update(self._json_value(version_metadata(chunk.metadata)).encode("utf-8"))
        version = digest.hexdigest()

        records: list[EvidenceRecord] = []
        for ordinal, (chunk_index, chunk) in enumerate(indexed_chunks):
            metadata = chunk.metadata
            content_hash = hashlib.sha256(
                chunk.page_content.encode("utf-8")
            ).hexdigest()
            evidence_id = str(
                uuid.uuid5(ID_NAMESPACE, f"{document_id}:{version}:{ordinal}")
            )
            records.append(
                EvidenceRecord(
                    evidence_id=evidence_id,
                    document_id=document_id,
                    version=version,
                    ordinal=chunk_index,
                    source=source,
                    file_type=str(metadata.get("file_type") or ""),
                    page=metadata.get("page"),
                    bbox=metadata.get("bbox"),
                    content_type=str(metadata.get("content_type") or "text"),
                    text=chunk.page_content,
                    image_path=metadata.get("image_path"),
                    start_index=int(metadata.get("start_index") or 0),
                    content_hash=content_hash,
                    parser_version=str(
                        metadata.get("extraction_method") or INDEX_SCHEMA_VERSION
                    ),
                    embedding_model=self.model_name,
                    embedding_dimension=self.dimension,
                )
            )
        return document_id, version, records

    def sync(self, chunks: list[Document]) -> dict[str, int]:
        grouped: dict[str, list[tuple[int, Document]]] = {}
        for index, chunk in enumerate(chunks):
            grouped.setdefault(str(chunk.metadata["source"]), []).append((index, chunk))

        updated = 0
        indexed = 0
        skipped = 0
        for source, indexed_chunks in grouped.items():
            document_id, version, records = self._records_for_source(
                source, indexed_chunks
            )
            evidence_current = self.store.is_active_version(source, version)
            index_current = self.store.is_index_current(
                self.collection_name,
                source,
                version,
                len(records),
            )
            if evidence_current and index_current:
                skipped += 1
                continue

            if not evidence_current:
                self.store.stage(records)
            vectors = self.embeddings.embed_documents([item.text for item in records])
            if any(len(vector) != self.dimension for vector in vectors):
                raise ValueError("文档向量维度与当前嵌入模型配置不一致")
            points = [
                models.PointStruct(
                    id=item.evidence_id,
                    vector=vector,
                    payload={
                        "document_id": item.document_id,
                        "evidence_id": item.evidence_id,
                        "version": item.version,
                        "content_hash": item.content_hash,
                        "source": item.source,
                        "file_type": item.file_type,
                        "page": item.page,
                        "bbox": item.bbox,
                        "content_type": item.content_type,
                        "text": item.text,
                        "image_path": item.image_path,
                        "parser_version": item.parser_version,
                        "embedding_model": item.embedding_model,
                        "embedding_dimension": item.embedding_dimension,
                        "chunk_index": item.ordinal,
                    },
                )
                for item, vector in zip(records, vectors, strict=True)
            ]
            batch_size = int(os.getenv("RESEARCH_ASSISTANT_QDRANT_BATCH_SIZE", "96"))
            for start in range(0, len(points), batch_size):
                self.client.upsert(
                    collection_name=self.collection_name,
                    points=points[start : start + batch_size],
                    wait=True,
                )
            if not evidence_current:
                self.store.activate(document_id, version)
            self._delete_stale_source(
                source,
                {item.evidence_id for item in records},
            )
            self.store.mark_index_current(
                self.collection_name,
                source,
                version,
                len(records),
            )
            updated += 1
            indexed += len(records)

        current_sources = set(grouped)
        self.store.deactivate_missing_sources(current_sources)
        missing_sources = self.store.indexed_sources(self.collection_name) - current_sources
        for source in missing_sources:
            self.client.delete(
                collection_name=self.collection_name,
                points_selector=models.Filter(
                    must=[
                        models.FieldCondition(
                            key="source", match=models.MatchValue(value=source)
                        )
                    ]
                ),
                wait=True,
            )
        self.store.remove_index_sources(self.collection_name, missing_sources)
        self.store.purge_inactive()
        return {"updated": updated, "indexed": indexed, "skipped": skipped}

    def _delete_stale_source(self, source: str, current_ids: set[str]) -> None:
        stored_ids: set[str] = set()
        offset = None
        query_filter = models.Filter(
            must=[
                models.FieldCondition(
                    key="source", match=models.MatchValue(value=source)
                )
            ]
        )
        while True:
            points, offset = self.client.scroll(
                collection_name=self.collection_name,
                scroll_filter=query_filter,
                limit=256,
                offset=offset,
                with_payload=False,
                with_vectors=False,
            )
            stored_ids.update(str(point.id) for point in points)
            if offset is None:
                break
        stale = sorted(stored_ids - current_ids)
        if stale:
            self.client.delete(
                collection_name=self.collection_name,
                points_selector=models.PointIdsList(points=stale),
                wait=True,
            )

    def search(
        self,
        query: str,
        *,
        k: int,
        min_score: float,
        sources: list[str] | None = None,
        file_types: list[str] | None = None,
    ) -> list[PersistentSearchHit]:
        vector = self.embeddings.embed_query(query)
        if len(vector) != self.dimension:
            raise ValueError("问题向量与 Qdrant collection 的向量维度不一致")

        must: list[models.FieldCondition] = [
            models.FieldCondition(
                key="embedding_model", match=models.MatchValue(value=self.model_name)
            ),
            models.FieldCondition(
                key="embedding_dimension", match=models.MatchValue(value=self.dimension)
            ),
        ]
        if sources:
            must.append(
                models.FieldCondition(key="source", match=models.MatchAny(any=sources))
            )
        if file_types:
            normalized = [
                item.lower() if item.startswith(".") else f".{item.lower()}"
                for item in file_types
            ]
            must.append(
                models.FieldCondition(
                    key="file_type", match=models.MatchAny(any=normalized)
                )
            )

        response = self.client.query_points(
            collection_name=self.collection_name,
            query=vector,
            query_filter=models.Filter(must=must),
            limit=max(k * 6, 30),
            with_payload=True,
            score_threshold=min_score,
        )
        active = self.store.active_ids(str(point.id) for point in response.points)
        hits: list[PersistentSearchHit] = []
        for point in response.points:
            evidence_id = str(point.id)
            if evidence_id not in active:
                continue
            payload = point.payload or {}
            hits.append(
                PersistentSearchHit(
                    evidence_id=evidence_id,
                    source=str(payload.get("source") or ""),
                    file_type=str(payload.get("file_type") or ""),
                    page=payload.get("page"),
                    text=str(payload.get("text") or ""),
                    score=float(point.score),
                    chunk_index=int(payload.get("chunk_index") or 0),
                    content_type=str(payload.get("content_type") or "text"),
                    bbox=payload.get("bbox"),
                    image_path=payload.get("image_path"),
                )
            )
        return hits


def qdrant_service_status(
    evidence_path: Path,
    qdrant_url: str | None = None,
) -> dict[str, Any]:
    url = qdrant_url or os.getenv("RESEARCH_ASSISTANT_QDRANT_URL", DEFAULT_QDRANT_URL)
    result: dict[str, Any] = {"url": url, "available": False}
    client = None
    try:
        client = QdrantClient(url=url, timeout=2)
        collections = client.get_collections().collections
        result.update(
            {
                "available": True,
                "collections": [item.name for item in collections],
                "evidence_store": EvidenceStore(evidence_path).stats(),
            }
        )
    except Exception as exc:
        result["detail"] = f"{type(exc).__name__}: {exc}"
    finally:
        if client is not None:
            client.close()
    return result
