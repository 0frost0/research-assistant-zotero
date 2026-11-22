"""Request-local profiles, inherited by asyncio tasks and to_thread.

Connection IDs group settings, not independently authenticated tenants.
Credentials must never be copied into task payloads or telemetry.
"""
from contextvars import ContextVar
from contextlib import contextmanager
import os
from research_assistant.core.service_config import ServiceConfigStore, chat_base_url

service_context = ContextVar("research_service_config", default=None)


@contextmanager
def stored_service_context(root, connection=None, *, allow_missing=False):
    """Load a saved profile. A search caller may use defaults for an unsaved profile."""
    if not connection:
        yield
        return
    config = ServiceConfigStore(root).get(connection)
    if not config and not allow_missing:
        raise ValueError("指定连接没有服务配置，请检查 --connection。")
    token = service_context.set({"connection": connection, "config": config})
    try:
        yield
    finally:
        service_context.reset(token)


def embedding_config():
    context = service_context.get()
    return context["config"].get("embedding") if context else None


def llm_config():
    context = service_context.get()
    configured = context["config"].get("llm") if context else None
    config = configured or {"base_url": os.getenv("OPENAI_BASE_URL", ""),
                            "model": os.getenv("OPENAI_MODEL", ""),
                            "api_key": os.getenv("OPENAI_API_KEY", "")}
    return {**config, "base_url": chat_base_url(config.get("base_url", ""))}


def review_model():
    context = service_context.get()
    if context and context["config"].get("llm"):
        return llm_config()["model"]
    return os.getenv("RESEARCH_QA_REVIEW_MODEL") or llm_config()["model"]
