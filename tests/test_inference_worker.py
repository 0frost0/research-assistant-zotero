"""Private worker boundary tests; no external models or real library."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from starlette.testclient import TestClient
from research_assistant.web.inference_worker import create_worker


class InferenceWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_disconnect_cancels_model_request(self):
        with tempfile.TemporaryDirectory() as root:
            app = create_worker(Path(root), "worker-token")
            started, cancelled = asyncio.Event(), asyncio.Event()

            class Caller:
                async def __call__(self, *args):
                    started.set()
                    try:
                        await asyncio.Event().wait()
                    finally:
                        cancelled.set()

            body = json.dumps({"connection": "", "role": "answer", "payload": {}}).encode()
            received_body = False

            async def receive():
                nonlocal received_body
                if not received_body:
                    received_body = True
                    return {"type": "http.request", "body": body, "more_body": False}
                await started.wait()
                return {"type": "http.disconnect"}

            messages = []

            async def send(message):
                messages.append(message)

            scope = {"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                     "method": "POST", "scheme": "http", "path": "/internal/model",
                     "raw_path": b"/internal/model", "query_string": b"",
                     "headers": [(b"authorization", b"Bearer worker-token")],
                     "server": ("127.0.0.1", 8778), "client": ("127.0.0.1", 1)}
            with patch("research_assistant.web.inference_worker.ModelCaller", Caller):
                await asyncio.wait_for(app(scope, receive, send), 3)
            self.assertTrue(cancelled.is_set())
            self.assertEqual(messages[0]["status"], 499)

    async def test_worker_auth_and_no_product_routes(self):
        with tempfile.TemporaryDirectory() as root:
            app = create_worker(Path(root), "worker-token")
            with TestClient(app) as client:
                response = client.post("/internal/model", json={"connection": "", "payload": {}})
                self.assertEqual(response.status_code, 401)
                self.assertEqual(client.get("/api/knowledge/jobs").status_code, 404)
