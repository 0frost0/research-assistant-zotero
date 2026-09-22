"""Local, content-free execution spans. Usage is reported, never guessed from text length."""

import copy
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def token_usage(raw):
    """LangChain input_tokens includes cached input; do not add cache counts again."""
    return normalize_usage(getattr(raw, "usage_metadata", None))


def normalize_usage(values):
    if not isinstance(values, dict):
        return None
    names = ("input_tokens", "output_tokens", "total_tokens")
    if any(type(values.get(k)) is not int or values[k] < 0 for k in names):
        return None
    if values["input_tokens"] + values["output_tokens"] != values["total_tokens"]:
        return None
    result = {k: values[k] for k in names}
    details = values.get("input_token_details", {})
    cached = details.get("cache_read") if isinstance(details, dict) else None
    if cached is None:
        cached = values.get("cache_read_tokens")
    if type(cached) is int and 0 <= cached <= result["input_tokens"]:
        result["cache_read_tokens"] = cached
    return result


@dataclass
class ModelReply:
    value: Any
    usage: dict | None = None
    usage_source: str = "provider"
    parsing_error: Exception | None = None


def error_category(exc):
    name = type(exc).__name__
    if isinstance(exc, TimeoutError) or name == "APITimeoutError":
        return "timeout"
    if isinstance(exc, ConnectionError) or name == "APIConnectionError":
        return "connection"
    if name in {"AuthenticationError", "PermissionDeniedError"}:
        return "authentication"
    if name == "RateLimitError":
        return "rate_limit"
    if name in {"ValidationError", "OutputParserException"}:
        return "invalid_output"
    return "execution_error"


def summarize(spans, wall_ms):
    calls = [s for s in spans if s["kind"] == "model" and s.get("request_start_ms") is not None]
    # Simulated usage must never be counted as provider-reported consumption.
    known = [s for s in calls if s.get("usage") is not None and s.get("usage_source") == "provider"]
    sums = {k: sum(s["usage"][k] for s in known) for k in ("input_tokens", "output_tokens", "total_tokens")}
    points = []
    for s in calls:
        start = s.get("request_start_ms")
        if start is not None:
            end = s.get("end_ms") if s.get("end_ms") is not None else wall_ms
            if end > start:
                points.extend([(start, 1), (end, -1)])
    active = peak = 0
    for _, delta in sorted(points):
        active += delta
        peak = max(peak, active)
    return {"wall_ms": wall_ms, "model_calls": len(calls),
            "completed_calls": sum(s["status"] == "ok" for s in calls),
            "failed_calls": sum(s["status"] in {"error", "timeout"} for s in calls),
            "cancelled_calls": sum(s["status"] == "cancelled" for s in calls),
            "provider_usage_calls": len(known), "unknown_usage_calls": len(calls) - len(known),
            "known_tokens": sums if known else None, "usage_complete": bool(calls) and len(known) == len(calls),
            "peak_model_concurrency": peak}


class ExecutionTrace:
    def __init__(self, publish=lambda snapshot: None, *, clock=time.perf_counter):
        self.clock, self.publish = clock, publish
        self.origin = clock()
        self.data = {"version": 1, "trace_id": uuid.uuid4().hex, "started_at": utc_now(),
                     "ended_at": None, "status": "running", "spans": []}
        self.wall_ms = None

    def elapsed(self):
        return max(0, round((self.clock() - self.origin) * 1000, 3))

    def emit(self):
        self.publish(self.snapshot())

    def start(self, name, kind, *, parent_id=None, paper_slot=None, payload_chars=None):
        span = {"id": uuid.uuid4().hex, "parent_id": parent_id, "name": name, "kind": kind,
                "paper_slot": paper_slot, "start_ms": self.elapsed(), "end_ms": None,
                "duration_ms": None, "request_start_ms": None, "request_duration_ms": None, "queue_ms": None,
                "status": "running", "error_category": None, "payload_chars": payload_chars,
                "usage": None, "usage_source": "not_reported"}
        self.data["spans"].append(span)
        self.emit()
        return span

    def requested(self, span):
        span["request_start_ms"] = self.elapsed()
        span["queue_ms"] = round(span["request_start_ms"] - span["start_ms"], 3)
        self.emit()

    def end(self, span, status="ok", *, category=None):
        if span["status"] != "running":
            return
        span.update(end_ms=self.elapsed(), status=status, error_category=category)
        span["duration_ms"] = round(span["end_ms"] - span["start_ms"], 3)
        if span["request_start_ms"] is not None:
            span["request_duration_ms"] = round(span["end_ms"] - span["request_start_ms"], 3)
        self.emit()

    def finish(self, status):
        for span in self.data["spans"]:
            if span["status"] == "running":
                self.end(span, "cancelled" if status == "cancelled" else "interrupted")
        self.wall_ms = self.elapsed()
        self.data.update(status=status, ended_at=utc_now())
        self.emit()

    def snapshot(self):
        data = copy.deepcopy(self.data)
        data["summary"] = summarize(data["spans"], self.wall_ms if self.wall_ms is not None else self.elapsed())
        return data


def interrupted_trace(data):
    """A restarted process cannot reconstruct monotonic timings from the old process."""
    data = copy.deepcopy(data)
    data.update(status="interrupted", ended_at=utc_now())
    for span in data["spans"]:
        if span["status"] == "running":
            span.update(status="interrupted", end_ms=None, duration_ms=None)
    data["summary"]["timing_incomplete"] = True
    return data
