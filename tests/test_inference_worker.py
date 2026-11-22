"""Private worker boundary tests; no external models or real library."""
import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from starlette.testclient import TestClient
from research_assistant.knowledge.service import KnowledgeService
from research_assistant.knowledge.store import KnowledgeStore
from research_assistant.web.inference_worker import create_worker


class InferenceWorkerTests(unittest.IsolatedAsyncioTestCase):
    async def test_search_accepts_connection_without_saved_services(self):
        with tempfile.TemporaryDirectory() as root:
            app = create_worker(Path(root), "worker-token")
            with TestClient(app, headers={"Authorization": "Bearer worker-token"}) as client:
                with patch.object(KnowledgeService, "search", return_value={"results": []}) as search:
                    query = {"query": "note", "kb_ids": ["sample"], "scope": "notes"}
                    response = client.post("/internal/search", json={"connection": "new", "payload": query})
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json(), {"results": []})
                    search.assert_called_once_with(query, expected_revisions=None)
                response = client.post("/internal/model", json={"connection": "new", "payload": {}})
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json(), {"error": "ValueError"})

    async def test_search_rejects_stale_expected_revision_before_indexing(self):
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            store = KnowledgeStore(root)
            base = store.create({"connection": "fixture", "library": "user:0", "collection": "A", "name": "A"})
            store.save_note(base["id"], {"body": "A synthetic sample note"})
            app = create_worker(root, "worker-token")
            with TestClient(app, headers={"Authorization": "Bearer worker-token"}) as client:
                query = {"query": "note", "kb_ids": [base["id"]], "scope": "notes"}
                with patch.object(KnowledgeService, "get_index") as index:
                    index.return_value = None
                    response = client.post("/internal/search", json={
                        "connection": "", "payload": query, "expected_revisions": {base["id"]: base["revision"]},
                    })
                    self.assertEqual(response.status_code, 409)
                    self.assertEqual(response.json(), {"error": "revision_conflict"})
                    index.assert_not_called()
                    current = store.base(base["id"])["revision"]
                    response = client.post("/internal/search", json={
                        "connection": "", "payload": query, "expected_revisions": {base["id"]: current},
                    })
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json()["revisions"], {base["id"]: current})
                    index.assert_called_once()

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
