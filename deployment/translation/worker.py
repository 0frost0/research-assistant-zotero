"""Isolated, pinned upstream API worker. Credentials only arrive over stdin.

Stdout is a small JSON protocol; upstream logs are discarded because exceptions
and debug logs may contain document text or authorization headers.
"""

import asyncio
import contextlib
import importlib.metadata
import json
import logging
import os
from pathlib import Path
import sys
from collections import Counter
from urllib.parse import urlsplit
import re

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from research_assistant.translation.policy import engine_settings, runtime_policy, error_code

# Windows spawn imports this module before running upstream's subprocess target.
# Apply the same bounded client in both processes without editing dependencies.
from bounded_client import install
install()


def preflight(request):
    """DeepSeek balance is read-only and costs no model tokens."""
    import httpx
    url = urlsplit(request["base_url"])
    if url.hostname != "api.deepseek.com":
        return None
    if url.scheme != "https" or url.username or url.password:
        return "invalid_model_request"
    try:
        response = httpx.get(
            request["base_url"].rstrip("/") + "/user/balance",
            headers={"Authorization": "Bearer " + request["key"]},
            timeout=10,
        )
        if response.status_code in (401, 402, 403):
            return error_code(response.status_code)
        response.raise_for_status()
        if response.json().get("is_available") is False:
            return "insufficient_balance"
    except Exception as exc:
        return error_code(getattr(getattr(exc, "response", None), "status_code", None), type(exc).__name__)
    return None


class SafeDiagnostics(logging.Handler):
    """Classify upstream records in memory; never emit messages or tracebacks."""
    def __init__(self):
        super().__init__()
        self.counts = Counter()
        self.failure_code = None

    def emit(self, record):
        message = record.getMessage()
        # The queue has formatted upstream exceptions already. Keep only known
        # categories, never persist its message (which can contain paper text).
        status = re.search(r"Error code: (400|401|402|403|404|422|429)\b", message)
        if status:
            self.failure_code = error_code(int(status[1]))
        if "Error translating paragraph." in message:
            self.counts["paragraph_errors"] += 1
        if "APITimeoutError" in message or "Request timed out" in message:
            self.counts["timeout_records"] += 1
        if "RateLimitError" in message:
            self.counts["rate_limit_records"] += 1
        if "Translation result is the same as input" in message:
            self.counts["unchanged_fallback_records"] += 1


async def main():
    request = json.loads(sys.stdin.readline())
    # Context is inherited by Windows spawn; credentials remain stdin-only.
    # Reject unmetered direct-script runs as well as service invocations.
    if not request.get("ledger"):
        print(json.dumps({"type": "error", "code": "missing_budget"}))
        return
    os.environ["RESEARCH_FULL_TRANSLATION_CONTEXT"] = json.dumps(request["ledger"])
    protocol = sys.stdout

    def emit(value):
        if value.get("type") == "progress":
            try:
                import psutil

                p = psutil.Process()
                value["rss_bytes"] = sum(
                    x.memory_info().rss for x in [p] + p.children(recursive=True)
                )
            except Exception:
                pass
        protocol.write(json.dumps(value, ensure_ascii=False) + "\n")
        protocol.flush()

    for package, expected in request["versions"].items():
        if importlib.metadata.version(package) != expected:
            emit({"type": "error", "code": "version_mismatch"})
            return
    emit({"type": "progress", "stage": "检查模型服务账户", "progress": None})
    failure = preflight(request)
    if failure:
        emit({"type": "error", "code": failure})
        return
    with (
        open(os.devnull, "w") as quiet,
        contextlib.redirect_stdout(quiet),
        contextlib.redirect_stderr(quiet),
    ):
        diagnostics = SafeDiagnostics()
        logging.disable(logging.NOTSET)
        logging.getLogger().handlers = [diagnostics]
        logging.getLogger().setLevel(logging.WARNING)
        try:
            from pdf2zh_next.config.model import SettingsModel
            from pdf2zh_next.high_level import do_translate_async_stream

            policy = runtime_policy(request["model"], request["base_url"])
            settings = SettingsModel(
                translate_engine_settings=engine_settings(request),
                translation={
                    "lang_in": "en",
                    "lang_out": "zh",
                    "output": request["output"],
                    "qps": policy["qps"],
                    "pool_max_workers": policy["workers"],
                    "no_auto_extract_glossary": True,
                },
                pdf={
                    "no_dual": True,
                    "no_mono": False,
                    "watermark_output_mode": "no_watermark",
                    "translate_table_text": True,
                },
                basic={"debug": False},
                report_interval=1,
            )
            async for event in do_translate_async_stream(
                settings, Path(request["input"])
            ):
                kind = event.get("type")
                if kind in {"progress_start", "progress_update", "progress_end"}:
                    emit(
                        {
                            "type": "progress",
                            "stage": str(event.get("stage", "翻译排版"))[:100],
                            "progress": event.get("overall_progress"),
                        }
                    )
                elif kind == "finish":
                    if diagnostics.counts["paragraph_errors"]:
                        emit({"type": "error", "code": diagnostics.failure_code or "partial_translation",
                              "diagnostics": dict(diagnostics.counts)})
                        return
                    result = event["translate_result"]
                    path = getattr(
                        result, "no_watermark_mono_pdf_path", None
                    ) or getattr(result, "mono_pdf_path", None)
                    emit(
                        {
                            "type": "finish",
                            "path": str(path) if path else None,
                            "seconds": getattr(result, "total_seconds", None),
                            "diagnostics": dict(diagnostics.counts),
                            "token_usage": event.get("token_usage", {}),
                        }
                    )
                elif kind == "error":
                    emit({"type": "error", "code": diagnostics.failure_code or "engine_failed",
                          "diagnostics": dict(diagnostics.counts)})
        except Exception as exc:
            emit({"type": "error", "code": type(exc).__name__})


if __name__ == "__main__":
    asyncio.run(main())
