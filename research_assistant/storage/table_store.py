"""Versioned MinerU tables, original cells and bounded read-only SQL."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager
from html.parser import HTMLParser
from pathlib import Path

from research_assistant.ingestion.mineru_extractor import MinerUExtractor

TABLE_SCHEMA = "mineru-cells-v1"
NAMESPACE = uuid.UUID("1c680291-7f8a-43c0-b97a-70e46b9631c5")


class TableHTMLParser(HTMLParser):
    """Use the standard HTML tokenizer; keep cells and spans without inferring headers."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.rows = []
        self.row = None
        self.cell = None
        self.depth = 0
        self.table_count = 0

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self.depth += 1
            self.table_count += 1
            if self.depth > 1 or self.table_count > 1:
                raise ValueError("Nested or multiple tables require manual separation")
        if not self.depth:
            return
        if tag == "tr":
            self._finish_row()
            self.row = []
        elif tag in {"td", "th"}:
            self._finish_cell()
            if self.row is None:
                raise ValueError("Cell outside a table row")
            attributes = dict(attrs)
            spans = [int(attributes.get(key) or 1) for key in ("rowspan", "colspan")]
            if any(not 1 <= value <= 512 for value in spans):
                raise ValueError("Invalid or oversized cell span")
            self.cell = {"parts": [], "is_header": tag == "th", "row_span": spans[0], "col_span": spans[1]}
        elif tag in {"br", "p", "div"} and self.cell is not None:
            self.cell["parts"].append(" ")

    def handle_endtag(self, tag):
        if tag in {"td", "th"}:
            self._finish_cell()
        elif tag == "tr":
            self._finish_row()
        elif tag == "table":
            self._finish_row()
            self.depth = max(0, self.depth - 1)

    def handle_data(self, data):
        if self.cell is not None:
            self.cell["parts"].append(data)

    def _finish_cell(self):
        if self.cell is not None:
            self.cell["text"] = " ".join("".join(self.cell.pop("parts")).split())
            self.row.append(self.cell)
            self.cell = None

    def _finish_row(self):
        self._finish_cell()
        if self.row is not None:
            self.rows.append(self.row)
            self.row = None


def parse_table_html(html: str) -> dict:
    if not html.strip():
        return {"status": "missing_html", "rows": 0, "columns": 0, "cells": []}
    parser = TableHTMLParser()
    try:
        parser.feed(html)
        parser.close()
        parser._finish_row()
        if not parser.rows:
            raise ValueError("No table rows")
        occupied = set()
        cells = []
        column_count = 0
        for row_index, row in enumerate(parser.rows):
            column = 0
            for item in row:
                while (row_index, column) in occupied:
                    column += 1
                bottom, right = row_index + item["row_span"], column + item["col_span"]
                if bottom > len(parser.rows) or right > 512:
                    raise ValueError("Cell span extends outside the parsed table")
                coverage = {(r, c) for r in range(row_index, bottom) for c in range(column, right)}
                if occupied & coverage or len(occupied | coverage) > 100_000:
                    raise ValueError("Overlapping or oversized table grid")
                occupied.update(coverage)
                text = item["text"]
                # Only an unambiguous scalar is numeric. Percentages/units stay verbatim.
                numeric = float(text) if re.fullmatch(r"[+-]?(?:\d+(?:\.\d+)?|\.\d+)", text) else None
                if numeric is not None and not math.isfinite(numeric):
                    numeric = None
                cells.append({**item, "row_index": row_index, "column_index": column, "numeric_value": numeric})
                column_count = max(column_count, right)
                column = right
        if not cells:
            raise ValueError("No cells")
        status = "ok" if len(occupied) == len(parser.rows) * column_count else "ragged"
        return {"status": status, "rows": len(parser.rows), "columns": column_count, "cells": cells}
    except (ValueError, TypeError) as exc:
        return {"status": "invalid_html", "rows": 0, "columns": 0, "cells": [], "warning": str(exc)}


class TableStore:
    def __init__(self, path: Path):
        self.path = path.resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS table_sources (
                    source TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, status TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS research_tables (
                    table_id TEXT PRIMARY KEY, source TEXT NOT NULL, page INTEGER,
                    block_index INTEGER NOT NULL, source_hash TEXT NOT NULL,
                    caption TEXT NOT NULL, footnote TEXT NOT NULL, bbox_json TEXT,
                    image_path TEXT, original_html TEXT NOT NULL,
                    row_count INTEGER NOT NULL, column_count INTEGER NOT NULL,
                    parse_status TEXT NOT NULL, warning TEXT
                );
                CREATE TABLE IF NOT EXISTS table_cells (
                    table_id TEXT NOT NULL REFERENCES research_tables(table_id) ON DELETE CASCADE,
                    row_index INTEGER NOT NULL, column_index INTEGER NOT NULL,
                    row_span INTEGER NOT NULL, col_span INTEGER NOT NULL,
                    is_header INTEGER NOT NULL, text TEXT NOT NULL, numeric_value REAL,
                    PRIMARY KEY(table_id, row_index, column_index)
                );
                CREATE INDEX IF NOT EXISTS table_source_page ON research_tables(source,page);
            """)

    @contextmanager
    def connect(self):
        con = sqlite3.connect(self.path, timeout=20)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA foreign_keys=ON")
        try:
            with con:
                yield con
        finally:
            con.close()

    @staticmethod
    def _text(value):
        return " ".join(str(x) for x in value) if isinstance(value, list) else str(value or "")

    def sync(self, library: Path, extractor: MinerUExtractor | None = None) -> dict:
        extractor = extractor or MinerUExtractor()
        present = set()
        indexed = skipped = 0
        for path in sorted(p for p in library.rglob("*") if p.is_file() and p.suffix.lower() == ".pdf"):
            source = str(path.relative_to(library))
            present.add(source)
            cache = extractor.cache_path(path)
            content_path = extractor._find_content_list(cache) if extractor.has_cache(path) else None
            raw = content_path.read_bytes() if content_path else b""
            fingerprint = hashlib.sha256(TABLE_SCHEMA.encode() + cache.name.encode() + raw).hexdigest()
            with self.connect() as con:
                previous = con.execute("SELECT fingerprint FROM table_sources WHERE source=?", (source,)).fetchone()
            if previous and previous[0] == fingerprint:
                skipped += 1
                continue
            blocks = json.loads(raw) if raw else []
            if not isinstance(blocks, list):
                raise ValueError("MinerU table cache must be a list")
            records = []
            for ordinal, block in enumerate(blocks):
                if not isinstance(block, dict) or block.get("type") != "table":
                    continue
                html = str(block.get("table_body") or "")
                parsed = parse_table_html(html)
                table_id = str(uuid.uuid5(NAMESPACE, f"{source}:{fingerprint}:{ordinal}"))
                image_path = None
                if block.get("img_path"):
                    candidate = (content_path.parent / block["img_path"]).resolve()
                    if candidate.is_relative_to(extractor.cache_dir) and candidate.is_file():
                        image_path = str(candidate)
                records.append((table_id, block, ordinal, html, parsed, image_path))
            # Publish a complete source snapshot, including an explicit no-cache state.
            with self.connect() as con:
                con.execute("DELETE FROM research_tables WHERE source=?", (source,))
                for table_id, block, ordinal, html, parsed, image_path in records:
                    page_index = block.get("page_idx")
                    con.execute("INSERT INTO research_tables VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                        table_id, source, page_index + 1 if isinstance(page_index, int) else None,
                        ordinal, fingerprint, self._text(block.get("table_caption")),
                        self._text(block.get("table_footnote")), json.dumps(block.get("bbox")),
                        image_path, html, parsed["rows"], parsed["columns"], parsed["status"], parsed.get("warning")))
                    con.executemany("INSERT INTO table_cells VALUES(?,?,?,?,?,?,?,?)", [
                        (table_id, c["row_index"], c["column_index"], c["row_span"], c["col_span"],
                         int(c["is_header"]), c["text"], c["numeric_value"]) for c in parsed["cells"]])
                con.execute("INSERT OR REPLACE INTO table_sources VALUES(?,?,?)",
                            (source, fingerprint, "parsed" if raw else "not_parsed"))
                indexed += len(records)
        with self.connect() as con:
            missing = {r[0] for r in con.execute("SELECT source FROM table_sources")} - present
            for source in missing:
                con.execute("DELETE FROM research_tables WHERE source=?", (source,))
                con.execute("DELETE FROM table_sources WHERE source=?", (source,))
        return {"indexed_tables": indexed, "skipped_sources": skipped, **self.stats()}

    def stats(self):
        with self.connect() as con:
            return {"tables": con.execute("SELECT count(*) FROM research_tables").fetchone()[0],
                    "cells": con.execute("SELECT count(*) FROM table_cells").fetchone()[0],
                    "parse_status": dict(con.execute("SELECT parse_status,count(*) FROM research_tables GROUP BY parse_status")),
                    "sources": [dict(r) for r in con.execute("SELECT source,status FROM table_sources ORDER BY source")]}

    def search(self, query="", source=None, limit=30, offset=0):
        aliases = {"心衰": "heart failure", "分期": "stages", "剂量": "doses", "药物": "drugs",
                   "分类": "classification", "痴呆": "dementia", "消融": "ablation", "性能": "performance"}
        expanded = query + " " + " ".join(v for k, v in aliases.items() if k in query)
        terms = list(dict.fromkeys(re.findall(r"\w{2,}", expanded.lower())))[:12]
        conditions, params = [], []
        if source:
            conditions.append("t.source=?")
            params.append(source)
        if terms:
            matches = []
            for term in terms:
                matches.append("(lower(t.caption) LIKE ? ESCAPE '\\' OR EXISTS (SELECT 1 FROM table_cells c WHERE c.table_id=t.table_id AND lower(c.text) LIKE ? ESCAPE '\\'))")
                pattern = "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
                params.extend([pattern, pattern])
            conditions.append("(" + " OR ".join(matches) + ")")
        where = " WHERE " + " AND ".join(conditions) if conditions else ""
        with self.connect() as con:
            rows = con.execute("SELECT t.table_id,t.source,t.page,t.caption,t.row_count,t.column_count,t.parse_status FROM research_tables t"
                               + where + " ORDER BY t.source,t.page,t.block_index LIMIT ? OFFSET ?",
                               [*params, min(100, max(1, limit)), max(0, int(offset))]).fetchall()
        return [dict(r) for r in rows]

    def get(self, table_id):
        with self.connect() as con:
            row = con.execute("SELECT * FROM research_tables WHERE table_id=?", (table_id,)).fetchone()
            if not row:
                return None
            result = dict(row)
            result.pop("original_html")  # Browser renders cells as text, never untrusted HTML.
            result["bbox"] = json.loads(result.pop("bbox_json"))
            result["cells"] = [dict(r) for r in con.execute("SELECT * FROM table_cells WHERE table_id=? ORDER BY row_index,column_index", (table_id,))]
            return result

    def query(self, sql: str, *, limit=200):
        limit = min(200, max(1, int(limit)))
        if len(sql) > 10000 or not re.match(r"\s*(SELECT|WITH)\b", sql, re.IGNORECASE):
            raise ValueError("Only a bounded SELECT/WITH query is allowed")
        con = sqlite3.connect(self.path.as_uri() + "?mode=ro", uri=True, timeout=2)
        started = time.monotonic()
        functions = {"count", "sum", "avg", "min", "max", "total", "round", "abs", "coalesce", "nullif",
                     "lower", "upper", "length", "substr", "instr", "replace", "trim", "typeof", "like"}

        def authorize(action, arg1, arg2, _database, _trigger):
            if action == sqlite3.SQLITE_SELECT:
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_READ and arg1 in {"research_tables", "table_cells"}:
                return sqlite3.SQLITE_OK
            if action == sqlite3.SQLITE_FUNCTION and (arg2 or "").lower() in functions:
                return sqlite3.SQLITE_OK
            return sqlite3.SQLITE_DENY

        try:
            con.execute("PRAGMA query_only=ON")
            if hasattr(con, "setlimit"):
                con.setlimit(sqlite3.SQLITE_LIMIT_LENGTH, 1_000_000)
            con.set_authorizer(authorize)
            con.set_progress_handler(lambda: int(time.monotonic() - started > 2), 1000)
            cursor = con.execute(sql)
            if cursor.description is None:
                raise ValueError("Query must return rows")
            rows = cursor.fetchmany(min(200, max(1, limit)) + 1)
            return {"columns": [c[0] for c in cursor.description],
                    "rows": [[v[:4000] if isinstance(v, str) else v for v in r] for r in rows[:limit]],
                    "truncated": len(rows) > limit}
        # Python/SQLite version combinations disagree on whether multiple
        # statements passed to execute() raise Error or Warning.  Keep the
        # public API stable and reject both through the same read-only boundary.
        except (sqlite3.Error, sqlite3.Warning) as exc:
            raise ValueError(f"Read-only SQL rejected or failed: {exc}") from exc
        finally:
            con.close()
