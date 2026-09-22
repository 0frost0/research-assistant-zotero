"""One-model, one-collection multimodal retrieval with versioned embeddings."""

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

from research_assistant.storage.evidence_store import EvidenceRecord, EvidenceStore, VisualCandidate, VisualRecord
from research_assistant.retrieval.qdrant_index import DEFAULT_QDRANT_URL, ID_NAMESPACE, INDEX_SCHEMA_VERSION


UNIFIED_INDEX_SCHEMA = "unified-multimodal-v2-image-caption"
DEFAULT_UNIFIED_COLLECTION = "research_assistant_multimodal_v1"


@dataclass(frozen=True)
class UnifiedSearchHit:
    point_id: str
    modality: str
    source: str
    file_type: str
    page: int | None
    text: str
    score: float
    chunk_index: int
    content_type: str
    bbox: object | None
    image_path: str | None


class UnifiedMultimodalIndex:
    """Encode text, images, and queries with one model and search once."""

    def __init__(
        self,
        *,
        embeddings: Any,
        evidence_path: Path,
        qdrant_url: str | None = None,
        collection_base: str | None = None,
        client: QdrantClient | None = None,
    ) -> None:
        self.embeddings = embeddings
        self.model_name = embeddings.model_name
        self.dimension = embeddings.dimension
        self.store = EvidenceStore(evidence_path)
        self.qdrant_url = qdrant_url or os.getenv(
            "RESEARCH_ASSISTANT_QDRANT_URL", DEFAULT_QDRANT_URL
        )
        base = collection_base or os.getenv(
            "RESEARCH_ASSISTANT_MULTIMODAL_COLLECTION",
            DEFAULT_UNIFIED_COLLECTION,
        )
        model_tag = hashlib.sha256(self.model_name.encode("utf-8")).hexdigest()[:10]
        safe_base = re.sub(r"[^a-zA-Z0-9_-]+", "_", base).strip("_")
        self.collection_name = f"{safe_base}_{self.dimension}_{model_tag}"
        self.client = client or QdrantClient(url=self.qdrant_url, timeout=20)
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
                    "index_schema": UNIFIED_INDEX_SCHEMA,
                    "modalities": ["text", "image"],
                },
            )
        info = self.client.get_collection(self.collection_name)
        size = getattr(info.config.params.vectors, "size", None)
        if size != self.dimension:
            raise ValueError(
                "Unified Qdrant collection dimension mismatch: "
                f"{size} != {self.dimension}"
            )

    def _upsert_batches(self, points: list[models.PointStruct]) -> None:
        batch_size = max(1, min(96, int(os.getenv("RESEARCH_ASSISTANT_QDRANT_BATCH_SIZE", "96"))))
        for start in range(0, len(points), batch_size):
            self.client.upsert(
                collection_name=self.collection_name,
                points=points[start : start + batch_size],
                wait=True,
            )

    @staticmethod
    def _json_value(value: object) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)

    def _text_records(
        self,
        source: str,
        indexed_chunks: list[tuple[int, Document]],
    ) -> tuple[str, str, list[EvidenceRecord]]:
        document_id = str(uuid.uuid5(ID_NAMESPACE, source))
        digest = hashlib.sha256(INDEX_SCHEMA_VERSION.encode("utf-8"))
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
            records.append(
                EvidenceRecord(
                    evidence_id=str(
                        uuid.uuid5(ID_NAMESPACE, f"{document_id}:{version}:{ordinal}")
                    ),
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

    def _visual_record(self, candidate: VisualCandidate) -> VisualRecord:
        path = Path(candidate.image_path)
        image_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        digest = hashlib.sha256(UNIFIED_INDEX_SCHEMA.encode("utf-8"))
        for value in (
            candidate.document_id,
            image_hash,
            candidate.document_version,
            candidate.caption,
        ):
            digest.update(value.encode("utf-8"))
        version = digest.hexdigest()
        return VisualRecord(
            visual_id=str(uuid.uuid5(ID_NAMESPACE, f"unified-visual:{version}")),
            document_id=candidate.document_id,
            document_version=candidate.document_version,
            version=version,
            source=candidate.source,
            file_type=candidate.file_type,
            page=candidate.page,
            bbox=candidate.bbox,
            content_type=candidate.content_type,
            image_path=candidate.image_path,
            caption=candidate.caption,
            image_hash=image_hash,
            visual_model=self.model_name,
            visual_dimension=self.dimension,
        )

    @staticmethod
    def _text_point(item: EvidenceRecord, vector: list[float]) -> models.PointStruct:
        return models.PointStruct(
            id=item.evidence_id,
            vector=vector,
            payload={
                "point_id": item.evidence_id,
                "modality": "text",
                "source": item.source,
                "file_type": item.file_type,
                "page": item.page,
                "bbox": item.bbox,
                "content_type": item.content_type,
                "text": item.text,
                "image_path": item.image_path,
                "content_hash": item.content_hash,
                "embedding_model": item.embedding_model,
                "embedding_dimension": item.embedding_dimension,
                "chunk_index": item.ordinal,
            },
        )

    @staticmethod
    def _image_point(item: VisualRecord, vector: list[float]) -> models.PointStruct:
        return models.PointStruct(
            id=item.visual_id,
            vector=vector,
            payload={
                "point_id": item.visual_id,
                "modality": "image",
                "source": item.source,
                "file_type": item.file_type,
                "page": item.page,
                "bbox": item.bbox,
                "content_type": item.content_type,
                "text": item.caption,
                "image_path": item.image_path,
                "image_hash": item.image_hash,
                "embedding_model": item.visual_model,
                "embedding_dimension": item.visual_dimension,
                "chunk_index": -1,
            },
        )

    def sync(self, chunks: list[Document]) -> dict[str, int]:
        grouped: dict[str, list[tuple[int, Document]]] = {}
        for index, chunk in enumerate(chunks):
            grouped.setdefault(str(chunk.metadata["source"]), []).append((index, chunk))

        text_index_name = f"{self.collection_name}:text"
        text_indexed = 0
        text_skipped = 0
        for source, indexed_chunks in grouped.items():
            document_id, version, records = self._text_records(source, indexed_chunks)
            evidence_current = self.store.is_active_version(source, version)
            index_current = self.store.is_index_current(
                text_index_name, source, version, len(records)
            )
            if index_current:
                index_current = self._contains_points([item.evidence_id for item in records])
            if not evidence_current:
                self.store.stage(records)
            if not index_current:
                vectors = self.embeddings.embed_texts([item.text for item in records])
                if any(len(vector) != self.dimension for vector in vectors):
                    raise ValueError("Unified text vector dimension mismatch")
                self._upsert_batches(
                    [
                        self._text_point(item, vector)
                        for item, vector in zip(records, vectors, strict=True)
                    ]
                )
                text_indexed += len(records)
            else:
                text_skipped += len(records)
            if not evidence_current:
                self.store.activate(document_id, version)
            self._delete_stale_source(
                source,
                "text",
                {item.evidence_id for item in records},
            )
            self.store.mark_index_current(
                text_index_name, source, version, len(records)
            )

        current_sources = set(grouped)
        self.store.deactivate_missing_sources(current_sources)
        self._delete_missing_sources(text_index_name, current_sources, "text")
        self.store.purge_inactive()

        visual_records = [
            self._visual_record(item) for item in self.store.active_image_candidates()
        ]
        self.store.stage_visual(visual_records)
        visual_grouped: dict[str, list[VisualRecord]] = {}
        for record in visual_records:
            visual_grouped.setdefault(record.source, []).append(record)

        image_index_name = f"{self.collection_name}:image"
        image_indexed = 0
        image_skipped = 0
        for source, records in visual_grouped.items():
            version = hashlib.sha256(
                "".join(sorted(item.version for item in records)).encode("ascii")
            ).hexdigest()
            index_current = self.store.is_index_current(
                image_index_name, source, version, len(records)
            )
            if index_current:
                index_current = self._contains_points([item.visual_id for item in records])
            if not index_current:
                vectors = self.embeddings.embed_images(
                    [Path(item.image_path) for item in records],
                    captions=[item.caption for item in records],
                )
                if any(len(vector) != self.dimension for vector in vectors):
                    raise ValueError("Unified image vector dimension mismatch")
                self._upsert_batches(
                    [
                        self._image_point(item, vector)
                        for item, vector in zip(records, vectors, strict=True)
                    ]
                )
                image_indexed += len(records)
            else:
                image_skipped += len(records)
            self._delete_stale_source(
                source,
                "image",
                {item.visual_id for item in records},
            )
            self.store.mark_index_current(
                image_index_name, source, version, len(records)
            )

        obsolete = self.store.activate_visual_set(
            [item.visual_id for item in visual_records]
        )
        if obsolete:
            self.client.delete(
                collection_name=self.collection_name,
                points_selector=models.PointIdsList(points=obsolete),
                wait=True,
            )
        self._delete_missing_sources(image_index_name, set(visual_grouped), "image")
        self.store.purge_inactive_visual()
        return {
            "text_indexed": text_indexed,
            "text_skipped": text_skipped,
            "image_indexed": image_indexed,
            "image_skipped": image_skipped,
        }

    def _contains_points(self, point_ids: list[str]) -> bool:
        # SQLite is not proof that a Qdrant backup/restore still has the vectors.
        for start in range(0, len(point_ids), 256):
            expected = set(point_ids[start:start + 256])
            points = self.client.retrieve(self.collection_name, ids=list(expected),
                                          with_payload=False, with_vectors=False)
            if {str(point.id) for point in points} != expected:
                return False
        return True

    def _filter(self, source: str, modality: str) -> models.Filter:
        return models.Filter(
            must=[
                models.FieldCondition(
                    key="source", match=models.MatchValue(value=source)
                ),
                models.FieldCondition(
                    key="modality", match=models.MatchValue(value=modality)
                ),
            ]
        )

    def _delete_stale_source(
        self,
        source: str,
        modality: str,
        current_ids: set[str],
    ) -> None:
        stored_ids: set[str] = set()
        offset = None
        while True:
            points, offset = self.client.scroll(
                collection_name=self.collection_name,
                scroll_filter=self._filter(source, modality),
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

    def _delete_missing_sources(
        self,
        index_name: str,
        current_sources: set[str],
        modality: str,
    ) -> None:
        missing = self.store.indexed_sources(index_name) - current_sources
        for source in missing:
            self.client.delete(
                collection_name=self.collection_name,
                points_selector=self._filter(source, modality),
                wait=True,
            )
        self.store.remove_index_sources(index_name, missing)

    def search(
        self,
        query: str,
        *,
        k: int,
        min_score: float,
        sources: list[str] | None = None,
        file_types: list[str] | None = None,
        query_image: Path | None = None,
    ) -> list[UnifiedSearchHit]:
        if sources == [] or file_types == []:
            return []
        vector = (self.embeddings.embed_query(query, image_path=query_image)
                  if query_image is not None else self.embeddings.embed_query(query))
        if len(vector) != self.dimension:
            raise ValueError("Query and unified collection dimensions do not match")
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
            limit=max(k * 5, 30),
            score_threshold=min_score,
            with_payload=True,
        )
        text_ids = [
            str(point.id)
            for point in response.points
            if (point.payload or {}).get("modality") == "text"
        ]
        image_ids = [
            str(point.id)
            for point in response.points
            if (point.payload or {}).get("modality") == "image"
        ]
        active_text = self.store.active_ids(text_ids)
        active_images = self.store.active_visual_ids(image_ids)

        hits: list[UnifiedSearchHit] = []
        for point in response.points:
            payload = point.payload or {}
            point_id = str(point.id)
            modality = str(payload.get("modality") or "text")
            if modality == "text" and point_id not in active_text:
                continue
            if modality == "image" and point_id not in active_images:
                continue
            hits.append(
                UnifiedSearchHit(
                    point_id=point_id,
                    modality=modality,
                    source=str(payload.get("source") or ""),
                    file_type=str(payload.get("file_type") or ""),
                    page=payload.get("page"),
                    text=str(payload.get("text") or ""),
                    score=float(point.score),
                    chunk_index=int(payload.get("chunk_index") or 0),
                    content_type=str(payload.get("content_type") or modality),
                    bbox=payload.get("bbox"),
                    image_path=payload.get("image_path"),
                )
            )
        return hits
