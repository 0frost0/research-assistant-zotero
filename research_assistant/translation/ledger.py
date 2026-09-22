"""Durable full-PDF request ledger; stdlib-only for isolated/spawned workers.

Normal jobs meter usage without a total cap. Explicit limits are reserved for
isolated paid validation scripts. Unknown usage is never reported as actual.
"""
from contextlib import closing, contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import sqlite3
import time
import uuid

DEFAULT_TOKENS = 0
DEFAULT_REQUESTS = 0
OUTPUT_TOKENS = 2048
MAX_IDENTICAL_REQUESTS = 6
MAX_IDENTICAL_FAILURES = 3
MAX_CONSECUTIVE_FAILURES = 8


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def day():
    return datetime.now(timezone(timedelta(hours=8))).date().isoformat()


class BudgetStopped(ValueError):
    pass


class TranslationLedger:
    def __init__(self, path):
        self.path = str(path)
        with self.transaction() as con:
            con.execute("CREATE TABLE IF NOT EXISTS translation_budget_schema(version INTEGER PRIMARY KEY)")
            version = con.execute("SELECT MAX(version) FROM translation_budget_schema").fetchone()[0]
            if version is not None and version > 2:
                raise ValueError("翻译账本版本较新，请升级应用。")
            con.execute("INSERT OR IGNORE INTO translation_budget_schema VALUES(1)")
            con.execute("CREATE TABLE IF NOT EXISTS translation_budgets(job_id TEXT PRIMARY KEY, tokens INTEGER NOT NULL, requests INTEGER NOT NULL, blocked TEXT)")
            con.execute("CREATE TABLE IF NOT EXISTS translation_attempts(job_id TEXT NOT NULL, attempt INTEGER NOT NULL, state TEXT NOT NULL, started_at TEXT NOT NULL, finished_at TEXT, PRIMARY KEY(job_id,attempt))")
            con.execute("CREATE TABLE IF NOT EXISTS translation_requests(id TEXT PRIMARY KEY, job_id TEXT NOT NULL, attempt INTEGER NOT NULL, day TEXT NOT NULL, state TEXT NOT NULL, reserved INTEGER NOT NULL, accounted INTEGER NOT NULL, input_bytes INTEGER NOT NULL, fingerprint TEXT NOT NULL, usage TEXT NOT NULL, error TEXT, created_at TEXT NOT NULL, finished_at TEXT)")
            con.execute("CREATE INDEX IF NOT EXISTS full_request_job ON translation_requests(job_id)")
            con.execute("CREATE INDEX IF NOT EXISTS full_request_day ON translation_requests(day)")
            con.execute("CREATE TABLE IF NOT EXISTS translation_response_diagnostics(request_id TEXT PRIMARY KEY, metadata TEXT NOT NULL)")
            if "enforce_limits" not in {r[1] for r in con.execute("PRAGMA table_info(translation_budgets)")}:
                # Old production limits become inactive; preserve their historical values.
                con.execute("ALTER TABLE translation_budgets ADD COLUMN enforce_limits INTEGER NOT NULL DEFAULT 0")
            con.execute("CREATE INDEX IF NOT EXISTS full_request_fingerprint ON translation_requests(job_id,attempt,fingerprint)")
            con.execute("INSERT OR IGNORE INTO translation_budget_schema VALUES(2)")

    @contextmanager
    def transaction(self):
        con = sqlite3.connect(self.path, timeout=15)
        con.row_factory = sqlite3.Row
        try:
            con.execute("BEGIN IMMEDIATE")
            yield con
            con.commit()
        except BaseException:
            con.rollback()
            raise
        finally:
            con.close()

    def account(self, job_id, tokens=DEFAULT_TOKENS, requests=DEFAULT_REQUESTS):
        if type(tokens) is not int or type(requests) is not int or tokens < 0 or requests < 0:
            raise ValueError("验证预算必须为非负整数，0 表示不限制。")
        with self.transaction() as con:
            con.execute("INSERT OR IGNORE INTO translation_budgets(job_id,tokens,requests,blocked,enforce_limits) VALUES(?,?,?,NULL,?)", (job_id, tokens, requests, int(bool(tokens or requests))))

    def start(self, job_id, attempt):
        with self.transaction() as con:
            con.execute("UPDATE translation_budgets SET blocked=NULL WHERE job_id=?", (job_id,))
            con.execute("INSERT INTO translation_attempts VALUES(?,?,?, ?,NULL)", (job_id, attempt, "running", timestamp()))

    def recover(self, job_id=None, state="interrupted"):
        # Called only after worker termination, or during single-process startup.
        suffix, args = (" AND job_id=?", (job_id,)) if job_id else ("", ())
        with self.transaction() as con:
            con.execute("UPDATE translation_requests SET state='interrupted',error='interrupted',finished_at=? WHERE state='pending'" + suffix, (timestamp(), *args))
            con.execute("UPDATE translation_attempts SET state=?,finished_at=? WHERE state='running'" + suffix, (state, timestamp(), *args))

    def reserve(self, job_id, attempt, payload):
        # In-flight reservations may be released when actual usage arrives.
        # Wait outside the transaction instead of prematurely failing a small
        # budget merely because several paragraph workers started together.
        deadline = time.monotonic() + 100
        while True:
            result = self._reserve_once(job_id, attempt, payload)
            if result is not None:
                return result
            if time.monotonic() >= deadline:
                with self.transaction() as con:
                    con.execute("UPDATE translation_budgets SET blocked='budget_exhausted' WHERE job_id=?", (job_id,))
                raise BudgetStopped("budget_exhausted")
            time.sleep(.05)

    def _reserve_once(self, job_id, attempt, payload):
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode()
        reserved = len(encoded) + OUTPUT_TOKENS + 256
        fingerprint = hashlib.sha256(encoded).hexdigest()
        ident = str(uuid.uuid4())
        denied = False
        with self.transaction() as con:
            account = con.execute("SELECT * FROM translation_budgets WHERE job_id=?", (job_id,)).fetchone()
            active = con.execute("SELECT state FROM translation_attempts WHERE job_id=? AND attempt=?", (job_id, attempt)).fetchone()
            if not account or not active or active[0] != "running":
                raise BudgetStopped("翻译账本未就绪，未发送请求。")
            if account["blocked"]:
                raise BudgetStopped(account["blocked"])
            n, total = con.execute("SELECT COUNT(*),COALESCE(SUM(accounted),0) FROM translation_requests WHERE job_id=?", (job_id,)).fetchone()
            same, failures = con.execute("SELECT COUNT(*),COALESCE(SUM(state='failed'),0) FROM translation_requests WHERE job_id=? AND attempt=? AND fingerprint=?", (job_id, attempt, fingerprint)).fetchone()
            # Count pending duplicates as well, atomically across threads/processes.
            reason = "repeated_request" if same >= MAX_IDENTICAL_REQUESTS or failures >= MAX_IDENTICAL_FAILURES else None
            pending = con.execute("SELECT COUNT(*) FROM translation_requests WHERE job_id=? AND state='pending'", (job_id,)).fetchone()[0]
            if account["enforce_limits"] and not reason:
                requests_full = bool(account["requests"] and n >= account["requests"])
                tokens_full = bool(account["tokens"] and total + reserved > account["tokens"])
                if tokens_full and not requests_full and reserved <= account["tokens"] and pending:
                    return None
                if requests_full or tokens_full:
                    reason = "budget_exhausted"
            if reason:
                con.execute("UPDATE translation_budgets SET blocked=? WHERE job_id=?", (reason, job_id))
                denied = reason
            else:
                con.execute("INSERT INTO translation_requests VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (ident, job_id, attempt, day(), "pending", reserved, reserved, len(encoded), fingerprint, "{}", None, timestamp(), None))
        if denied:
            raise BudgetStopped(denied)
        return ident

    def activity(self, job_id):
        with closing(sqlite3.connect(self.path, timeout=15)) as con:
            row = con.execute(
                "SELECT blocked,(SELECT COUNT(*) FROM translation_requests WHERE job_id=? AND state='pending') "
                "FROM translation_budgets WHERE job_id=?", (job_id, job_id),
            ).fetchone()
        return (row[0], row[1]) if row else (None, 0)

    def finish(self, ident, *, usage=None, error=None, halt=False, diagnostics=None):
        # Whitelist numeric usage. Never persist exception messages or prompts.
        safe = {k: v for k, v in (usage or {}).items() if k in {
            "prompt_tokens", "completion_tokens", "total_tokens", "prompt_cache_hit_tokens", "prompt_cache_miss_tokens"
        } and type(v) is int and v >= 0}
        # A zero total for a nonempty request is not credible accounting.
        if safe.get("total_tokens", 0) <= 0:
            safe.pop("total_tokens", None)
        with self.transaction() as con:
            row = con.execute("SELECT * FROM translation_requests WHERE id=? AND state='pending'", (ident,)).fetchone()
            if not row:
                raise BudgetStopped("请求账本已结束，拒绝覆盖。")
            con.execute("UPDATE translation_requests SET state=?,accounted=?,usage=?,error=?,finished_at=? WHERE id=?",
                        ("failed" if error else "succeeded", safe.get("total_tokens", row["reserved"]), json.dumps(safe), error, timestamp(), ident))
            if diagnostics is not None:
                metadata = {k: v for k, v in diagnostics.items() if k in {
                    "content_chars", "reasoning_chars", "reasoning_tokens"
                } and type(v) is int and v >= 0}
                reason = diagnostics.get("finish_reason")
                metadata["finish_reason"] = reason if isinstance(reason, str) and reason in {
                    "stop", "length", "content_filter", "tool_calls", "function_call"
                } else "unknown"
                con.execute("INSERT INTO translation_response_diagnostics VALUES(?,?)",
                            (ident, json.dumps(metadata)))
            if halt:
                con.execute("UPDATE translation_budgets SET blocked=COALESCE(blocked,?) WHERE job_id=?", (error, row["job_id"]))
            elif error:
                recent = con.execute("SELECT state FROM translation_requests WHERE job_id=? AND attempt=? AND state IN ('succeeded','failed') ORDER BY finished_at DESC,rowid DESC LIMIT ?", (row["job_id"], row["attempt"], MAX_CONSECUTIVE_FAILURES)).fetchall()
                if len(recent) == MAX_CONSECUTIVE_FAILURES and all(r[0] == "failed" for r in recent):
                    con.execute("UPDATE translation_budgets SET blocked=COALESCE(blocked,'consecutive_failures') WHERE job_id=?", (row["job_id"],))

    def summary(self, job_id):
        with self.transaction() as con:
            account = con.execute("SELECT * FROM translation_budgets WHERE job_id=?", (job_id,)).fetchone()
            if not account:
                return None  # Legacy tasks have no request history; never invent it.
            rows = con.execute("SELECT attempt,state,reserved,accounted,usage,error FROM translation_requests WHERE job_id=?", (job_id,)).fetchall()
            attempts = [dict(r) for r in con.execute("SELECT attempt,state,started_at,finished_at FROM translation_attempts WHERE job_id=? ORDER BY attempt", (job_id,))]
        reported, unknown, input_tokens, output_tokens, cached = 0, 0, 0, 0, 0
        for row in rows:
            usage = json.loads(row["usage"])
            if "total_tokens" in usage:
                reported += usage["total_tokens"]
            else:
                unknown += row["accounted"]
            input_tokens += usage.get("prompt_tokens", 0)
            output_tokens += usage.get("completion_tokens", 0)
            cached += usage.get("prompt_cache_hit_tokens", 0)
        for attempt in attempts:
            current = [r for r in rows if r["attempt"] == attempt["attempt"]]
            usages = [json.loads(r["usage"]) for r in current]
            attempt.update(requests=len(current),
                           reported_tokens=sum(u.get("total_tokens", 0) for u in usages),
                           prompt_tokens_reported=sum(u.get("prompt_tokens", 0) for u in usages),
                           completion_tokens_reported=sum(u.get("completion_tokens", 0) for u in usages),
                           unknown_requests=sum("total_tokens" not in u for u in usages))
        return {"token_limit": account["tokens"] if account["enforce_limits"] else None,
                "request_limit": account["requests"] if account["enforce_limits"] else None,
                "unknown_requests": sum("total_tokens" not in json.loads(r["usage"]) for r in rows),
                "requests": len(rows), "accounted_tokens": sum(r["accounted"] for r in rows),
                "reported_tokens": reported, "unknown_reserved_tokens": unknown,
                "prompt_tokens_reported": input_tokens, "completion_tokens_reported": output_tokens,
                "provider_cache_tokens_reported": cached, "blocked": account["blocked"], "attempts": attempts}


def metered_create(create, ledger, job_id, attempt):
    """Wrap the real SDK boundary, including upstream fallback calls."""
    def call(*args, **kwargs):
        if args or kwargs.get("stream"):
            raise BudgetStopped("不支持未计量的模型请求。")
        kwargs["max_tokens"] = min(kwargs.get("max_tokens", OUTPUT_TOKENS), OUTPUT_TOKENS)
        ident = ledger.reserve(job_id, attempt, kwargs)
        try:
            response = create(**kwargs)
        except Exception as exc:
            from research_assistant.translation.policy import error_code
            status = getattr(exc, "status_code", None)
            ledger.finish(ident, error=error_code(status, type(exc).__name__), halt=status in (400, 401, 402, 403, 404, 422))
            raise
        usage = response.usage.model_dump() if getattr(response, "usage", None) else {}
        choices = getattr(response, "choices", None)
        choice = choices[0] if choices else None
        reason = getattr(choice, "finish_reason", None)
        message = getattr(choice, "message", None)
        content = getattr(message, "content", None)
        reasoning = getattr(message, "reasoning_content", None)
        diagnostics = {
            "finish_reason": reason,
            "content_chars": len(content) if isinstance(content, str) else 0,
            "reasoning_chars": len(reasoning) if isinstance(reasoning, str) else 0,
        }
        details = usage.get("completion_tokens_details")
        if isinstance(details, dict):
            diagnostics["reasoning_tokens"] = details.get("reasoning_tokens")
        if reason == "length":
            error = "output_truncated"
        elif reason == "content_filter":
            error = "content_filtered"
        elif reason != "stop":
            error = "invalid_response"
        elif not isinstance(content, str) or not content.strip():
            error = "empty_response"
        else:
            error = None
        ledger.finish(ident, usage=usage, error=error, halt=error is not None, diagnostics=diagnostics)
        if error:
            raise BudgetStopped(error)
        return response
    return call
