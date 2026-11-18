"""Provision this user's loopback Qwen API without printing credentials.

Reads the existing key over SSH, updates only embedding settings, then runs
synthetic text/image checks through the real local adapter. No corpus indexing.
"""
import argparse
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from research_assistant.core.service_config import ServiceConfigStore
from research_assistant.retrieval.embeddings.api_embeddings import QwenAPIEmbeddings


def main():
    parser = argparse.ArgumentParser()
    connection_source = parser.add_mutually_exclusive_group(required=True)
    connection_source.add_argument('--prefs', type=Path)
    connection_source.add_argument('--connection')
    parser.add_argument('--ssh-host', default='research-web')
    parser.add_argument('--remote-root', default='/home/ubuntu/research-assistant/app')
    args = parser.parse_args()
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.@-]*', args.ssh_host):
        raise ValueError('Use an SSH config alias')
    if not args.remote_root.startswith('/') or '\n' in args.remote_root or '\r' in args.remote_root:
        raise ValueError('Remote root must be an absolute POSIX path')
    if args.connection:
        connection = args.connection
    else:
        prefs = args.prefs.read_text(encoding='utf-8')
        match = re.search(r'user_pref\("extensions\.researchAssistant\.connection",\s*("(?:[^"\\]|\\.)*")\);', prefs)
        if not match:
            raise RuntimeError('Zotero connection profile missing')
        connection = json.loads(match[1])
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    for attempt in range(20):
        try:
            with opener.open('http://127.0.0.1:30001/health', timeout=3) as response:
                health = json.load(response)
            break
        except OSError:
            if attempt == 19:
                raise RuntimeError('Qwen loopback tunnel/service unavailable') from None
            time.sleep(2)
    key = subprocess.check_output([
        'ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', args.ssh_host,
        'cat ' + shlex.quote(args.remote_root.rstrip('/') + '/deployment/embedding/api-key')], timeout=20, text=True).strip()
    if not key:
        raise RuntimeError('Remote key missing')
    config = {'base_url': 'http://127.0.0.1:30001/v1', 'model': health['model_id'],
              'dimension': health['dimension'], 'revision': health['fingerprint'], 'api_key': key}
    adapter = QwenAPIEmbeddings(config)
    vectors = adapter.embed_texts(['白色背景上有一个红色矩形。', 'A red rectangle on a white background.'])
    from PIL import Image, ImageDraw
    temporary_root = ROOT / '.cache/configure-qwen'
    temporary_root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=temporary_root) as directory:
        path = Path(directory) / 'synthetic.png'
        image = Image.new('RGB', (128, 128), 'white')
        ImageDraw.Draw(image).rectangle((24, 32, 104, 96), fill='red')
        image.save(path)
        vectors += adapter.embed_images([path], ['A red rectangle.'])
        vectors += [adapter.embed_query('红色矩形', path)]
    # Save only after successful API checks; preserve LLM/MinerU settings.
    store = ServiceConfigStore(ROOT)
    with store.lock:
        all_configs = store._read()
        current = all_configs.setdefault(connection, {})
        current['embedding'] = config
        store._write(all_configs)
    print(json.dumps({'configured': True, 'model': config['model'],
                      'endpoint': config['base_url'], 'synthetic_vectors': len(vectors),
                      'dimensions': [len(v) for v in vectors],
                      'llm_and_mineru_preserved': True}))


if __name__ == '__main__':
    main()
