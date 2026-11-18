"""Bounded, authenticated client for the project-local Qwen reranker service."""
import json
import math
import os
import urllib.request
from urllib.parse import urlsplit

from research_assistant.core.service_context import embedding_config
from research_assistant.retrieval.embeddings.api_embeddings import NoRedirect


MODEL = "Qwen/Qwen3-Reranker-0.6B"
REVISION = "e61197ed45024b0ed8a2d74b80b4d909f1255473"
INSTRUCTION = (
    "Retrieve passages that directly answer the question, preserving its conditions, entities and numbers. "
    "For a definition question, prefer a passage explaining what the requested concept is, "
    "not a passage that merely mentions it."
)


class RerankerClient:
    def __init__(self, endpoint=None, api_key=None, timeout=20):
        self.endpoint = endpoint or os.getenv("RESEARCH_RERANKER_URL", "")
        parsed = urlsplit(self.endpoint)
        # This deployment is explicitly loopback behind the existing SSH tunnel.
        if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("Reranker must use the project loopback endpoint")
        config = embedding_config() or {}
        self.key = api_key if api_key is not None else os.getenv("RESEARCH_RERANKER_API_KEY", "") or config.get("api_key", "")
        if not self.key:
            raise ValueError("Reranker authentication not configured")
        self.timeout = timeout
        self.opener = urllib.request.build_opener(NoRedirect(), urllib.request.ProxyHandler({}))

    def scores(self, question, documents):
        if not documents:
            return []
        if (not isinstance(question, str) or not 1 <= len(question) <= 2000 or len(documents) > 40
                or any(not isinstance(d, str) or not 1 <= len(d) <= 2000 for d in documents)):
            raise ValueError("Reranking input exceeds bounded budget")
        body = {"model": MODEL, "query": question, "documents": documents, "instruction": INSTRUCTION}
        request = urllib.request.Request(self.endpoint, data=json.dumps(body).encode(), headers={
            "Authorization": "Bearer " + self.key, "Content-Type": "application/json"})
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                raw = response.read(65537)
                if len(raw) > 65536:
                    raise ValueError("Oversized reranker response")
                result = json.loads(raw)
        except (OSError, ValueError):
            raise RuntimeError("Reranker unavailable or invalid response") from None
        if not isinstance(result, dict) or result.get("model") != MODEL or result.get("revision") != REVISION:
            raise ValueError("Reranker model/revision mismatch")
        scores = result.get("scores")
        if (not isinstance(scores, list) or len(scores) != len(documents) or
                any(type(s) not in (int, float) or not math.isfinite(s) or not 0 <= s <= 1 for s in scores)):
            raise ValueError("Reranker score count or range invalid")
        return scores
