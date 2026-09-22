"""One remote Qwen model for text, images, and questions via an SSH tunnel."""

from __future__ import annotations

import base64
import json
import math
import hashlib
import os
import re
import urllib.request
from pathlib import Path


QUERY_INSTRUCTION = "Given a search query, retrieve relevant text passages, figures, or tables from scientific papers."
IMAGE_QUERY_INSTRUCTION = "Retrieve an image that matches the given description."
VISUAL_QUERY_PATTERN = r"图片|图表|流程图|示意图|框架图|架构图|结构图|X光片|X线片|曲线图|柱状图|\b(image|figure|diagram|flowchart|radiograph|x-ray)\b"


def query_instruction(text: str) -> str:
    # A deterministic task hint, not another model or a second retrieval pass.
    return IMAGE_QUERY_INSTRUCTION if re.search(VISUAL_QUERY_PATTERN, text, re.IGNORECASE) else QUERY_INSTRUCTION


class RemoteQwenEmbeddings:
    def __init__(self, url: str | None = None):
        self.url = (url or os.getenv("RESEARCH_ASSISTANT_EMBEDDING_URL",
                                    "http://127.0.0.1:30001")).rstrip("/")
        # Do not send private research to an arbitrary endpoint by configuration mistake.
        from urllib.parse import urlsplit
        parsed = urlsplit(self.url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost"}:
            raise ValueError("Embedding endpoint must use a localhost SSH tunnel")
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with self.opener.open(f"{self.url}/health", timeout=5) as response:
            profile = json.load(response)
        if (profile.get("model_id") != "Qwen/Qwen3-VL-Embedding-2B"
                or profile.get("dimension") != 2048
                or profile.get("normalized") is not True
                or len(str(profile.get("fingerprint", ""))) != 64):
            raise ValueError("Unexpected unified embedding service profile")
        self.profile = {key: profile[key] for key in ("model_id", "dimension", "fingerprint", "normalized")}
        retrieval_profile = hashlib.sha256(
            (profile["fingerprint"] + QUERY_INSTRUCTION + IMAGE_QUERY_INSTRUCTION
             + VISUAL_QUERY_PATTERN).encode("utf-8")
        ).hexdigest()
        self.model_name = f"{profile['model_id']}@{retrieval_profile}"
        self.dimension = profile["dimension"]

    def _embed(self, inputs: list[dict]) -> list[list[float]]:
        vectors = []
        for start in range(0, len(inputs), 4):
            batch = inputs[start:start + 4]
            request = urllib.request.Request(f"{self.url}/embed",
                data=json.dumps({"inputs": batch}).encode("utf-8"),
                headers={"Content-Type": "application/json"})
            with self.opener.open(request, timeout=180) as response:
                result = json.load(response)
            if any(result.get(key) != value for key, value in self.profile.items()):
                raise ValueError("Embedding model changed; rebuild the index before searching")
            batch_vectors = result.get("vectors", [])
            if len(batch_vectors) != len(batch):
                raise ValueError("Embedding response count mismatch")
            for vector in batch_vectors:
                if len(vector) != self.dimension or not all(math.isfinite(x) for x in vector):
                    raise ValueError("Invalid embedding vector")
                norm = math.sqrt(sum(x * x for x in vector))
                if not 0.98 <= norm <= 1.02:
                    raise ValueError("Embedding vector is not normalized")
                vectors.append([x / norm for x in vector])
        return vectors

    def embed_texts(self, texts: list[str]) -> list[list[float]]:
        return self._embed([{"text": text} for text in texts])

    def embed_images(self, paths: list[Path], captions: list[str] | None = None) -> list[list[float]]:
        if captions is not None and len(captions) != len(paths):
            raise ValueError("Image and caption counts must match")
        vectors = []
        # Batch image reads as well as inference, avoiding an entire corpus in memory.
        for start in range(0, len(paths), 4):
            inputs = []
            for offset, path in enumerate(paths[start:start + 4]):
                if path.stat().st_size > 4 * 1024 * 1024:
                    raise ValueError("Image exceeds 4 MiB embedding request limit")
                item = {"image_base64": base64.b64encode(path.read_bytes()).decode("ascii")}
                if captions and captions[start + offset].strip():
                    item["text"] = captions[start + offset]
                inputs.append(item)
            vectors.extend(self._embed(inputs))
        return vectors

    def embed_query(self, text: str, image_path: Path | None = None) -> list[float]:
        # Query and evidence have different retrieval roles, not different models.
        item = {"text": text, "instruction": query_instruction(text)}
        if image_path is not None:
            if not text.strip():
                item.pop("text")
            if image_path.stat().st_size > 4 * 1024 * 1024:
                raise ValueError("Query image exceeds 4 MiB")
            item["image_base64"] = base64.b64encode(image_path.read_bytes()).decode("ascii")
            item["instruction"] = QUERY_INSTRUCTION
        return self._embed([item])[0]


def embedding_service_status() -> dict:
    try:
        model = RemoteQwenEmbeddings()
        return {"available": True, **model.profile, "index_model": model.model_name}
    except Exception as exc:
        return {"available": False, "detail": f"{type(exc).__name__}: {exc}"}
