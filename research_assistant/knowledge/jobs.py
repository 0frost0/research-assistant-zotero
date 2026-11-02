"""Durable, bounded state for asynchronous Zotero QA requests.

The job database is deliberately separate from the knowledge database.  A
restart can therefore mark unfinished work as interrupted without changing
knowledge-base revisions or backup compatibility.  Jobs are never retried
implicitly: a user must submit a new request after an interruption.
"""

from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
import threading
import uuid


def now():
    return datetime.now(timezone.utc).isoformat()


class QAJobStore:
    STATES = {"queued", "running", "succeeded", "failed", "interrupted", "cancelled"}

    def __init__(self, path: Path, *, readonly=False):
        self.path = Path(path)
        self.readonly = readonly
        self.lock = threading.RLock()
        if readonly:
            # Evaluation is not a service restart. Never create/migrate a DB
            # or interrupt the web worker's in-flight requests from a reader.
            if not self.path.is_file():
                raise FileNotFoundError(f"问答任务库不存在，请核对 --qa-jobs/--db 路径：{self.path}")
            with closing(self._connect()) as con:
                tables = {row[0] for row in con.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                if not {"qa_jobs", "qa_feedback"}.issubset(tables):
                    raise ValueError("问答任务表不完整，请先由应用完成数据库初始化；评测不自动迁移。")
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as con, con:
            con.executescript("""
                CREATE TABLE IF NOT EXISTS qa_jobs(
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    state TEXT NOT NULL,
                    result TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    started_at TEXT,
                    finished_at TEXT
                );
                CREATE INDEX IF NOT EXISTS qa_jobs_created_at ON qa_jobs(created_at DESC);
                CREATE TABLE IF NOT EXISTS qa_feedback(
                    id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL UNIQUE,
                    rating TEXT NOT NULL,
                    note TEXT NOT NULL DEFAULT '',
                    question TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    kb_ids TEXT NOT NULL,
                    result_hash TEXT NOT NULL,
                    snapshot TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS qa_feedback_created_at ON qa_feedback(created_at DESC);
            """)
            # A process cannot resume an in-flight provider request safely.
            # Mark it interrupted instead of guessing whether it was billed.
            stamp = now()
            con.execute("""UPDATE qa_jobs SET state='interrupted',
                          error='服务重启中断，未自动重试；已发送的请求可能仍由模型服务计费。',
                          updated_at=?, finished_at=?
                          WHERE state IN ('queued','running')""", (stamp, stamp))

    def _connect(self):
        # as_uri quotes spaces, # and ? in paths on both Windows and Linux.
        # mode=ro enforces read-only access even if a write method is called.
        target = self.path.resolve().as_uri() + "?mode=ro" if self.readonly else self.path
        con = sqlite3.connect(target, timeout=15, uri=self.readonly)
        con.row_factory = sqlite3.Row
        return con

    @staticmethod
    def _json(value, limit):
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        if len(payload.encode("utf-8")) > limit:
            raise ValueError("问答任务数据超过允许大小。")
        return payload

    def create(self, kind, payload):
        if kind != "ask" or not isinstance(payload, dict):
            raise ValueError("不支持的问答任务。")
        with self.lock, closing(self._connect()) as con, con:
            ident = self._insert_locked(con, kind, payload)
        return self.public(ident)

    def _insert_locked(self, con, kind, payload):
        encoded = self._json(payload, 100_000)
        ident, stamp = uuid.uuid4().hex[:20], now()
        con.execute("INSERT INTO qa_jobs(id,kind,payload,state,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                    (ident, kind, encoded, "queued", stamp, stamp))
        # Keep the local history bounded; completed results are useful for
        # recovery, but an unattended Zotero client must not grow the DB
        # without limit.  The newest 200 jobs are retained.
        stale = con.execute(
            "SELECT id FROM qa_jobs ORDER BY created_at DESC LIMIT -1 OFFSET 200"
        ).fetchall()
        if stale:
            stale_ids = [row["id"] for row in stale]
            placeholders = ",".join("?" for _ in stale_ids)
            # Feedback is an immutable audit table without a database-level
            # cascade. Remove it before pruning the job so exports never
            # contain an orphaned candidate whose task cannot be reopened.
            con.execute(f"DELETE FROM qa_feedback WHERE job_id IN ({placeholders})", stale_ids)
            con.execute(f"DELETE FROM qa_jobs WHERE id IN ({placeholders})", stale_ids)
        return ident

    def payload(self, ident):
        """Read a private task payload for an explicitly authorized retry."""
        with self.lock, closing(self._connect()) as con:
            row = con.execute("SELECT payload FROM qa_jobs WHERE id=?", (ident,)).fetchone()
        if not row:
            raise KeyError(ident)
        return json.loads(row["payload"])

    def retry(self, ident):
        """Create a bounded manual retry; never retries automatically."""
        with self.lock, closing(self._connect()) as con, con:
            row = con.execute("SELECT kind,state,payload FROM qa_jobs WHERE id=?", (ident,)).fetchone()
            if not row:
                raise KeyError(ident)
            if row["kind"] != "ask" or row["state"] not in {"failed", "interrupted"}:
                raise ValueError("只有失败或服务重启中断的问答任务可以手动重试。")
            payload = json.loads(row["payload"])
            retry_count = payload.get("_retry_count", 0)
            if type(retry_count) is not int or retry_count >= 3:
                raise ValueError("同一问答最多允许手动重试 3 次。")
            retry_payload = {key: value for key, value in payload.items()
                             if not key.startswith("_")}
            retry_payload["_retry_of"] = ident
            retry_payload["_retry_count"] = retry_count + 1
            new_id = self._insert_locked(con, "ask", retry_payload)
        return self.public(new_id)

    def cancel(self, ident):
        """Persist an explicit user stop before cancelling the in-process task."""
        stamp = now()
        with self.lock, closing(self._connect()) as con, con:
            changed = con.execute(
                "UPDATE qa_jobs SET state='cancelled', error=?, updated_at=?, finished_at=? "
                "WHERE id=? AND state IN ('queued','running')",
                ("用户主动停止；已发送的模型请求可能仍由服务计费。", stamp, stamp, ident),
            ).rowcount
        if not changed:
            current = self.public(ident)
            if current["state"] == "cancelled":
                return current
            raise ValueError("只有排队中或执行中的问答任务可以停止。")
        return self.public(ident)

    @staticmethod
    def _feedback_snapshot(payload, result):
        """Keep only auditable answer/evidence fields; never store prompts or keys."""
        evidence = []
        for item in result.get("evidence", []) or []:
            evidence.append({key: item.get(key) for key in (
                "evidence_id", "kind", "entity_id", "source_id", "revision", "title",
                "page", "quote", "url", "source_status") if key in item})
        claims = []
        for claim in result.get("claims", []) or []:
            claims.append({"text": claim.get("text"), "scope": claim.get("scope"),
                           "citations": [{key: cite.get(key) for key in (
                               "evidence_id", "support_quote", "quote", "source_id", "revision",
                               "page", "url", "kind") if key in cite}
                                          for cite in claim.get("citations", [])]})
        snapshot = {"status": result.get("status"), "scope": result.get("scope"),
                    "revisions": result.get("revisions", {}), "evidence": evidence,
                    "claims": claims, "gaps": result.get("gaps", [])}
        encoded = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > 500_000:
            raise ValueError("问答结果过大，无法保存反馈快照。")
        result_hash = hashlib.sha256(json.dumps(
            result, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")).hexdigest()
        return encoded, result_hash

    def record_feedback(self, ident, data):
        """Record one optional user judgment for a completed task.

        Feedback is immutable per job and is explicitly a candidate for later
        evaluation review; it never changes notes, revisions, or gold data.
        """
        if not isinstance(data, dict) or data.get("rating") not in {
            "helpful", "partial", "incorrect", "correct_no_answer"
        }:
            raise ValueError("反馈评分无效。")
        note = data.get("note", "")
        if not isinstance(note, str) or len(note) > 1000:
            raise ValueError("反馈备注过长。")
        with self.lock, closing(self._connect()) as con, con:
            row = con.execute("SELECT * FROM qa_jobs WHERE id=?", (ident,)).fetchone()
            if not row:
                raise KeyError(ident)
            if row["state"] != "succeeded" or row["result"] is None:
                raise ValueError("只有已完成且有回答结果的任务可以反馈。")
            payload = json.loads(row["payload"])
            result = json.loads(row["result"])
            snapshot, result_hash = self._feedback_snapshot(payload, result)
            feedback_id = uuid.uuid4().hex[:20]
            try:
                con.execute("""INSERT INTO qa_feedback
                    (id,job_id,rating,note,question,scope,kb_ids,result_hash,snapshot,created_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?)""", (
                        feedback_id, ident, data["rating"], note.strip(),
                        str(payload.get("query", ""))[:2000], payload.get("scope", "original"),
                        json.dumps(payload.get("kb_ids", []), ensure_ascii=False), result_hash,
                        snapshot, now()))
            except sqlite3.IntegrityError as exc:
                raise ValueError("该问答已经提交过反馈。") from exc
            return self._feedback_public_locked(con, feedback_id, include_snapshot=False)

    @staticmethod
    def _feedback_public_locked(con, feedback_id, include_snapshot=False):
        row = con.execute("SELECT * FROM qa_feedback WHERE id=?", (feedback_id,)).fetchone()
        if not row:
            raise KeyError(feedback_id)
        result = {"feedback_id": row["id"], "job_id": row["job_id"],
                  "rating": row["rating"], "note": row["note"],
                  "question": row["question"], "scope": row["scope"],
                  "result_hash": row["result_hash"], "created_at": row["created_at"]}
        if include_snapshot:
            result["kb_ids"] = json.loads(row["kb_ids"])
            result["snapshot"] = json.loads(row["snapshot"])
        return result

    @staticmethod
    def _job_public_locked(con, row, include_result=True):
        result = {
            "job_id": row["id"], "kind": row["kind"], "state": row["state"],
            "created_at": row["created_at"], "updated_at": row["updated_at"],
            "started_at": row["started_at"], "finished_at": row["finished_at"],
            "error": row["error"],
        }
        payload = json.loads(row["payload"])
        result["question"] = str(payload.get("query", ""))[:500]
        result["scope"] = payload.get("scope", "original")
        result["retry_of"] = payload.get("_retry_of")
        result["retry_count"] = payload.get("_retry_count", 0)
        if include_result and row["result"] is not None:
            result["result"] = json.loads(row["result"])
        feedback = con.execute("SELECT id FROM qa_feedback WHERE job_id=?", (row["id"],)).fetchone()
        result["feedback"] = (QAJobStore._feedback_public_locked(
            con, feedback["id"], include_snapshot=False
        ) if feedback else None)
        return result

    def list_feedback(self, limit=100):
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("反馈列表长度无效。")
        with self.lock, closing(self._connect()) as con:
            rows = con.execute("SELECT id FROM qa_feedback ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
            return [self._feedback_public_locked(con, row["id"], include_snapshot=True) for row in rows]

    def list_feedback_candidates(self, limit=100, scope=None, ratings=None):
        """Read only the prioritized feedback snapshots needed for review.

        Priority ordering happens in SQLite so exporting a small review queue
        does not first materialize every large answer snapshot in Python.
        """
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("反馈列表长度无效。")
        clauses, params = [], []
        if scope:
            if scope not in {"original", "notes", "both"}:
                raise ValueError("反馈范围无效。")
            clauses.append("scope=?")
            params.append(scope)
        if ratings:
            allowed = {"helpful", "partial", "incorrect", "correct_no_answer"}
            selected = list(dict.fromkeys(ratings))
            if not selected or any(item not in allowed for item in selected):
                raise ValueError("反馈评分无效。")
            placeholders = ",".join("?" for _ in selected)
            clauses.append(f"rating IN ({placeholders})")
            params.extend(selected)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        query = ("SELECT id FROM qa_feedback" + where + " ORDER BY "
                 "CASE rating WHEN 'incorrect' THEN 0 WHEN 'partial' THEN 1 "
                 "WHEN 'correct_no_answer' THEN 2 ELSE 3 END, created_at DESC LIMIT ?")
        params.append(limit)
        with self.lock, closing(self._connect()) as con:
            rows = con.execute(query, params).fetchall()
            return [self._feedback_public_locked(con, row["id"], include_snapshot=True) for row in rows]

    def public(self, ident, include_result=True):
        with self.lock, closing(self._connect()) as con:
            row = con.execute("SELECT * FROM qa_jobs WHERE id=?", (ident,)).fetchone()
            if not row:
                raise KeyError(ident)
            return self._job_public_locked(con, row, include_result)

    def list(self, limit=20):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("任务列表长度无效。")
        with self.lock, closing(self._connect()) as con:
            rows = con.execute(
                "SELECT id FROM qa_jobs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
            # Read the complete rows while this connection is open.  This
            # avoids a race where pruning could delete an ID between a list
            # query and a second detail query.
            full_rows = [con.execute("SELECT * FROM qa_jobs WHERE id=?", (row["id"],)).fetchone()
                         for row in rows]
            return [self._job_public_locked(con, row, include_result=False)
                    for row in full_rows if row is not None]

    def stats(self):
        """Return content-free task counts for the authenticated status API."""
        with self.lock, closing(self._connect()) as con:
            rows = con.execute("SELECT state, payload FROM qa_jobs").fetchall()
        by_state = {}
        notes_total = notes_succeeded = 0
        for row in rows:
            state = row["state"]
            by_state[state] = by_state.get(state, 0) + 1
            try:
                scope = json.loads(row["payload"]).get("scope", "original")
            except (TypeError, ValueError, json.JSONDecodeError):
                scope = "original"
            if scope == "notes":
                notes_total += 1
                notes_succeeded += int(state == "succeeded")
        return {"total": len(rows), "by_state": by_state,
                "notes_total": notes_total, "notes_succeeded": notes_succeeded}

    def review_questions(self, limit=50, scope="original"):
        """Return deduplicated user questions for a private review template.

        Only the question and selected knowledge-base IDs are exposed. Model
        results, excerpts, prompts and credentials remain in the job store.
        The benchmark command retrieves current evidence and still requires
        human approval before producing a dataset.
        """
        if type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("评测问题数量无效。")
        if scope not in {"original", "notes", "both"}:
            raise ValueError("评测范围无效。")
        with self.lock, closing(self._connect()) as con:
            rows = con.execute(
                "SELECT j.id,j.payload,f.rating FROM qa_jobs j "
                "LEFT JOIN qa_feedback f ON f.job_id=j.id "
                "WHERE j.kind='ask' AND j.state='succeeded' ORDER BY "
                "CASE f.rating WHEN 'incorrect' THEN 0 WHEN 'partial' THEN 1 "
                "WHEN 'correct_no_answer' THEN 2 WHEN 'helpful' THEN 3 ELSE 4 END, "
                "j.created_at DESC LIMIT ?", (min(limit * 4, 1000),)
            ).fetchall()
        result, seen = [], set()
        for row in rows:
            try:
                payload = json.loads(row["payload"])
                query = str(payload.get("query", "")).strip()
                kb_ids = payload.get("kb_ids")
                job_scope = payload.get("scope", "original")
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if job_scope != scope or not query or not isinstance(kb_ids, list) or not kb_ids:
                continue
            normalized_scope = tuple(sorted(str(value) for value in kb_ids))
            identity = (query, normalized_scope, scope)
            if identity in seen:
                continue
            seen.add(identity)
            rating = row["rating"]
            result.append({"id": "QJOB-" + row["id"], "query": query[:2000],
                           "kb_ids": list(normalized_scope), "source_job_id": row["id"],
                           "feedback_rating": rating,
                           "review_priority": {"incorrect": 0, "partial": 1,
                                               "correct_no_answer": 2, "helpful": 3}.get(rating, 4)})
            if len(result) >= limit:
                break
        return result

    def conversation_history(self, conversation_id, kb_ids, limit=3):
        """Return only recent successful turns in the same KB scope.

        History is navigation context for a follow-up question. It is never
        treated as evidence, and only claim text is exposed to the QA layer;
        source quotes remain in the current retrieval path.
        """
        if not isinstance(conversation_id, str) or not conversation_id.strip():
            return []
        wanted = tuple(sorted(str(value) for value in (kb_ids or [])))
        if not wanted:
            return []
        with self.lock, closing(self._connect()) as con:
            rows = con.execute(
                "SELECT payload,result FROM qa_jobs WHERE state='succeeded' "
                "ORDER BY created_at DESC LIMIT 200"
            ).fetchall()
        turns = []
        for row in rows:
            try:
                payload = json.loads(row["payload"])
                result = json.loads(row["result"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if payload.get("conversation_id") != conversation_id:
                continue
            scope = tuple(sorted(str(value) for value in payload.get("kb_ids", [])))
            if scope != wanted:
                continue
            claims = [item.get("text", "") for item in result.get("claims", [])
                      if isinstance(item, dict) and item.get("text")]
            turns.append({"question": str(payload.get("query", "")), "claims": claims})
            if len(turns) >= limit:
                break
        return list(reversed(turns))

    def mark_running(self, ident):
        stamp = now()
        with self.lock, closing(self._connect()) as con, con:
            changed = con.execute("UPDATE qa_jobs SET state='running',started_at=?,updated_at=? "
                                  "WHERE id=? AND state='queued'", (stamp, stamp, ident)).rowcount
        if not changed:
            raise ValueError("问答任务已结束或不存在。")
        return self.public(ident, include_result=False)

    def finish(self, ident, state, result=None, error=None):
        if state not in {"succeeded", "failed", "interrupted"}:
            raise ValueError("无效的任务结束状态。")
        encoded = None if result is None else self._json(result, 2_000_000)
        stamp = now()
        with self.lock, closing(self._connect()) as con, con:
            changed = con.execute("""UPDATE qa_jobs SET state=?,result=?,error=?,updated_at=?,finished_at=?
                                  WHERE id=? AND state IN ('queued','running')""",
                                  (state, encoded, error, stamp, stamp, ident)).rowcount
        if not changed:
            return self.public(ident)
        return self.public(ident)

