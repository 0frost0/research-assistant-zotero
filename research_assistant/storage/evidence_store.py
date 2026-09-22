"""SQLite evidence store for parsed, versioned research content."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


SCHEMA_VERSION = 3


@dataclass(frozen=True)
class EvidenceRecord:
    evidence_id: str
    document_id: str
    version: str
    ordinal: int
    source: str
    file_type: str
    page: int | None
    bbox: object | None
    content_type: str
    text: str
    image_path: str | None
    start_index: int
    content_hash: str
    parser_version: str
    embedding_model: str
    embedding_dimension: int


@dataclass(frozen=True)
class VisualCandidate:
    document_id: str
    document_version: str
    source: str
    file_type: str
    page: int | None
    bbox: object | None
    content_type: str
    image_path: str
    caption: str


@dataclass(frozen=True)
class VisualRecord:
    visual_id: str
    document_id: str
    document_version: str
    version: str
    source: str
    file_type: str
    page: int | None
    bbox: object | None
    content_type: str
    image_path: str
    caption: str
    image_hash: str
    visual_model: str
    visual_dimension: int


class EvidenceStore:
    """Canonical evidence metadata and activation state.

    Each operation opens a short-lived connection so the web worker can safely
    use the store from different threads.
    """

    def __init__(self, path: Path):
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    @contextmanager
    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schema_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS documents (
                    document_id TEXT PRIMARY KEY,
                    source TEXT NOT NULL UNIQUE,
                    active_version TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS evidence (
                    evidence_id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    version TEXT NOT NULL,
                    ordinal INTEGER NOT NULL,
                    source TEXT NOT NULL,
                    file_type TEXT NOT NULL,
                    page INTEGER,
                    bbox_json TEXT,
                    content_type TEXT NOT NULL,
                    text TEXT NOT NULL,
                    image_path TEXT,
                    start_index INTEGER NOT NULL,
                    content_hash TEXT NOT NULL,
                    parser_version TEXT NOT NULL,
                    embedding_model TEXT NOT NULL,
                    embedding_dimension INTEGER NOT NULL,
                    active INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(document_id) REFERENCES documents(document_id)
                );
                CREATE INDEX IF NOT EXISTS evidence_active_idx
                    ON evidence(active, source);
                CREATE INDEX IF NOT EXISTS evidence_document_version_idx
                    ON evidence(document_id, version);
                CREATE TABLE IF NOT EXISTS visual_assets (
                    visual_id TEXT PRIMARY KEY,
                    document_id TEXT NOT NULL,
                    document_version TEXT NOT NULL,
                    version TEXT NOT NULL,
                    source TEXT NOT NULL,
                    file_type TEXT NOT NULL,
                    page INTEGER,
                    bbox_json TEXT,
                    content_type TEXT NOT NULL,
                    image_path TEXT NOT NULL,
                    caption TEXT NOT NULL,
                    image_hash TEXT NOT NULL,
                    visual_model TEXT NOT NULL,
                    visual_dimension INTEGER NOT NULL,
                    active INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(document_id) REFERENCES documents(document_id)
                );
                CREATE INDEX IF NOT EXISTS visual_assets_active_idx
                    ON visual_assets(active, source);
                CREATE TABLE IF NOT EXISTS index_state (
                    index_name TEXT NOT NULL,
                    source TEXT NOT NULL,
                    evidence_version TEXT NOT NULL,
                    point_count INTEGER NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(index_name, source)
                );
                """
            )
            connection.execute(
                "INSERT OR REPLACE INTO schema_meta(key, value) VALUES(?, ?)",
                ("schema_version", str(SCHEMA_VERSION)),
            )

    def is_active_version(self, source: str, version: str) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT 1 FROM documents WHERE source=? AND active_version=?",
                (source, version),
            ).fetchone()
        return row is not None

    def stage(self, records: list[EvidenceRecord]) -> None:
        if not records:
            return
        first = records[0]
        if any(
            item.document_id != first.document_id or item.version != first.version
            for item in records
        ):
            raise ValueError("一次只能暂存同一文档版本的证据")

        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO documents(document_id, source, active_version, updated_at)
                VALUES(?, ?, NULL, ?)
                ON CONFLICT(document_id) DO UPDATE SET
                    source=excluded.source,
                    updated_at=excluded.updated_at
                """,
                (first.document_id, first.source, now),
            )
            connection.execute(
                "DELETE FROM evidence WHERE document_id=? AND version=?",
                (first.document_id, first.version),
            )
            connection.executemany(
                """
                INSERT INTO evidence(
                    evidence_id, document_id, version, ordinal, source,
                    file_type, page, bbox_json, content_type, text, image_path,
                    start_index, content_hash, parser_version, embedding_model,
                    embedding_dimension, active, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                """,
                [
                    (
                        item.evidence_id,
                        item.document_id,
                        item.version,
                        item.ordinal,
                        item.source,
                        item.file_type,
                        item.page,
                        json.dumps(item.bbox, ensure_ascii=False)
                        if item.bbox is not None
                        else None,
                        item.content_type,
                        item.text,
                        item.image_path,
                        item.start_index,
                        item.content_hash,
                        item.parser_version,
                        item.embedding_model,
                        item.embedding_dimension,
                        now,
                    )
                    for item in records
                ],
            )

    def activate(self, document_id: str, version: str) -> list[str]:
        """Atomically expose a staged version and return obsolete vector IDs."""
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            obsolete = [
                row[0]
                for row in connection.execute(
                    """
                    SELECT evidence_id FROM evidence
                    WHERE document_id=? AND active=1 AND version<>?
                    """,
                    (document_id, version),
                ).fetchall()
            ]
            staged = connection.execute(
                "SELECT COUNT(*) FROM evidence WHERE document_id=? AND version=?",
                (document_id, version),
            ).fetchone()[0]
            if not staged:
                raise ValueError("不能激活没有证据记录的文档版本")
            connection.execute(
                "UPDATE evidence SET active=0 WHERE document_id=?",
                (document_id,),
            )
            connection.execute(
                "UPDATE evidence SET active=1 WHERE document_id=? AND version=?",
                (document_id, version),
            )
            connection.execute(
                "UPDATE documents SET active_version=?, updated_at=? WHERE document_id=?",
                (version, now, document_id),
            )
        return obsolete

    def deactivate_missing_sources(self, sources: set[str]) -> list[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT document_id, source FROM documents WHERE active_version IS NOT NULL"
            ).fetchall()
            missing_ids = [row["document_id"] for row in rows if row["source"] not in sources]
            if not missing_ids:
                return []
            placeholders = ",".join("?" for _ in missing_ids)
            obsolete = [
                row[0]
                for row in connection.execute(
                    f"SELECT evidence_id FROM evidence WHERE active=1 AND document_id IN ({placeholders})",
                    missing_ids,
                ).fetchall()
            ]
            connection.execute(
                f"UPDATE evidence SET active=0 WHERE document_id IN ({placeholders})",
                missing_ids,
            )
            connection.execute(
                f"UPDATE documents SET active_version=NULL WHERE document_id IN ({placeholders})",
                missing_ids,
            )
        return obsolete

    def active_ids(self, evidence_ids: Iterable[str]) -> set[str]:
        values = list(evidence_ids)
        if not values:
            return set()
        placeholders = ",".join("?" for _ in values)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT evidence_id FROM evidence WHERE active=1 AND evidence_id IN ({placeholders})",
                values,
            ).fetchall()
        return {row[0] for row in rows}

    def purge_inactive(self) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM evidence WHERE active=0")

    def active_image_candidates(self) -> list[VisualCandidate]:
        """Return one candidate per document/image, preserving source ownership."""
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT e.document_id, e.version, e.source, e.file_type, e.page,
                       e.bbox_json, e.content_type, e.image_path, e.text
                FROM evidence e
                WHERE e.active=1 AND e.image_path IS NOT NULL
                ORDER BY e.source, e.page, e.ordinal
                """
            ).fetchall()

        grouped: dict[tuple[str, str], list[sqlite3.Row]] = {}
        for row in rows:
            grouped.setdefault((row["document_id"], row["image_path"]), []).append(row)

        candidates: list[VisualCandidate] = []
        for (_, image_path), image_rows in grouped.items():
            path = Path(image_path)
            if not path.is_file():
                continue
            first = image_rows[0]
            captions: list[str] = []
            for row in image_rows:
                normalized = " ".join(row["text"].split())
                if normalized and normalized not in captions:
                    captions.append(normalized)
            candidates.append(
                VisualCandidate(
                    document_id=first["document_id"],
                    document_version=first["version"],
                    source=first["source"],
                    file_type=first["file_type"],
                    page=first["page"],
                    bbox=json.loads(first["bbox_json"])
                    if first["bbox_json"]
                    else None,
                    content_type=first["content_type"],
                    image_path=image_path,
                    caption=" ".join(captions)[:4000],
                )
            )
        return candidates

    def active_visual_versions(self) -> set[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT version FROM visual_assets WHERE active=1"
            ).fetchall()
        return {row[0] for row in rows}

    def stage_visual(self, records: list[VisualRecord]) -> None:
        if not records:
            return
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.executemany(
                """
                INSERT INTO visual_assets(
                    visual_id, document_id, document_version, version, source,
                    file_type, page, bbox_json, content_type, image_path,
                    caption, image_hash, visual_model, visual_dimension,
                    active, created_at
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                ON CONFLICT(visual_id) DO UPDATE SET
                    document_version=excluded.document_version,
                    source=excluded.source,
                    file_type=excluded.file_type,
                    page=excluded.page,
                    bbox_json=excluded.bbox_json,
                    content_type=excluded.content_type,
                    image_path=excluded.image_path,
                    caption=excluded.caption,
                    image_hash=excluded.image_hash,
                    visual_model=excluded.visual_model,
                    visual_dimension=excluded.visual_dimension
                """,
                [
                    (
                        item.visual_id,
                        item.document_id,
                        item.document_version,
                        item.version,
                        item.source,
                        item.file_type,
                        item.page,
                        json.dumps(item.bbox, ensure_ascii=False)
                        if item.bbox is not None
                        else None,
                        item.content_type,
                        item.image_path,
                        item.caption,
                        item.image_hash,
                        item.visual_model,
                        item.visual_dimension,
                        now,
                    )
                    for item in records
                ],
            )

    def activate_visual_set(self, visual_ids: list[str]) -> list[str]:
        """Atomically publish the complete visual snapshot."""
        with self._connect() as connection:
            active_rows = connection.execute(
                "SELECT visual_id FROM visual_assets WHERE active=1"
            ).fetchall()
            current = set(visual_ids)
            obsolete = [row[0] for row in active_rows if row[0] not in current]
            connection.execute("UPDATE visual_assets SET active=0")
            if visual_ids:
                placeholders = ",".join("?" for _ in visual_ids)
                connection.execute(
                    f"UPDATE visual_assets SET active=1 WHERE visual_id IN ({placeholders})",
                    visual_ids,
                )
        return obsolete

    def active_visual_ids(self, visual_ids: Iterable[str]) -> set[str]:
        values = list(visual_ids)
        if not values:
            return set()
        placeholders = ",".join("?" for _ in values)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT v.visual_id FROM visual_assets v JOIN documents d "
                f"ON d.document_id=v.document_id AND d.active_version=v.document_version "
                f"WHERE v.active=1 AND v.visual_id IN ({placeholders})",
                values,
            ).fetchall()
        return {row[0] for row in rows}

    def visual_path(self, visual_id: str) -> Path | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT v.image_path FROM visual_assets v JOIN documents d "
                "ON d.document_id=v.document_id AND d.active_version=v.document_version "
                "WHERE v.visual_id=? AND v.active=1",
                (visual_id,),
            ).fetchone()
        return Path(row[0]).resolve() if row else None

    def purge_inactive_visual(self) -> None:
        with self._connect() as connection:
            connection.execute("DELETE FROM visual_assets WHERE active=0")

    def is_index_current(
        self,
        index_name: str,
        source: str,
        evidence_version: str,
        point_count: int,
    ) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT 1 FROM index_state
                WHERE index_name=? AND source=? AND evidence_version=?
                      AND point_count=?
                """,
                (index_name, source, evidence_version, point_count),
            ).fetchone()
        return row is not None

    def mark_index_current(
        self,
        index_name: str,
        source: str,
        evidence_version: str,
        point_count: int,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO index_state(
                    index_name, source, evidence_version, point_count, updated_at
                ) VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(index_name, source) DO UPDATE SET
                    evidence_version=excluded.evidence_version,
                    point_count=excluded.point_count,
                    updated_at=excluded.updated_at
                """,
                (index_name, source, evidence_version, point_count, now),
            )

    def indexed_sources(self, index_name: str) -> set[str]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT source FROM index_state WHERE index_name=?",
                (index_name,),
            ).fetchall()
        return {row[0] for row in rows}

    def remove_index_sources(self, index_name: str, sources: set[str]) -> None:
        if not sources:
            return
        placeholders = ",".join("?" for _ in sources)
        with self._connect() as connection:
            connection.execute(
                f"DELETE FROM index_state WHERE index_name=? AND source IN ({placeholders})",
                [index_name, *sorted(sources)],
            )

    def stats(self) -> dict[str, int]:
        with self._connect() as connection:
            documents = connection.execute(
                "SELECT COUNT(*) FROM documents WHERE active_version IS NOT NULL"
            ).fetchone()[0]
            evidence = connection.execute(
                "SELECT COUNT(*) FROM evidence WHERE active=1"
            ).fetchone()[0]
            visual_assets = connection.execute(
                "SELECT COUNT(*) FROM visual_assets WHERE active=1"
            ).fetchone()[0]
        return {
            "documents": documents,
            "evidence": evidence,
            "visual_assets": visual_assets,
        }
