"""vLLM Qwen3-VL chat embeddings: one messages request is ONE item.

Protocol reference: examples/pooling/embed/vision_embedding_online.py,
run_qwen3_vl. This is /v1/embeddings, not /chat/completions.
"""
import base64
import hashlib
import json
import math
import sqlite3
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit
import urllib.request
import urllib.error

from .remote_embeddings import query_instruction, QUERY_INSTRUCTION


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class QwenAPIEmbeddings:
    def __init__(self, config, *, cache_path=None):
        base = config["base_url"].rstrip("/")
        parsed = urlsplit(base)
        if (parsed.username or parsed.password or parsed.query or parsed.fragment
                or not parsed.hostname or not (parsed.scheme == "https" or
                parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1", "::1"})):
            raise ValueError("Invalid embedding API address")
        self.url = base if base.endswith("/embeddings") else base + "/embeddings"
        self.model, self.key = config["model"], config["api_key"]
        self.dimension = config.get("dimension", 2048)
        if type(self.dimension) is not int or not 1 <= self.dimension <= 8192:
            raise ValueError("Invalid embedding dimension")
        if not self.model or not self.key:
            raise ValueError("Embedding API model/key missing")
        profile = {"endpoint": self.url, "model": self.model,
                   "dimension": self.dimension, "revision": config.get("revision", "1"),
                   "protocol": "vllm-qwen3-vl-messages-v1", "normalization": "l2",
                   "document_instruction": "Represent the user's input.",
                   "query_instruction": QUERY_INSTRUCTION,
                   "visual_instruction": query_instruction("figure")}
        fingerprint = hashlib.sha256(json.dumps(profile, sort_keys=True).encode()).hexdigest()
        self.model_name = f"{self.model}@{fingerprint}"
        handlers = [NoRedirect()]
        if parsed.hostname in {"localhost", "127.0.0.1", "::1"}:
            handlers.append(urllib.request.ProxyHandler({}))
        self.opener = urllib.request.build_opener(*handlers)
        self.cache_path = Path(cache_path) if cache_path else None
        if self.cache_path:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            with closing(sqlite3.connect(self.cache_path)) as con, con:
                con.execute("CREATE TABLE IF NOT EXISTS vectors (key TEXT PRIMARY KEY, vector TEXT NOT NULL)")

    @staticmethod
    def _image(path):
        path = Path(path)
        if path.stat().st_size > 4 * 1024 * 1024:
            raise ValueError("Image exceeds 4 MiB")
        raw = path.read_bytes()
        mime = ("image/png" if raw.startswith(b"\x89PNG\r\n\x1a\n") else
                "image/jpeg" if raw.startswith(b"\xff\xd8\xff") else
                "image/webp" if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP" else None)
        if not mime:
            raise ValueError("Embedding images must be PNG, JPEG or WebP")
        return {"type": "image_url", "image_url": {
            "url": f"data:{mime};base64," + base64.b64encode(raw).decode("ascii")}}

    def _embed_one(self, text, image=None, instruction="Represent the user's input."):
        if not isinstance(text, str) or len(text) > 16000 or not text.strip() and image is None:
            raise ValueError("Invalid embedding input")
        content = ([self._image(image)] if image is not None else [])
        content.append({"type": "text", "text": text})
        # Cache exact content + model/protocol/instruction, not a document scope.
        # Each successful item is durable even if a later item fails.
        cache_key = hashlib.sha256(json.dumps(
            [self.model_name, instruction, content], sort_keys=True).encode()).hexdigest()
        if self.cache_path:
            with closing(sqlite3.connect(self.cache_path)) as con:
                row = con.execute("SELECT vector FROM vectors WHERE key=?", (cache_key,)).fetchone()
            if row:
                vector = json.loads(row[0])
                if (len(vector) == self.dimension and all(type(v) in (float, int) and math.isfinite(v) for v in vector)
                        and abs(sum(v*v for v in vector) - 1) < 0.001):
                    return vector
        payload = {"model": self.model, "encoding_format": "float",
                   "continue_final_message": True, "add_special_tokens": True,
                   "messages": [
                       {"role": "system", "content": [{"type": "text", "text": instruction}]},
                       {"role": "user", "content": content},
                       {"role": "assistant", "content": [{"type": "text", "text": ""}]}]}
        request = urllib.request.Request(self.url, data=json.dumps(payload).encode(), headers={
            "Content-Type": "application/json", "Authorization": "Bearer " + self.key})
        try:
            # No automatic retry: failed responses can already be billable.
            with self.opener.open(request, timeout=45) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"Embedding API HTTP {exc.code}; check authentication/model/protocol") from None
        except (OSError, ValueError):
            raise RuntimeError("Embedding API connection or response failed") from None
        data = result.get("data") if isinstance(result, dict) else None
        if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
            raise ValueError("Embedding API must return one data item per messages request")
        vector = data[0].get("embedding")
        if (not isinstance(vector, list) or len(vector) != self.dimension or
                not all(type(v) in (int, float) and math.isfinite(v) for v in vector)):
            raise ValueError("Embedding vector invalid or dimension mismatch; check configured dimension")
        norm = math.sqrt(sum(v * v for v in vector))
        if not math.isfinite(norm) or norm <= 0:
            raise ValueError("Invalid embedding norm")
        vector = [v / norm for v in vector]
        if self.cache_path:
            with closing(sqlite3.connect(self.cache_path)) as con, con:
                con.execute("INSERT OR REPLACE INTO vectors VALUES (?, ?)", (cache_key, json.dumps(vector)))
        return vector

    def embed_texts(self, texts):
        return [self._embed_one(text) for text in texts]

    def embed_images(self, paths, captions=None):
        if captions is not None and len(captions) != len(paths):
            raise ValueError("Image and caption counts must match")
        return [self._embed_one(captions[i] if captions else "", path) for i, path in enumerate(paths)]

    def embed_query(self, text, image_path=None):
        return self._embed_one(text, image_path, QUERY_INSTRUCTION if image_path is not None else query_instruction(text))
