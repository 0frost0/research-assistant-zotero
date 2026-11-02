"""Atomic collection snapshots, immutable PDF objects, and versioned notes."""
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import threading
import uuid

from research_assistant.memory.store import Conflict


def now():
    return datetime.now(timezone.utc).isoformat()


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value):
    return hashlib.sha256(value).hexdigest()


def text(value, name, limit=2000, empty=False):
    if not isinstance(value, str) or len(value) > limit or (not empty and not value.strip()):
        raise ValueError(f"{name}格式或长度不正确。")
    return value.strip()


def compact_text(value):
    """Normalize PDF line wrapping for source-quote containment checks."""
    return re.sub(r"\s+", "", str(value)).casefold()


def key(value):
    value = text(value, "Zotero 标识", 128)
    if not re.fullmatch(r"[A-Za-z0-9_.:-]+", value):
        raise ValueError("Zotero 标识含有不支持的字符。")
    return value


def identity(*parts):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, encoded(parts)))


class KnowledgeStore:
    def __init__(self, root):
        self.root = Path(root)
        self.home = self.root / ".data/zotero"
        self.objects = self.home / "objects"
        self.objects.mkdir(parents=True, exist_ok=True)
        self.path = self.home / "knowledge.sqlite3"
        self.lock = threading.RLock()
        with self.transaction() as con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS kb_schema(version INTEGER PRIMARY KEY);
                INSERT OR IGNORE INTO kb_schema VALUES(1);
                CREATE TABLE IF NOT EXISTS knowledge_bases(
                    id TEXT PRIMARY KEY, connection TEXT NOT NULL, library TEXT NOT NULL,
                    collection TEXT NOT NULL, name TEXT NOT NULL, subtree INTEGER NOT NULL,
                    revision INTEGER NOT NULL, archived INTEGER NOT NULL DEFAULT 0,
                    snapshot_hash TEXT, updated_at TEXT NOT NULL,
                    UNIQUE(connection,library,collection));
                CREATE TABLE IF NOT EXISTS objects(
                    hash TEXT PRIMARY KEY, pages TEXT NOT NULL, report TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS entities(
                    id TEXT PRIMARY KEY, connection TEXT NOT NULL, library TEXT NOT NULL,
                    item_key TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT NOT NULL,
                    fingerprint TEXT NOT NULL, revision INTEGER NOT NULL,
                    UNIQUE(connection,library,item_key));
                CREATE TABLE IF NOT EXISTS entity_revisions(
                    id TEXT NOT NULL, revision INTEGER NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(id,revision));
                CREATE TABLE IF NOT EXISTS memberships(
                    kb_id TEXT NOT NULL REFERENCES knowledge_bases(id),
                    entity_id TEXT NOT NULL REFERENCES entities(id),
                    PRIMARY KEY(kb_id,entity_id));
                CREATE TABLE IF NOT EXISTS local_notes(
                    id TEXT PRIMARY KEY, kb_id TEXT NOT NULL REFERENCES knowledge_bases(id),
                    payload TEXT NOT NULL, revision INTEGER NOT NULL);
                CREATE TABLE IF NOT EXISTS note_revisions(
                    id TEXT NOT NULL, revision INTEGER NOT NULL, payload TEXT NOT NULL,
                    PRIMARY KEY(id,revision));
            """)
            if con.execute("SELECT MAX(version) FROM kb_schema").fetchone()[0] != 1:
                raise ValueError("知识库版本不兼容。")

    @contextmanager
    def transaction(self):
        with self.lock:
            con = sqlite3.connect(self.path, timeout=15)
            con.row_factory = sqlite3.Row
            con.execute("PRAGMA foreign_keys=ON")
            try:
                con.execute("BEGIN IMMEDIATE")
                yield con
                con.commit()
            except BaseException:
                con.rollback()
                raise
            finally:
                con.close()

    @contextmanager
    def read(self):
        con = sqlite3.connect(self.path, timeout=15)
        con.row_factory = sqlite3.Row
        try:
            con.execute("PRAGMA foreign_keys=ON")
            yield con
        finally:
            con.close()

    def bases(self):
        with self.transaction() as con:
            return [dict(row) for row in con.execute("""
                SELECT k.*, COUNT(m.entity_id) AS member_count
                FROM knowledge_bases k LEFT JOIN memberships m ON m.kb_id=k.id
                GROUP BY k.id ORDER BY k.name,k.id
            """)]

    def stats(self):
        """Return content-free operational counts for the authenticated status API."""
        with self.read() as con:
            bases = con.execute("SELECT COUNT(*), COALESCE(SUM(archived=0),0) FROM knowledge_bases").fetchone()
            entities = con.execute(
                "SELECT COUNT(*), COALESCE(SUM(kind='document'),0), "
                "COALESCE(SUM(kind IN ('note','annotation')),0) FROM entities"
            ).fetchone()
            note_payloads = [row[0] for row in con.execute("SELECT payload FROM local_notes")]
            objects = con.execute("SELECT COUNT(*) FROM objects").fetchone()[0]
        active_notes = 0
        for payload in note_payloads:
            try:
                value = json.loads(payload)
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            active_notes += int(value.get("confirmed") is True and value.get("archived") is not True)
        return {
            "knowledge_bases": int(bases[0]),
            "active_knowledge_bases": int(bases[1]),
            "entities": int(entities[0]),
            "documents": int(entities[1]),
            "external_records": int(entities[2]),
            "local_notes": len(note_payloads),
            "active_confirmed_notes": active_notes,
            "pdf_objects": int(objects),
        }

    def base(self, ident, con=None):
        if con is None:
            with self.transaction() as conn:
                return self.base(ident, conn)
        row = con.execute("SELECT * FROM knowledge_bases WHERE id=?", (ident,)).fetchone()
        if not row:
            raise KeyError(ident)
        return dict(row)

    def create(self, data):
        connection, library, collection = (key(data.get(k)) for k in ("connection", "library", "collection"))
        name = text(data.get("name"), "分类名称", 500)
        subtree = data.get("include_children", True)
        if type(subtree) is not bool:
            raise ValueError("包含子分类须为布尔值。")
        ident = identity(connection, library, collection)
        with self.transaction() as con:
            con.execute("INSERT OR IGNORE INTO knowledge_bases VALUES(?,?,?,?,?,?,0,0,NULL,?)",
                        (ident, connection, library, collection, name, int(subtree), now()))
            return self.base(ident, con)

    def object_exists(self, sha):
        if not isinstance(sha, str) or not re.fullmatch("[0-9a-f]{64}", sha):
            raise ValueError("文件摘要无效。")
        with self.transaction() as con:
            exists = con.execute("SELECT 1 FROM objects WHERE hash=?", (sha,)).fetchone()
        path = self.objects / (sha + ".pdf")
        return bool(exists and path.is_file() and digest(path.read_bytes()) == sha)

    def _annotate_note_payload(self, payload, con):
        """Add provenance verification to old notes without rewriting history.

        Notes created before ``verification_status`` was introduced remain
        readable.  Recomputing the status against immutable PDF objects at
        read time avoids presenting them as unverified merely because they
        have not been edited since the field was added.
        """
        result = dict(payload)
        anchors = []
        for raw in payload.get("anchors", []) or []:
            if not isinstance(raw, dict):
                continue
            anchor = dict(raw)
            verified = anchor.get("verification_status") == "verified"
            if anchor.get("page") is not None and str(anchor.get("source", "")).endswith(".pdf"):
                row = con.execute(
                    "SELECT pages FROM objects WHERE hash=?",
                    (str(anchor.get("source"))[:-4],),
                ).fetchone()
                if row:
                    pages = json.loads(row[0])
                    page = next((item for item in pages if item.get("page") == anchor.get("page")), None)
                    verified = bool(page and compact_text(anchor.get("quote", "")) in compact_text(page.get("text", "")))
            anchor["verification_status"] = "verified" if verified else "unverified"
            anchors.append(anchor)
        result["anchors"] = anchors
        result["source_verified"] = bool(anchors) and all(
            anchor["verification_status"] == "verified" for anchor in anchors
        )
        return result

    def put_object(self, sha, content):
        if len(content) > 25 * 1024 * 1024 or not content.startswith(b"%PDF-"):
            raise ValueError("只接受不超过 25 MB 的 PDF。")
        if digest(content) != sha:
            raise ValueError("附件摘要不匹配。")
        with self.lock:
            if self.object_exists(sha):
                return {"hash": sha, "cached": True}
            path = self.objects / (sha + ".pdf")
            temporary = self.objects / (uuid.uuid4().hex + ".part")
            try:
                temporary.write_bytes(content)
                from research_assistant.ingestion.pdf_extractor import PDFExtractor
                # No OCR process, model download, generation or full-PDF translation.
                documents, report = PDFExtractor(enable_ocr=False).extract(temporary, source=sha + ".pdf")
                pages = [{"text": doc.page_content, "page": doc.metadata.get("page")} for doc in documents]
                temporary.replace(path)
                with self.transaction() as con:
                    con.execute("INSERT OR REPLACE INTO objects VALUES(?,?,?)",
                                (sha, encoded(pages), encoded(report.model_dump())))
            finally:
                temporary.unlink(missing_ok=True)
        return {"hash": sha, "cached": False, "report": report.model_dump()}

    def normalize_entity(self, value):
        if not isinstance(value, dict):
            raise ValueError("条目须为对象。")
        kind = value.get("kind")
        if kind not in {"document", "note", "annotation"}:
            raise ValueError("不支持的条目类型。")
        result = {
            "key": key(value.get("key")), "kind": kind,
            "title": text(value.get("title", ""), "条目标题", 1000, empty=True),
            "parent_key": key(value["parent_key"]) if value.get("parent_key") else None,
        }
        if kind == "document":
            sha = value.get("hash")
            if not self.object_exists(sha):
                raise ValueError("附件尚未成功上传；分类成员未更新。")
            result["hash"] = sha
        else:
            result["body"] = text(value.get("body", ""), "笔记内容", 100000, empty=True)
            result["comment"] = text(value.get("comment", ""), "批注评论", 20000, empty=True)
            result["confirmed"] = value.get("confirmed", True) is True
            # An imported note is not automatically certified as human-authored.
            result["author_source"] = "external_zotero"
            if kind == "annotation":
                result["attachment_key"] = key(value.get("attachment_key"))
                page = value.get("page")
                if type(page) is not int or not 1 <= page <= 100000:
                    raise ValueError("批注物理页码无效。")
                result["page"] = page
        return result

    def sync(self, ident, data):
        if data.get("complete") is not True:
            raise ValueError("仅完整快照可以替换成员，部分上传不能清空知识库。")
        raw = data.get("entities")
        if not isinstance(raw, list) or len(raw) > 5000:
            raise ValueError("单次同步最多 5000 个条目。")
        entities = [self.normalize_entity(item) for item in raw]
        if len({e["key"] for e in entities}) != len(entities):
            raise ValueError("快照包含重复条目标识。")
        name = text(data.get("name"), "分类名称", 500)
        subtree = data.get("include_children")
        if type(subtree) is not bool:
            raise ValueError("包含子分类须为布尔值。")
        fingerprint = digest(encoded({"name": name, "subtree": subtree,
                                      "entities": sorted(entities, key=lambda e: e["key"])}).encode())
        with self.transaction() as con:
            base = self.base(ident, con)
            if base["archived"]:
                raise Conflict("知识库已归档，请先恢复。")
            # Idempotent replay only if the supplied revision is still applicable.
            expected = data.get("expected_revision")
            if type(expected) is not int or expected != base["revision"]:
                if fingerprint == base["snapshot_hash"] and expected == base["revision"] - 1:
                    return {**base, "unchanged": True}
                raise Conflict("知识库已更新，请重新取得状态后同步。")
            changed = fingerprint != base["snapshot_hash"]
            ids, updated = [], set()
            for entity in entities:
                eid = identity(base["connection"], base["library"], entity["key"])
                ids.append(eid)
                payload = encoded(entity)
                fp = digest(payload.encode())
                old = con.execute("SELECT * FROM entities WHERE id=?", (eid,)).fetchone()
                if old and old["fingerprint"] == fp:
                    continue
                revision = old["revision"] + 1 if old else 1
                con.execute("INSERT OR REPLACE INTO entities VALUES(?,?,?,?,?,?,?,?)",
                            (eid, base["connection"], base["library"], entity["key"], entity["kind"], payload, fp, revision))
                con.execute("INSERT INTO entity_revisions VALUES(?,?,?)", (eid, revision, payload))
                updated.add(eid)
            changed = changed or bool(updated)
            if not changed:
                return {**base, "unchanged": True}
            # Shared content updates invalidate the revisions of all referencing bases.
            affected = {ident}
            for eid in updated:
                affected.update(row[0] for row in con.execute("SELECT kb_id FROM memberships WHERE entity_id=?", (eid,)))
            con.execute("DELETE FROM memberships WHERE kb_id=?", (ident,))
            con.executemany("INSERT INTO memberships VALUES(?,?)", [(ident, eid) for eid in ids])
            for kb in affected:
                con.execute("UPDATE knowledge_bases SET revision=revision+1,updated_at=? WHERE id=?", (now(), kb))
                if kb != ident:
                    con.execute("UPDATE knowledge_bases SET snapshot_hash=NULL WHERE id=?", (kb,))
            con.execute("UPDATE knowledge_bases SET name=?,subtree=?,snapshot_hash=? WHERE id=?",
                        (name, int(subtree), fingerprint, ident))
            return {**self.base(ident, con), "unchanged": False}

    def set_archive(self, ident, data):
        if type(data.get("archived")) is not bool:
            raise ValueError("归档状态无效。")
        with self.transaction() as con:
            base = self.base(ident, con)
            if data.get("expected_revision") != base["revision"]:
                raise Conflict("知识库版本已变化。")
            con.execute("UPDATE knowledge_bases SET archived=?,revision=revision+1,updated_at=? WHERE id=?",
                        (int(data["archived"]), now(), ident))
            return self.base(ident, con)

    def snapshot(self, ids):
        if not isinstance(ids, list) or not ids or len(ids) > 20:
            raise ValueError("请明确选择 1–20 个知识库。")
        with self.transaction() as con:
            bases = [self.base(ident, con) for ident in dict.fromkeys(ids)]
            if any(b["archived"] for b in bases):
                raise Conflict("已归档的知识库不参与检索。")
            entities, notes = {}, {}
            for base in bases:
                for row in con.execute("SELECT e.* FROM entities e JOIN memberships m ON e.id=m.entity_id WHERE m.kb_id=?", (base["id"],)):
                    value = json.loads(row["payload"])
                    if value["kind"] == "document":
                        report = json.loads(con.execute("SELECT report FROM objects WHERE hash=?", (value["hash"],)).fetchone()[0])
                        value["extraction"] = {k: report[k] for k in ("total_pages", "native_pages", "low_text_pages", "empty_pages")}
                    entities[row["id"]] = {**dict(row), "data": value}
                for row in con.execute("SELECT * FROM local_notes WHERE kb_id=?", (base["id"],)):
                    notes[row["id"]] = {**dict(row), "data": self._annotate_note_payload(json.loads(row["payload"]), con)}
            return {"bases": bases, "entities": list(entities.values()), "notes": list(notes.values())}

    def assert_current(self, snapshot):
        with self.transaction() as con:
            if any(self.base(b["id"], con)["revision"] != b["revision"] for b in snapshot["bases"]):
                raise Conflict("检索期间资料已更新，请重新检索。")

    def save_note(self, kb_id, data, ident=None):
        body = text(data.get("body"), "笔记正文", 100000)
        title = text(data.get("title", ""), "笔记标题", 500, empty=True)
        confirmed = data.get("confirmed", True)
        if type(confirmed) is not bool:
            raise ValueError("确认状态无效。")
        old_payload = {}
        if ident:
            with self.read() as con:
                old_row = con.execute("SELECT payload FROM local_notes WHERE id=? AND kb_id=?", (ident, kb_id)).fetchone()
            if old_row:
                old_payload = json.loads(old_row[0])
        kind = data.get("type", old_payload.get("type", "understanding"))
        if kind not in {"understanding", "question", "idea"}:
            raise ValueError("笔记类型只能是理解、疑问或想法。")
        author_source = data.get("author_source", old_payload.get("author_source", "user"))
        if author_source not in {"user", "ai"}:
            raise ValueError("笔记来源只能是 user 或 ai。")
        if old_payload.get("author_source") == "ai" and author_source != "ai":
            raise ValueError("AI 草稿的来源不可改写为用户手写记录。")
        # Editing a note should not silently restore an archived record.  The
        # archive action sends an explicit boolean; ordinary edits preserve the
        # current state so a stale or partial client payload cannot re-enable
        # an old memory in retrieval.
        if "archived" in data and type(data["archived"]) is not bool:
            raise ValueError("归档状态无效。")
        archived = data["archived"] if "archived" in data else bool(old_payload.get("archived", False))
        anchors = data.get("anchors", old_payload.get("anchors", []))
        if not isinstance(anchors, list) or len(anchors) > 12:
            raise ValueError("笔记最多关联 12 个来源。")
        clean_anchors = []
        for anchor in anchors:
            if not isinstance(anchor, dict):
                raise ValueError("来源锚点格式错误。")
            source = anchor.get("source")
            quote = anchor.get("quote", "")
            page = anchor.get("page")
            if (not isinstance(source, str) or not source.strip() or len(source) > 500
                    or not isinstance(quote, str) or not 1 <= len(quote.strip()) <= 12000):
                raise ValueError("来源锚点必须包含来源标识和原文摘录。")
            if page is not None and (type(page) is not int or not 1 <= page <= 100000):
                raise ValueError("来源页码无效。")
            url = anchor.get("url")
            if url is not None and (not isinstance(url, str) or len(url) > 2000
                                    or not (url.startswith("zotero://") or url.startswith("/"))):
                raise ValueError("来源链接只允许 Zotero 或本站路径。")
            clean_anchors.append({"source": source.strip(), "page": page,
                                  "quote": quote.strip(), "url": url,
                                  "kind": str(anchor.get("kind", "original"))[:40]})
        with self.transaction() as con:
            base = self.base(kb_id, con)
            if base["archived"]:
                raise Conflict("知识库已归档。")
            allowed_sources = set()
            for row in con.execute("SELECT payload FROM entities e JOIN memberships m ON e.id=m.entity_id WHERE m.kb_id=?", (kb_id,)):
                entity = json.loads(row[0])
                if entity.get("kind") == "document" and entity.get("hash"):
                    allowed_sources.add(entity["hash"] + ".pdf")
            for anchor in clean_anchors:
                if anchor["source"] not in allowed_sources:
                    raise ValueError("来源论文不属于当前知识库，未保存笔记。")
            # User notes may contain a paraphrase or a Zotero annotation whose
            # text differs from the extracted PDF. Keep the note, but record
            # whether the quoted text can be found on the declared page. AI
            # drafts below remain strict and reject unverified quotes.
            for anchor in clean_anchors:
                verified = False
                if anchor["page"] is not None and anchor["source"].endswith(".pdf"):
                    row = con.execute(
                        "SELECT pages FROM objects WHERE hash=?",
                        (anchor["source"][:-4],),
                    ).fetchone()
                    if row:
                        pages = json.loads(row[0])
                        page = next((item for item in pages if item.get("page") == anchor["page"]), None)
                        verified = bool(page and compact_text(anchor["quote"]) in compact_text(page.get("text", "")))
                anchor["verification_status"] = "verified" if verified else "unverified"
            if author_source == "ai":
                if not clean_anchors:
                    raise ValueError("AI 草稿必须关联原文来源。")
                objects = {
                    row["hash"]: json.loads(row["pages"])
                    for row in con.execute("SELECT hash,pages FROM objects")
                }
                for anchor in clean_anchors:
                    if anchor["page"] is None:
                        raise ValueError("AI 草稿来源必须包含原文页码。")
                    pages = objects.get(anchor["source"][:-4], [])
                    page = next((item for item in pages if item.get("page") == anchor["page"]), None)
                    if not page or compact_text(anchor["quote"]) not in compact_text(page.get("text", "")):
                        raise ValueError("AI 草稿摘录不在指定原文页面中，未保存。")
            old = con.execute("SELECT * FROM local_notes WHERE id=? AND kb_id=?", (ident, kb_id)).fetchone() if ident else None
            if ident and not old:
                raise KeyError(ident)
            if old and data.get("expected_revision") != old["revision"]:
                raise Conflict("笔记已有更新，未覆盖你的内容。")
            ident = ident or str(uuid.uuid4())
            revision = old["revision"] + 1 if old else 1
            # AI may create only an unconfirmed draft.  A later user edit can
            # confirm it, while retaining the original AI provenance.
            if author_source == "ai" and confirmed and old_payload.get("author_source") != "ai":
                raise ValueError("AI 来源笔记必须先以未确认草稿保存。")
            payload = {"title": title, "body": body, "type": kind, "confirmed": confirmed,
                       "archived": archived, "author_source": "user",
                       "confirmed_by": "user" if confirmed else None,
                       "anchors": clean_anchors,
                       "source_status": "linked" if clean_anchors else "unlinked",
                       "source_verified": bool(clean_anchors) and all(
                           anchor["verification_status"] == "verified" for anchor in clean_anchors
                       ),
                       "updated_at": now()}
            if author_source == "ai" or old_payload.get("author_source") == "ai":
                payload["author_source"] = "ai"
                payload["confirmed_by"] = "user" if confirmed else None
            con.execute("INSERT OR REPLACE INTO local_notes VALUES(?,?,?,?)", (ident, kb_id, encoded(payload), revision))
            con.execute("INSERT INTO note_revisions VALUES(?,?,?)", (ident, revision, encoded(payload)))
            con.execute("UPDATE knowledge_bases SET revision=revision+1,updated_at=? WHERE id=?", (now(), kb_id))
            return {"id": ident, "kb_id": kb_id, "revision": revision, **payload}

    def note_history(self, ident):
        with self.read() as con:
            current = con.execute("SELECT kb_id FROM local_notes WHERE id=?", (ident,)).fetchone()
            revisions = con.execute(
                "SELECT revision, payload FROM note_revisions WHERE id=? ORDER BY revision DESC",
                (ident,),
            ).fetchall()
            if not current or not revisions:
                raise KeyError(ident)
            result = []
            seen = set()
            for row in revisions:
                if row[0] in seen:
                    continue
                seen.add(row[0])
                value = self._annotate_note_payload(json.loads(row[1]), con)
                result.append({"id": ident, "kb_id": current[0], "revision": row[0], **value})
            return result

    def corpus_signature(self):
        """Cheap change probe: reads only identifiers and revisions, never page texts."""
        with self.transaction() as con:
            entities = con.execute("""
                SELECT DISTINCT e.id AS id, e.revision AS revision
                FROM entities e JOIN memberships m ON e.id=m.entity_id ORDER BY e.id
            """).fetchall()
            notes = con.execute("SELECT id, revision FROM local_notes ORDER BY id").fetchall()
            parsed = [(r["hash"], digest(r["report"].encode())) for r in
                      con.execute("SELECT hash,report FROM objects ORDER BY hash")]
        return digest(encoded([(row["id"], row["revision"]) for row in entities] +
                              [(row["id"], row["revision"]) for row in notes] + parsed).encode())

    def record_qa_run(self, result):
        trace = result["trace"]
        safe = {k: v for k, v in trace.items() if k != "steps"}
        # Persist a useful operational projection while keeping the question,
        # page text and evidence out of the durable telemetry table.
        from research_assistant.core.agent_telemetry import summarize_qa_trace
        safe["qa_observability"] = summarize_qa_trace(trace)
        safe.update(status=result["status"], claims=len(result["claims"]), evidence=len(result["evidence"]))
        with self.transaction() as con:
            con.execute("CREATE TABLE IF NOT EXISTS qa_runs(id TEXT PRIMARY KEY, created_at TEXT NOT NULL, metrics TEXT NOT NULL)")
            con.execute("INSERT INTO qa_runs VALUES(?,?,?)", (trace["run_id"], now(), encoded(safe)))

    def corpus(self):
        with self.transaction() as con:
            documents = {}
            for row in con.execute("SELECT * FROM objects"):
                blocks = json.loads(row["report"]).get("mineru", {}).get("blocks")
                documents[row["hash"]] = ([{"text": b["text"], "page": b["metadata"].get("page"),
                                            "metadata": b["metadata"]} for b in blocks]
                                           if blocks else json.loads(row["pages"]))
            entities = [dict(r) for r in con.execute("SELECT DISTINCT e.* FROM entities e JOIN memberships m ON e.id=m.entity_id")]
            notes = []
            for row in con.execute("SELECT * FROM local_notes"):
                item = dict(row)
                item["payload"] = encoded(self._annotate_note_payload(json.loads(row["payload"]), con))
                notes.append(item)
        return documents, entities, notes
