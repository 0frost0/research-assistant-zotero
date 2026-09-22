"""Immutable PDF artifacts and append-only reading revisions. SQLite is authoritative."""

from __future__ import annotations
import hashlib
import json
import math
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from pypdf import PdfReader


def now():
    return datetime.now(timezone.utc).isoformat()


def dump(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)


def digest(value):
    return hashlib.sha256(value).hexdigest()


class Conflict(ValueError):
    pass


def inspect_pdf(path):
    pdf = PdfReader(path)
    if pdf.is_encrypted or not pdf.pages:
        raise ValueError("暂不支持加密或没有页面的 PDF。")
    pages = []
    for page in pdf.pages:
        media, crop = page.mediabox, page.cropbox
        box = [
            max(float(media.left), float(crop.left)),
            max(float(media.bottom), float(crop.bottom)),
            min(float(media.right), float(crop.right)),
            min(float(media.top), float(crop.top)),
        ]
        if box[2] <= box[0] or box[3] <= box[1]:
            raise ValueError("页面裁剪范围无效。")
        content = page.extract_text() or ""
        pages.append(
            {
                "view_box": box,
                "rotation": int(page.get("/Rotate", 0)) % 360,
                "user_unit": float(page.get("/UserUnit", 1)),
                "text_chars": len(content.strip()),
                "latin": len(re.findall(r"[A-Za-z]", content)),
                "cjk": len(re.findall(r"[\u4e00-\u9fff]", content)),
            }
        )
    en, zh = sum(p["latin"] for p in pages), sum(p["cjk"] for p in pages)
    language = (
        "unknown"
        if en + zh < 80
        else "zh"
        if zh > en * 0.7
        else "en"
        if en > zh * 20
        else "mixed"
    )
    low = [i + 1 for i, p in enumerate(pages) if p["text_chars"] < 40]
    return {
        "pages": pages,
        "language": language,
        "page_count": len(pages),
        "text_layer": not low,
        "low_text_pages": low,
        "warning": "低文字页可能为扫描件或纯图页；第一版不自动翻译，原文仍可阅读。"
        if low
        else None,
    }


class ReadingStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.path = self.root / ".data/reading.sqlite3"
        self.blobs = self.root / ".data/reading_artifacts"
        self.blobs.mkdir(parents=True, exist_ok=True)
        self._cache = None
        with self.transaction() as con:
            if con.execute("PRAGMA user_version").fetchone()[0] > 1:
                raise ValueError("阅读数据库版本较新，请使用对应应用版本。")
            for statement in (
                "CREATE TABLE IF NOT EXISTS papers(id TEXT PRIMARY KEY, source TEXT NOT NULL, original_id TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS artifacts(id TEXT PRIMARY KEY, paper_id TEXT NOT NULL, kind TEXT NOT NULL, hash TEXT NOT NULL, parent_id TEXT, metadata TEXT NOT NULL, created_at TEXT NOT NULL)",
                "CREATE INDEX IF NOT EXISTS artifact_hash_idx ON artifacts(hash,kind)",
                "CREATE TABLE IF NOT EXISTS annotations(id TEXT PRIMARY KEY, paper_id TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS notes(id TEXT PRIMARY KEY, paper_id TEXT NOT NULL, current_revision INTEGER NOT NULL)",
                "CREATE TABLE IF NOT EXISTS note_revisions(note_id TEXT NOT NULL, revision INTEGER NOT NULL, previous_revision INTEGER, payload TEXT NOT NULL, created_at TEXT NOT NULL, PRIMARY KEY(note_id,revision))",
                "CREATE TABLE IF NOT EXISTS reading_settings(key TEXT PRIMARY KEY, value TEXT NOT NULL)",
                "CREATE TABLE IF NOT EXISTS translation_jobs(id TEXT PRIMARY KEY, cache_key TEXT NOT NULL UNIQUE, payload TEXT NOT NULL)",
                "CREATE TRIGGER IF NOT EXISTS revision_no_update BEFORE UPDATE ON note_revisions BEGIN SELECT RAISE(ABORT,'Immutable revision'); END",
                "CREATE TRIGGER IF NOT EXISTS revision_no_delete BEFORE DELETE ON note_revisions BEGIN SELECT RAISE(ABORT,'Immutable revision'); END",
            ):
                con.execute(statement)
            con.execute("PRAGMA user_version=1")

    @contextmanager
    def transaction(self):
        con = sqlite3.connect(self.path, timeout=15)
        con.row_factory = sqlite3.Row
        try:
            con.execute("PRAGMA foreign_keys=ON")
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
            yield con
        finally:
            con.close()

    def _save_blob(self, content):
        fingerprint = digest(content)
        path = self.blobs / (fingerprint + ".pdf")
        if not path.exists():
            try:
                with path.open("xb") as stream:
                    stream.write(content)
            except FileExistsError:
                pass
        if digest(path.read_bytes()) != fingerprint:
            raise ValueError("原件哈希校验失败或并发保存未完成，拒绝覆盖，请重试。")
        return fingerprint

    def register(self, path, source):
        content = Path(path).read_bytes()
        fingerprint = digest(content)
        with self.read() as con:
            row = con.execute(
                "SELECT paper_id FROM artifacts WHERE hash=? AND kind='original'",
                (fingerprint,),
            ).fetchone()
        if row:
            return self.paper(row[0])
        metadata = inspect_pdf(path)
        self._save_blob(content)
        paper_id, artifact_id = str(uuid.uuid4()), str(uuid.uuid4())
        with self.transaction() as con:
            row = con.execute(
                "SELECT paper_id FROM artifacts WHERE hash=? AND kind='original'",
                (fingerprint,),
            ).fetchone()
            if row:
                paper_id = row[0]
            else:
                con.execute(
                    "INSERT INTO papers VALUES(?,?,?,?)",
                    (paper_id, source, artifact_id, now()),
                )
                con.execute(
                    "INSERT INTO artifacts VALUES(?,?,?,?,?,?,?)",
                    (
                        artifact_id,
                        paper_id,
                        "original",
                        fingerprint,
                        None,
                        dump(metadata),
                        now(),
                    ),
                )
        return self.paper(paper_id)

    def artifact(self, artifact_id):
        with self.read() as con:
            row = con.execute(
                "SELECT * FROM artifacts WHERE id=?", (artifact_id,)
            ).fetchone()
        if row is None:
            raise KeyError("文档产物不存在。")
        result = dict(row)
        result["metadata"] = json.loads(result["metadata"])
        result["url"] = f"/api/reading/artifacts/{artifact_id}/file"
        return result

    def artifact_path(self, artifact_id):
        item = self.artifact(artifact_id)
        path = self.blobs / (item["hash"] + ".pdf")
        if not path.is_file() or digest(path.read_bytes()) != item["hash"]:
            raise ValueError("文档缺失或哈希已变化，不能保证定位，请从备份恢复。")
        return path

    def paper(self, paper_id):
        with self.read() as con:
            row = con.execute("SELECT * FROM papers WHERE id=?", (paper_id,)).fetchone()
            ids = [
                r[0]
                for r in con.execute(
                    "SELECT id FROM artifacts WHERE paper_id=? ORDER BY created_at",
                    (paper_id,),
                )
            ]
        if row is None:
            raise KeyError("论文不存在。")
        return {**dict(row), "artifacts": [self.artifact(i) for i in ids]}

    def papers(self):
        with self.read() as con:
            ids = [
                r[0]
                for r in con.execute("SELECT id FROM papers ORDER BY created_at DESC")
            ]
        return [self.paper(i) for i in ids]

    def add_translation(self, paper_id, original_id, path, provenance, validation):
        original = self.artifact(original_id)
        if original["paper_id"] != paper_id or original["kind"] != "original":
            raise ValueError("译文原件关联错误。")
        fingerprint = self._save_blob(Path(path).read_bytes())
        metadata = inspect_pdf(path)
        metadata.update(
            {
                "provenance": provenance,
                "validation": validation,
                "mapping_status": "unaligned",
                "mapping_method": "no_verified_paragraph_map",
            }
        )
        artifact_id = str(uuid.uuid4())
        with self.transaction() as con:
            con.execute(
                "INSERT INTO artifacts VALUES(?,?,?,?,?,?,?)",
                (
                    artifact_id,
                    paper_id,
                    "translation",
                    fingerprint,
                    original_id,
                    dump(metadata),
                    now(),
                ),
            )
        return self.artifact(artifact_id)

    def validate_anchor(self, anchor, paper_id):
        if not isinstance(anchor, dict):
            raise ValueError("来源锚点格式错误。")
        item = self.artifact(str(anchor.get("artifact_id", "")))
        if item["paper_id"] != paper_id or item["hash"] != anchor.get("artifact_hash"):
            raise ValueError("来源不属于本论文或文档版本不匹配。")
        page = anchor.get("page")
        if type(page) is not int or not 1 <= page <= item["metadata"]["page_count"]:
            raise ValueError("页码须为从 1 开始的 PDF 物理页码。")
        geometry = item["metadata"]["pages"][page - 1]
        if (
            anchor.get("coordinate_system") != "pdf_user_space"
            or anchor.get("view_box") != geometry["view_box"]
        ):
            raise ValueError("坐标系或页面裁剪版本不匹配，请重新选择。")
        rects, box = anchor.get("rects", []), geometry["view_box"]
        if not isinstance(rects, list) or len(rects) > 200:
            raise ValueError("选区过大，请分段保存。")
        for rect in rects:
            if (
                not isinstance(rect, list)
                or len(rect) != 4
                or any(
                    type(x) not in (int, float) or not math.isfinite(x) for x in rect
                )
                or not (
                    box[0] - 0.5 <= rect[0] < rect[2] <= box[2] + 0.5
                    and box[1] - 0.5 <= rect[1] < rect[3] <= box[3] + 0.5
                )
            ):
                raise ValueError("选区超出页面或坐标无效。")
        excerpt = str(anchor.get("excerpt", "")).strip()
        if not excerpt or len(excerpt) > 12000:
            raise ValueError("摘录须为 1–12000 字符。")
        return {
            "artifact_id": item["id"],
            "artifact_hash": item["hash"],
            "kind": item["kind"],
            "page": page,
            "coordinate_system": "pdf_user_space",
            "view_box": box,
            "rotation": geometry["rotation"],
            "user_unit": geometry["user_unit"],
            "rects": rects,
            "excerpt": excerpt,
            "evidence_id": anchor.get("evidence_id"),
            "evidence_id_status": "reference_only_not_verified"
            if anchor.get("evidence_id")
            else None,
            "mapping_status": ("exact" if rects else "page_only")
            if item["kind"] == "original"
            else "unaligned",
            "mapping_method": (
                "original_selection" if rects else "unverified_evidence_excerpt"
            )
            if item["kind"] == "original"
            else "translation_selection_only",
        }

    def create_annotation(self, data):
        paper_id = str(data.get("paper_id", ""))
        self.paper(paper_id)
        anchor = self.validate_anchor(data.get("anchor"), paper_id)
        if not anchor["rects"]:
            raise ValueError("划线必须包含选区；证据卡片摘录请保存为笔记锚点。")
        style = data.get("style", "highlight")
        if style not in {"highlight", "underline"}:
            raise ValueError("不支持的批注样式。")
        item = {
            "id": str(uuid.uuid4()),
            "paper_id": paper_id,
            "anchor": anchor,
            "style": style,
            "created_at": now(),
        }
        with self.transaction() as con:
            con.execute(
                "INSERT INTO annotations VALUES(?,?,?,?)",
                (item["id"], paper_id, dump(item), item["created_at"]),
            )
        return item

    def annotations(self, paper_id):
        with self.read() as con:
            return [
                json.loads(r[0])
                for r in con.execute(
                    "SELECT payload FROM annotations WHERE paper_id=?", (paper_id,)
                )
            ]

    def get(self, note_id, revision=None):
        with self.read() as con:
            if revision is None:
                row = con.execute(
                    "SELECT r.payload FROM notes n JOIN note_revisions r ON n.id=r.note_id AND n.current_revision=r.revision WHERE n.id=?",
                    (note_id,),
                ).fetchone()
            else:
                row = con.execute(
                    "SELECT payload FROM note_revisions WHERE note_id=? AND revision=?",
                    (note_id, revision),
                ).fetchone()
        if row is None:
            raise KeyError("笔记不存在。")
        return json.loads(row[0])

    def history(self, note_id):
        self.get(note_id)
        with self.read() as con:
            return [
                json.loads(r[0])
                for r in con.execute(
                    "SELECT payload FROM note_revisions WHERE note_id=? ORDER BY revision DESC",
                    (note_id,),
                )
            ]

    def notes(self, paper_id=None):
        with self.read() as con:
            rows = con.execute(
                "SELECT r.payload FROM notes n JOIN note_revisions r ON n.id=r.note_id AND n.current_revision=r.revision "
                + ("WHERE n.paper_id=? " if paper_id else "")
                + "ORDER BY r.created_at DESC",
                (paper_id,) if paper_id else (),
            ).fetchall()
        return [json.loads(r[0]) for r in rows]

    def validate_source_links(self, links, anchors, old=None, *, ai_draft=False):
        """User-attested pairs, stored inside immutable note revisions, never engine alignment."""
        if not isinstance(links, list) or len(links) > 12:
            raise ValueError("来源关联格式错误。")
        prior = {}
        if old:
            for link in old.get("source_links", []):
                pair = (dump(old["anchors"][link["translation_index"]]), dump(old["anchors"][link["original_index"]]))
                prior[pair] = link
        result, seen = [], set()
        for link in links:
            if not isinstance(link, dict):
                raise ValueError("来源关联格式错误。")
            ti, oi = link.get("translation_index"), link.get("original_index")
            if any(type(i) is not int or not 0 <= i < len(anchors) for i in (ti, oi)) or ti in seen:
                raise ValueError("来源关联索引错误或重复。")
            translated, original = anchors[ti], anchors[oi]
            if (translated["kind"] != "translation" or original["kind"] != "original"
                    or self.artifact(translated["artifact_id"])["parent_id"] != original["artifact_id"]
                    or not translated["rects"] or not original["rects"]):
                raise ValueError("须关联译文选区及其绑定原件的独立选区。")
            previous = prior.get((dump(translated), dump(original)))
            if not previous and (ai_draft or link.get("confirm") is not True):
                raise ValueError("新增或修改原译文关联须由用户明确确认。")
            result.append({"translation_index": ti, "original_index": oi,
                           "status": "user_confirmed", "method": "manual_selection",
                           "confirmed_at": previous["confirmed_at"] if previous else now(),
                           "confirmed_by": "user"})
            seen.add(ti)
        return result

    def save_note(self, data, note_id=None, *, ai_draft=False, generation_source=None):
        old = self.get(note_id) if note_id else None
        paper_id = old["paper_id"] if old else str(data.get("paper_id", ""))
        self.paper(paper_id)
        text = str(data.get("body", old["body"] if old else "")).strip()
        kind = data.get("type", old["type"] if old else "understanding")
        if (
            not text
            or len(text) > 12000
            or kind not in {"understanding", "question", "idea"}
        ):
            raise ValueError("笔记须为 1–12000 字符，类型为理解、疑问或想法。")
        raw_anchors = data.get("anchors", old["anchors"] if old else [])
        if not isinstance(raw_anchors, list) or len(raw_anchors) > 12:
            raise ValueError("笔记最多关联 12 个来源。")
        anchors = [self.validate_anchor(a, paper_id) for a in raw_anchors]
        annotation_ids = data.get(
            "annotation_ids", old["annotation_ids"] if old else []
        )
        known = {a["id"]: a for a in self.annotations(paper_id)}
        if not isinstance(annotation_ids, list) or any(
            i not in known for i in annotation_ids
        ):
            raise ValueError("批注不属于本论文。")
        for i in annotation_ids:
            if known[i]["anchor"] not in anchors:
                anchors.append(known[i]["anchor"])
        if len(anchors) > 12:
            raise ValueError("关联来源过多。")
        if not anchors:
            raise ValueError(
                "请先选择文字或从证据卡片带入来源，再保存笔记；划线也可以单独保存。"
            )
        source = old["author_source"] if old else "ai_assisted" if ai_draft else "user"
        links = self.validate_source_links(data.get("source_links", old.get("source_links", []) if old else []), anchors, old, ai_draft=ai_draft)
        confirmed = (
            False
            if ai_draft
            else (old["confirmed"] or data.get("confirm") is True)
            if old
            else True
        )
        archived = data.get("archived", old["archived"] if old else False)
        if type(archived) is not bool:
            raise ValueError("归档状态错误。")
        revision = old["revision"] + 1 if old else 1
        item = {
            "id": note_id or str(uuid.uuid4()),
            "paper_id": paper_id,
            "type": kind,
            "body": text,
            "author_source": source,
            "confirmed": confirmed,
            "archived": archived,
            "revision": revision,
            "previous_revision": old["revision"] if old else None,
            "created_at": old["created_at"] if old else now(),
            "modified_at": now(),
            "annotation_ids": annotation_ids,
            "anchors": anchors,
            "source_links": links,
            "source_kind": "user_note" if confirmed else "ai_draft",
            "generation_source": old.get("generation_source") if old else generation_source if ai_draft else None,
        }
        with self.transaction() as con:
            if old:
                expected = data.get("expected_revision")
                if type(expected) is not int or expected != old["revision"]:
                    raise Conflict(
                        "其他窗口已修改笔记；保留未保存内容，请对照最新版本。"
                    )
                if (
                    con.execute(
                        "UPDATE notes SET current_revision=? WHERE id=? AND current_revision=?",
                        (revision, note_id, expected),
                    ).rowcount
                    != 1
                ):
                    raise Conflict("笔记发生并发修改，请先查看最新版本。")
            else:
                con.execute(
                    "INSERT INTO notes VALUES(?,?,?)", (item["id"], paper_id, revision)
                )
            con.execute(
                "INSERT INTO note_revisions VALUES(?,?,?,?,?)",
                (
                    item["id"],
                    revision,
                    item["previous_revision"],
                    dump(item),
                    item["modified_at"],
                ),
            )
        try:
            self._invalidate_cache()
        except Exception:
            pass
        return item

    def _invalidate_cache(self):
        self._cache = None

    @staticmethod
    def terms(text):
        text = text.lower()
        result = set(re.findall(r"[a-z0-9]+", text))
        for run in re.findall(r"[\u4e00-\u9fff]+", text):
            result.update(
                run[i : i + n] for n in (2, 3) for i in range(len(run) - n + 1)
            )
            if len(run) == 1:
                result.add(run)
        return result

    def search(self, query, paper_id=None, k=5):
        if not isinstance(query, str) or not query.strip() or len(query) > 2000:
            raise ValueError("请输入 1–2000 字符的检索问题。")
        terms = self.terms(query)
        current = [
            n for n in self.notes(paper_id) if n["confirmed"] and not n["archived"]
        ]
        ranked = []
        for n in current:
            key = (n["id"], n["revision"])
            try:
                if self._cache is None:
                    self._cache = {}
                words = self._cache.setdefault(key, self.terms(n["body"]))
            except Exception:
                words = self.terms(n["body"])
            overlap = terms & words
            score = len(overlap) / max(1, len(terms))
            if overlap and score >= 0.08:
                latest = self.get(n["id"])
                if (
                    latest["revision"] == n["revision"]
                    and latest["confirmed"]
                    and not latest["archived"]
                ):
                    ranked.append(
                        {
                            **n,
                            "score": round(score, 4),
                            "url": f"/reader?paper={n['paper_id']}&note={n['id']}&revision={n['revision']}",
                        }
                    )
        ranked = sorted(ranked, key=lambda n: (-n["score"], n["id"]))[
            : max(1, min(int(k), 20))
        ]
        return [
            n
            for n in ranked
            if (latest := self.get(n["id"]))["revision"] == n["revision"]
            and latest["confirmed"]
            and not latest["archived"]
        ]

    def settings(self):
        with self.read() as con:
            return {
                r[0]: json.loads(r[1])
                for r in con.execute("SELECT key,value FROM reading_settings")
            }

    def set_auto(self, enabled):
        if type(enabled) is not bool:
            raise ValueError("自动翻译设置必须为布尔值。")
        with self.transaction() as con:
            con.execute(
                "INSERT INTO reading_settings VALUES('auto_translate',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (dump(enabled),),
            )

    def reserve_job(self, paper_id, profile):
        paper = self.paper(paper_id)
        original = self.artifact(paper["original_id"])
        key = digest(dump([original["hash"], profile]).encode())
        with self.transaction() as con:
            row = con.execute(
                "SELECT payload FROM translation_jobs WHERE cache_key=?", (key,)
            ).fetchone()
            if row:
                return json.loads(row[0]), False
            job = {
                "id": str(uuid.uuid4()),
                "paper_id": paper_id,
                "original_id": original["id"],
                "original_hash": original["hash"],
                "cache_key": key,
                "profile": profile,
                "state": "queued",
                "stage": "等待翻译",
                "progress": None,
                "attempt": 1,
                "created_at": now(),
                "updated_at": now(),
                "artifact_id": None,
                "error": None,
            }
            con.execute(
                "INSERT INTO translation_jobs VALUES(?,?,?)",
                (job["id"], key, dump(job)),
            )
        return job, True

    def jobs(self, paper_id=None):
        with self.read() as con:
            jobs = [
                json.loads(r[0])
                for r in con.execute("SELECT payload FROM translation_jobs")
            ]
        return sorted(
            [j for j in jobs if paper_id is None or j["paper_id"] == paper_id],
            key=lambda j: j["created_at"],
            reverse=True,
        )

    def job(self, job_id):
        with self.read() as con:
            row = con.execute(
                "SELECT payload FROM translation_jobs WHERE id=?", (job_id,)
            ).fetchone()
        if not row:
            raise KeyError("翻译任务不存在。")
        return json.loads(row[0])

    def update_job(self, job_id, *, expected=None, **values):
        with self.transaction() as con:
            row = con.execute(
                "SELECT payload FROM translation_jobs WHERE id=?", (job_id,)
            ).fetchone()
            if not row:
                raise KeyError("翻译任务不存在。")
            job = json.loads(row[0])
            if expected and job["state"] not in expected:
                raise Conflict("任务状态已变化，请刷新。")
            job.update(values, updated_at=now())
            con.execute(
                "UPDATE translation_jobs SET payload=? WHERE id=?", (dump(job), job_id)
            )
        return job

    def interrupt_jobs(self):
        for job in self.jobs():
            if job["state"] in {"queued", "running"}:
                self.update_job(
                    job["id"],
                    expected={"queued", "running"},
                    state="failed",
                    stage="应用已重启",
                    error="上次任务被中断，未标记成功；请手动重试。",
                    progress=None,
                )
