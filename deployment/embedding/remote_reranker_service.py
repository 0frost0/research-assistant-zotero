"""Loopback-only, authenticated Qwen reranking; no generation or input logging.

Uses the official yes/no logit scoring recipe, with bounded micro-batches.
Rejects inputs that would require token truncation rather than dropping qualifiers.
"""
import json
import os
from pathlib import Path
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODEL = "Qwen/Qwen3-Reranker-0.6B"
REVISION = "e61197ed45024b0ed8a2d74b80b4d909f1255473"
ROOT = Path(__file__).resolve().parents[2]
PREFIX = ('<|im_start|>system\nJudge whether the Document meets the requirements based on the Query '
          'and the Instruct provided. Note that the answer can only be "yes" or "no".<|im_end|>\n'
          '<|im_start|>user\n')
SUFFIX = '<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n'


def validate(body):
    if not isinstance(body, dict) or body.get("model") != MODEL:
        raise ValueError("Invalid model")
    query, documents, instruction = body.get("query"), body.get("documents"), body.get("instruction")
    if not isinstance(query, str) or not 1 <= len(query) <= 2000:
        raise ValueError("Invalid query")
    if not isinstance(instruction, str) or not 1 <= len(instruction) <= 512:
        raise ValueError("Invalid instruction")
    if (not isinstance(documents, list) or not 1 <= len(documents) <= 40 or
            any(not isinstance(d, str) or not 1 <= len(d) <= 2000 for d in documents)):
        raise ValueError("Invalid documents")
    return query, documents, instruction


class Scorer:
    def __init__(self, path):
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
        self.torch = torch
        self.tokenizer = AutoTokenizer.from_pretrained(path, padding_side="left", local_files_only=True)
        self.model = AutoModelForCausalLM.from_pretrained(path, torch_dtype=torch.bfloat16,
            local_files_only=True, attn_implementation="sdpa").cuda().eval()
        self.no = self.tokenizer.convert_tokens_to_ids("no")
        self.yes = self.tokenizer.convert_tokens_to_ids("yes")

    def scores(self, query, documents, instruction):
        pairs = [PREFIX + f"<Instruct>: {instruction}\n<Query>: {query}\n<Document>: {doc}" + SUFFIX
                 for doc in documents]
        encoded = self.tokenizer(pairs, add_special_tokens=False, padding=False, truncation=False)
        if any(len(ids) > 4096 for ids in encoded["input_ids"]):
            raise ValueError("Token budget exceeded; no text was truncated")
        result = []
        with self.torch.inference_mode():
            for start in range(0, len(pairs), 4):
                batch = {k: v[start:start + 4] for k, v in encoded.items()}
                inputs = self.tokenizer.pad(batch, padding=True, return_tensors="pt").to(self.model.device)
                # Compute only last-position vocabulary logits (avoid large full-sequence logits).
                logits = self.model(**inputs, logits_to_keep=1).logits[:, -1, :].float()
                result.extend(logits[:, [self.no, self.yes]].softmax(dim=-1)[:, 1].cpu().tolist())
        return result


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def respond(self, status, body):
        raw = json.dumps(body, allow_nan=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        self.respond(200 if self.path == "/health" else 404,
                     {"model": MODEL, "revision": REVISION} if self.path == "/health" else {})

    def do_POST(self):
        if self.path != "/rerank":
            self.respond(404, {})
            return
        keyfile = ROOT / "deployment/embedding/api-key"
        key = keyfile.read_text().strip() if keyfile.is_file() else ""
        if not key or not secrets.compare_digest(self.headers.get("Authorization", ""), "Bearer " + key):
            self.respond(401, {"error": "Authentication required"})
            return
        try:
            size = int(self.headers.get("Content-Length", "0"))
            if not 0 < size <= 600000:
                raise ValueError()
            query, documents, instruction = validate(json.loads(self.rfile.read(size)))
        except (ValueError, TypeError):
            self.respond(400, {"error": "Invalid bounded input"})
            return
        if not LOCK.acquire(blocking=False):
            self.respond(503, {"error": "Reranker busy"})
            return
        try:
            scores = SCORER.scores(query, documents, instruction)
            self.respond(200, {"model": MODEL, "revision": REVISION, "scores": scores})
        except Exception:
            self.respond(500, {"error": "Reranking failed"})
        finally:
            LOCK.release()


if __name__ == "__main__":
    path = ROOT / "models/Qwen3-Reranker-0.6B"
    manifest = json.loads((ROOT / "runtime/reranker-model.json").read_text())
    if manifest.get("revision") != REVISION:
        raise ValueError("Pinned reranker revision not verified")
    SCORER, LOCK = Scorer(str(path)), threading.Lock()
    print(json.dumps({"ready": True, "model": MODEL, "revision": REVISION}), flush=True)
    ThreadingHTTPServer(("127.0.0.1", 30002), Handler).serve_forever()
