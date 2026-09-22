"""Private, loopback-only Qwen embedding service using the official helper."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import torch
from PIL import Image


MODEL_ID = "Qwen/Qwen3-VL-Embedding-2B"
ROOT = Path(__file__).resolve().parent
MODEL_PATH = ROOT / "models" / "Qwen3-VL-Embedding-2B"
sys.path.insert(0, str(MODEL_PATH))
from scripts.qwen3_vl_embedding import Qwen3VLEmbedder


class StrictEmbedder(Qwen3VLEmbedder):
    def _preprocess_inputs(self, conversations):
        inputs = super()._preprocess_inputs(conversations)
        has_image = any(
            part.get("type") == "image"
            for conversation in conversations
            for message in conversation
            for part in message["content"]
        )
        # The official helper can fall back to NULL on vision errors. Never index that.
        if has_image and inputs.get("pixel_values") is None:
            raise ValueError("Vision preprocessing failed; no image embedding produced")
        return inputs


def fingerprint() -> str:
    digest = hashlib.sha256()
    for path in sorted(MODEL_PATH.glob("*.safetensors")):
        with path.open("rb") as stream:
            for block in iter(lambda: stream.read(4 * 1024 * 1024), b""):
                digest.update(block)
    for relative in ("config.json", "preprocessor_config.json", "tokenizer.json",
                     "chat_template.jinja", "scripts/qwen3_vl_embedding.py"):
        digest.update((MODEL_PATH / relative).read_bytes())
    digest.update(b"official-last-pool/bf16/length8192/pixels1048576/default-instruction/image-caption-v2")
    return digest.hexdigest()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, _format, *_args):
        pass  # Do not log research text or image payloads.

    def respond(self, status, value):
        data = json.dumps(value, allow_nan=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        self.respond(200 if self.path == "/health" else 404,
                     PROFILE if self.path == "/health" else {"error": "Not found"})

    def do_POST(self):
        if self.path != "/embed":
            self.respond(404, {"error": "Not found"})
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 24 * 1024 * 1024:
                raise ValueError("Request body must be between 1 byte and 24 MiB")
            body = json.loads(self.rfile.read(size))
            items = body.get("inputs")
            if not isinstance(items, list) or not 1 <= len(items) <= 4:
                raise ValueError("Expected 1 to 4 inputs")
            inputs = []
            for item in items:
                if not isinstance(item, dict):
                    raise ValueError("Expected input object")
                instruction = item.get("instruction")
                item = {key: value for key, value in item.items() if key != "instruction"}
                if set(item) not in (
                    {"text"}, {"image_base64"}, {"text", "image_base64"}
                ):
                    raise ValueError("Each input must contain text and/or image_base64")
                prepared = {}
                if instruction is not None:
                    if not isinstance(instruction, str) or not 1 <= len(instruction) <= 512:
                        raise ValueError("Invalid instruction")
                    prepared["instruction"] = instruction
                if "text" in item:
                    value = item["text"]
                    if not isinstance(value, str) or not value.strip() or len(value) > 16000:
                        raise ValueError("Text must contain 1 to 16000 characters")
                    prepared["text"] = value
                if "image_base64" in item:
                    raw = base64.b64decode(item["image_base64"], validate=True)
                    with Image.open(io.BytesIO(raw)) as image:
                        if image.width * image.height > 40_000_000:
                            raise ValueError("Image exceeds 40 megapixels")
                        prepared["image"] = image.convert("RGB")
                inputs.append(prepared)
        except Exception:
            self.respond(400, {"error": "Invalid embedding input or image"})
            return
        if not MODEL_LOCK.acquire(timeout=120):
            self.respond(503, {"error": "Embedding service busy"})
            return
        try:
            vectors = EMBEDDER.process(inputs).float().cpu().tolist()
            if len(vectors) != len(inputs) or any(len(v) != 2048 for v in vectors):
                raise ValueError("Invalid embedding shape")
            self.respond(200, {**PROFILE, "vectors": vectors})
        except Exception as exc:
            print(f"Embedding failed: {type(exc).__name__}", flush=True)
            self.respond(500, {"error": "Embedding inference failed; input was not indexed"})
        finally:
            MODEL_LOCK.release()


if __name__ == "__main__":
    PROFILE = {"model_id": MODEL_ID, "dimension": 2048,
               "fingerprint": fingerprint(), "normalized": True,
               "gpu_uuid": os.getenv("CUDA_VISIBLE_DEVICES")}
    EMBEDDER = StrictEmbedder(str(MODEL_PATH), torch_dtype=torch.bfloat16,
                             attn_implementation="sdpa", max_pixels=1048576)
    MODEL_LOCK = threading.Lock()
    print(json.dumps({"ready": True, **PROFILE}), flush=True)
    ThreadingHTTPServer(("127.0.0.1", int(os.getenv("EMBEDDING_PORT", "30001"))), Handler).serve_forever()
