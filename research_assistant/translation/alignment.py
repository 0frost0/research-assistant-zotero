"""Read-only bilingual alignment, isolated CPU process and rebuildable cache.

Alignment is model-generated navigation assistance, never a confirmed note link.
"""
import json
import os
import subprocess
import threading
from pathlib import Path
from research_assistant.memory.store import digest, dump, now

MODEL = 'cstr/awesome-align-onnx-int8'
REVISION = 'e803a2f501474044099b539160aaca461976ecd0'
ALGORITHM = 'bilingual-sentence-v3-portable'
TIMEOUT = 75


class AlignmentService:
    def __init__(self, store, runtime_root=None, runner=None):
        self.store = store
        self.root = Path(runtime_root or store.root)
        self.python = self.root / ('.alignment_env/Scripts/python.exe' if os.name == 'nt' else '.alignment_env/bin/python')
        self.model = self.root / '.alignment_models' / REVISION
        self.runner = runner or self._run
        self.lock = threading.Lock()
        with store.transaction() as con:
            con.execute('CREATE TABLE IF NOT EXISTS alignment_schema(version INTEGER PRIMARY KEY)')
            version = con.execute('SELECT MAX(version) FROM alignment_schema').fetchone()[0]
            if version and version > 1:
                raise ValueError('对齐数据库版本较新，请升级应用。')
            con.execute('INSERT OR IGNORE INTO alignment_schema VALUES(1)')
            con.execute('CREATE TABLE IF NOT EXISTS alignment_results(cache_key TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at TEXT NOT NULL)')

    def status(self):
        ready = self.python.is_file() and (self.model / 'manifest.json').is_file() and (self.model / 'portable.json').is_file()
        return {'available': ready, 'model': MODEL, 'revision': REVISION, 'algorithm': ALGORITHM,
                'local_only': True, 'message': '本地 CPU 完整句对齐；自动结果需核对。' if ready else '请运行 scripts/install_alignment.ps1 安装本地对齐模型。'}

    def _run(self, payload):
        completed = subprocess.run(
            [str(self.python), '-B', str(self.root / 'research_assistant/translation/alignment_worker.py')],
            input=dump(payload), capture_output=True, text=True, encoding='utf-8',
            timeout=TIMEOUT, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
        if completed.returncode:
            raise ValueError('本地对齐运行失败，请检查安装；中文选区与笔记未受影响。')
        try:
            return json.loads(completed.stdout)
        except (ValueError, TypeError):
            raise ValueError('本地对齐返回异常，未显示推测选区。') from None

    def align(self, data):
        paper_id = str(data.get('paper_id', ''))
        anchor = self.store.validate_anchor(data.get('anchor'), paper_id)
        translated = self.store.artifact(anchor['artifact_id'])
        if translated['kind'] != 'translation' or not anchor['rects'] or len(anchor['excerpt']) > 2000:
            raise ValueError('请在中文译文上选择词句，一次不超过 2000 字符。')
        original = self.store.artifact(translated['parent_id'])
        if original['paper_id'] != paper_id or original['kind'] != 'original':
            raise ValueError('原件关联无效。')
        # Verify actual files even on cache hits; never reuse a stale artifact.
        original_path = self.store.artifact_path(original['id'])
        translated_path = self.store.artifact_path(translated['id'])
        identity = {'algorithm': ALGORITHM, 'model': MODEL, 'revision': REVISION,
                    'original_hash': original['hash'], 'translated_hash': translated['hash'],
                    'anchor': anchor}
        key = digest(dump(identity).encode())
        with self.store.read() as con:
            row = con.execute('SELECT payload FROM alignment_results WHERE cache_key=?', (key,)).fetchone()
        if row:
            try:
                result = json.loads(row[0])
                if result['status'] in ('aligned', 'unmatched'):
                    self._validate_result(result, original, paper_id)
                    return dict(result, cached=True)
            except (ValueError, KeyError, TypeError):
                pass  # Cache is disposable. Never modify note/artifact history.
        if not self.status()['available']:
            return {'status': 'unavailable', 'message': self.status()['message']}
        if not self.lock.acquire(blocking=False):
            return {'status': 'busy', 'message': '本地对齐正在处理上一选区，稍后重新选择即可；不会排队堆积。'}
        try:
            result = self.runner({'original_path': str(original_path), 'translation_path': str(translated_path),
                                  'model_dir': str(self.model), 'cache_path': str(self.store.root / '.data/alignment_embeddings.sqlite3'),
                                  'anchor': anchor, 'original': original, 'identity': identity})
            self._validate_result(result, original, paper_id)
            result.update(model=MODEL, revision=REVISION, algorithm=ALGORITHM,
                          generated_at=now(), confirmed=False, cached=False, local_only=True)
            if result['status'] in ('aligned', 'unmatched'):
                try:
                    with self.store.transaction() as con:
                        con.execute('INSERT OR REPLACE INTO alignment_results VALUES(?,?,?)', (key, dump(result), now()))
                except Exception:
                    result['cache_warning'] = '缓存写入失败，本次对齐仍可查看。'
            return result
        except subprocess.TimeoutExpired:
            return {'status': 'timeout', 'message': '本地对齐超过 75 秒，已终止本次进程；不会自动重试。'}
        finally:
            self.lock.release()

    def _validate_result(self, result, original, paper_id):
        if result.get('status') not in ('aligned', 'unmatched', 'unsupported'):
            raise ValueError('本地对齐状态异常。')
        if result['status'] == 'aligned':
            anchors = result.get('anchors', [])
            if result.get('alignment_unit') != 'sentence' or len(anchors) != 1:
                raise ValueError('本地对齐范围异常。')
            for a in anchors:
                valid = self.store.validate_anchor(a, paper_id)
                if valid['artifact_id'] != original['id'] or valid['artifact_hash'] != original['hash']:
                    raise ValueError('对齐结果原件版本不匹配。')
