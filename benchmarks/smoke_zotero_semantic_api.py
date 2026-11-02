"""Real embedding + Qdrant smoke test with an isolated synthetic knowledge API.

Does not copy papers, call an LLM, or write to the production knowledge DB.
"""
import json
from pathlib import Path
import re
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tests.test_reading_memory import pdf
from starlette.applications import Starlette
from starlette.testclient import TestClient
from research_assistant.core.service_config import ServiceConfigStore
from research_assistant.web.knowledge_api import KnowledgeAPI
from research_assistant.knowledge.store import digest


def main():
    prefs = Path(sys.argv[1]).read_text(encoding='utf-8')
    match = re.search(r'user_pref\("extensions\.researchAssistant\.connection",\s*(".*?")\);', prefs)
    config = ServiceConfigStore(ROOT).get(json.loads(match[1]))
    assert config.get('embedding'), 'Embedding configuration missing'
    with tempfile.TemporaryDirectory(prefix='zotero-semantic-smoke-') as directory:
        root = Path(directory)
        api = KnowledgeAPI(root, token='synthetic-token', backend='multimodal')
        with api._service_configs.lock:
            api._service_configs._write({'smoke': {'embedding': config['embedding']}})
        pdf(root / 'synthetic.pdf')
        content = (root / 'synthetic.pdf').read_bytes()
        sha = digest(content)
        api.store.put_object(sha, content)
        base = api.store.create({'connection': 'smoke', 'library': 'user:0', 'collection': 'SYNTHETIC', 'name': 'Synthetic only'})
        api.store.sync(base['id'], {'complete': True, 'include_children': True, 'name': 'Synthetic only', 'expected_revision': base['revision'],
                                  'entities': [{'key': 'SMOKE001', 'kind': 'document', 'title': 'Synthetic sensor', 'hash': sha}]})
        try:
            with TestClient(Starlette(routes=api.routes())) as client:
                response = client.post('/api/knowledge/search',
                    headers={'Authorization': 'Bearer synthetic-token', 'X-Research-Connection': 'smoke'},
                    json={'kb_ids': [base['id']], 'scope': 'original', 'query': '科研证据 scientific evidence'})
                result = response.json()
                deadline = time.monotonic() + 60
                while response.status_code == 202 and time.monotonic() < deadline:
                    time.sleep(0.5)
                    response = client.post('/api/knowledge/search',
                        headers={'Authorization': 'Bearer synthetic-token', 'X-Research-Connection': 'smoke'},
                        json={'kb_ids': [base['id']], 'scope': 'original', 'query': '科研证据 scientific evidence'})
                    result = response.json()
                assert response.status_code == 200, result
                assert result['backend'] == 'multimodal', result.get('warning')
                assert any('scientific evidence' in r['quote'].lower() for r in result['results']), 'Expected evidence not retrieved'
                print(json.dumps({'http': response.status_code, 'backend': result['backend'],
                                  'hits': len(result['results']), 'expected_evidence': True,
                                  'source_jump': result['results'][0]['url'].startswith('zotero://open-pdf/'),
                                  'warning': result['warning'], 'llm_calls': 0}))
        finally:
            index = api.service.index
            if index and index.unified_index:
                unified = index.unified_index
                # This collection belongs solely to the random temporary root.
                unified.client.delete_collection(unified.collection_name)
                unified.client.close()


if __name__ == '__main__':
    main()
