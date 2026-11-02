"""Offline service settings and embedding protocol checks; never calls a model."""
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock

from starlette.applications import Starlette
from starlette.testclient import TestClient
from research_assistant.web.knowledge_api import KnowledgeAPI
from research_assistant.core.service_context import service_context, llm_config
from research_assistant.retrieval.embeddings.api_embeddings import QwenAPIEmbeddings


class ServiceWiringTests(unittest.TestCase):
    def test_config_roundtrip_redacts_secret_and_drives_request(self):
        with tempfile.TemporaryDirectory() as directory:
            api = KnowledgeAPI(Path(directory), token="test-token", backend="tfidf")
            config = {"llm": {"base_url": "https://example.invalid/v1", "model": "test", "api_key": "secret-llm"},
                      "embedding": {"base_url": "http://127.0.0.1:30001/v1", "model": "test", "api_key": "secret-embed", "dimension": 2},
                      "mineru": {"mode": "local", "python": str(Path(directory) / "python.exe"), "auto_parse": False}}
            headers = {"Authorization": "Bearer test-token", "X-Research-Connection": "test-profile"}
            with TestClient(Starlette(routes=api.routes())) as client:
                response = client.post("/api/knowledge/services", headers=headers, json=config)
                self.assertEqual(response.status_code, 200, response.text)
                self.assertNotIn("secret-", response.text)
                self.assertTrue(client.get("/api/knowledge/status", headers=headers).json()["generation"])
                self.assertEqual(client.get("/api/knowledge/parsing", headers=headers).json(), {"jobs": []})
                for section in ("llm", "embedding"):
                    config[section]["api_key"] = ""
                self.assertEqual(client.post("/api/knowledge/services", headers=headers, json=config).status_code, 200)
                self.assertEqual(api._service_configs.get("test-profile")["llm"]["api_key"], "secret-llm")
                config['mineru'] = {'mode': 'disabled', 'url': '', 'auto_parse': True}
                response = client.post('/api/knowledge/services', headers=headers, json=config)
                self.assertEqual(response.status_code, 400, response.text)
            self.assertIsNone(service_context.get())

    def test_messages_protocol_and_vector_validation(self):
        adapter = QwenAPIEmbeddings({"base_url": "http://127.0.0.1:30001/v1", "model": "test", "api_key": "secret", "dimension": 2})
        adapter.opener = Mock()
        adapter.opener.open.return_value = io.BytesIO(json.dumps({"data": [{"embedding": [3, 4]}]}).encode())
        self.assertEqual(adapter.embed_texts(["中文科研问题"]), [[0.6, 0.8]])
        request = adapter.opener.open.call_args.args[0]
        self.assertEqual(request.full_url, "http://127.0.0.1:30001/v1/embeddings")
        payload = json.loads(request.data)
        self.assertEqual(payload["messages"][1]["content"][0]["text"], "中文科研问题")
        adapter.opener.open.return_value = io.BytesIO(b'{"data":[{"embedding":[1]}]}')
        with self.assertRaisesRegex(ValueError, "dimension"):
            adapter.embed_query("test")

    def test_profile_does_not_change_other_context(self):
        original = llm_config()
        token = service_context.set({"connection": "test", "config": {"llm": {"model": "profile", "base_url": "https://example.invalid", "api_key": "secret"}}})
        try:
            self.assertEqual(llm_config()["model"], "profile")
        finally:
            service_context.reset(token)
        self.assertEqual(llm_config(), original)
