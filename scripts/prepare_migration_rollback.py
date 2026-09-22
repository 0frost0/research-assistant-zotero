"""Run on new server: stop ONLY verified migration services, back up non-model data."""
import hashlib
import json
import subprocess
import tarfile
from pathlib import Path

ROOT=Path('/home/allon/research_assistant')
assert ROOT.resolve()==ROOT and not ROOT.is_symlink()
UNITS=[f'research-assistant-{s}.service' for s in ('web','qdrant','embedding','mineru')]
for name in UNITS:
    p=Path('/home/allon/.config/systemd/user')/name
    assert p.is_file() and not p.is_symlink() and str(ROOT) in p.read_text()
subprocess.run(['systemctl','--user','stop',*UNITS],check=True)
SKIP={'.venv','.gpu_env','.translation_env','.alignment_env','.alignment_models','.benchmark_models',
      'models','__pycache__','.rollback-20260914.tar.gz','.rollback-20260914.json'}
archive=ROOT/'.rollback-20260914.tar.gz'
manifest={}
with tarfile.open(archive,'w:gz') as out:
    for p in ROOT.rglob('*'):
        rel=p.relative_to(ROOT)
        if any(s in SKIP for s in rel.parts) or p.is_symlink() or not p.is_file():continue
        if rel.parts[:2]==('runtime','ocr') or rel.parts[:2]==('runtime','qdrant') and rel.parts[2:3]!=('storage',):continue
        manifest[rel.as_posix()]=hashlib.sha256(p.read_bytes()).hexdigest()
        out.add(p,arcname=rel.as_posix(),recursive=False)
archive.chmod(0o600)
record={'archive_sha256':hashlib.sha256(archive.read_bytes()).hexdigest(),'files':manifest}
(ROOT/'.rollback-20260914.json').write_text(json.dumps(record,ensure_ascii=False))
print(json.dumps({'files':len(manifest),'bytes':archive.stat().st_size,'sha256':record['archive_sha256']}))
