"""Request-local profiles, inherited by asyncio tasks and to_thread.

Connection IDs group settings, not independently authenticated tenants.
Credentials must never be copied into task payloads or telemetry.
"""
from contextvars import ContextVar
import os

service_context = ContextVar("research_service_config", default=None)


def embedding_config():
    context = service_context.get()
    return context["config"].get("embedding") if context else None


def llm_config():
    context = service_context.get()
    configured = context["config"].get("llm") if context else None
    return configured or {"base_url": os.getenv("OPENAI_BASE_URL", ""),
                          "model": os.getenv("OPENAI_MODEL", ""),
                          "api_key": os.getenv("OPENAI_API_KEY", "")}


def review_model():
    context = service_context.get()
    if context and context["config"].get("llm"):
        return llm_config()["model"]
    return os.getenv("RESEARCH_QA_REVIEW_MODEL") or llm_config()["model"]
