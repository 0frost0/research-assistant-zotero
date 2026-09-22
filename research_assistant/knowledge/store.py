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

    def bases(self):
        with self.transaction() as con:
            return [dict(row) for row in con.execute("""
                SELECT k.*, COUNT(m.entity_id) AS member_count
                FROM knowledge_bases k LEFT JOIN memberships m ON m.kb_id=k.id
                GROUP BY k.id ORDER BY k.name,k.id
            """)]

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
                    notes[row["id"]] = {**dict(row), "data": json.loads(row["payload"])}
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
        with self.transaction() as con:
            base = self.base(kb_id, con)
            if base["archived"]:
                raise Conflict("知识库已归档。")
            old = con.execute("SELECT * FROM local_notes WHERE id=? AND kb_id=?", (ident, kb_id)).fetchone() if ident else None
            if ident and not old:
                raise KeyError(ident)
            if old and data.get("expected_revision") != old["revision"]:
                raise Conflict("笔记已有更新，未覆盖你的内容。")
            ident = ident or str(uuid.uuid4())
            revision = old["revision"] + 1 if old else 1
            payload = {"title": title, "body": body, "confirmed": confirmed,
                       "archived": data.get("archived") is True, "author_source": "user",
                       "updated_at": now()}
            con.execute("INSERT OR REPLACE INTO local_notes VALUES(?,?,?,?)", (ident, kb_id, encoded(payload), revision))
            con.execute("INSERT INTO note_revisions VALUES(?,?,?)", (ident, revision, encoded(payload)))
            con.execute("UPDATE knowledge_bases SET revision=revision+1,updated_at=? WHERE id=?", (now(), kb_id))
            return {"id": ident, "kb_id": kb_id, "revision": revision, **payload}

    def corpus(self):
        with self.transaction() as con:
            documents = {r["hash"]: json.loads(r["pages"]) for r in con.execute("SELECT * FROM objects")}
            entities = [dict(r) for r in con.execute("SELECT DISTINCT e.* FROM entities e JOIN memberships m ON e.id=m.entity_id")]
            notes = [dict(r) for r in con.execute("SELECT * FROM local_notes")]
        return documents, entities, notes
