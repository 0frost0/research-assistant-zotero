"""Delete ONLY explicitly verified migration targets on siat-my-laptop.

Default is a read-only plan. --apply requires a verified local recovery archive hash.
Shared cache roots, unrelated files, shared models and global linger are untouched.
"""
import hashlib
import json
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

ROOT=Path('/home/allon/research_assistant')
CACHE=Path('/home/allon/.cache')
UNITS=[Path('/home/allon/.config/systemd/user')/f'research-assistant-{s}.service' for s in ('web','qdrant','embedding','mineru')]
PACKAGES=[Path('/home/allon')/s for s in ('project.tar','manifest.json','model-font-assets.tar','research-source-final.tar')]


def sha(path):
    h=hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda:f.read(1024*1024),b''):h.update(block)
    return h.hexdigest()


def plan():
    assert ROOT.resolve()==ROOT and ROOT.is_dir() and not ROOT.is_symlink()
    record=json.loads((ROOT/'.rollback-20260914.json').read_text())
    assert sha(ROOT/'.rollback-20260914.tar.gz')==record['archive_sha256']
    for p in UNITS:
        assert p.is_file() and not p.is_symlink() and str(ROOT) in p.read_text()
        state=subprocess.run(['systemctl','--user','is-active',p.name],capture_output=True,text=True).stdout.strip()
        assert state in ('inactive','failed'),(p.name,state)
    original=json.loads(Path('/home/allon/manifest.json').read_text())
    assert sha(Path('/home/allon/project.tar'))==original['archive_hash']
    cached=[];preserved=[]
    with tarfile.open('/home/allon/model-font-assets.tar') as package:
        for m in package:
            if not m.isfile():continue
            rel=Path(m.name)
            assert not rel.is_absolute() and '..' not in rel.parts
            assert rel.parts[0] in ('babeldoc','huggingface')
            p=CACHE/rel
            if not p.exists():continue
            assert p.resolve()==p and p.is_file(),str(p)
            expected=hashlib.sha256(package.extractfile(m).read()).hexdigest()
            if sha(p)==expected:cached.append({'path':str(p),'sha256':expected,'bytes':p.stat().st_size})
            else:preserved.append(str(p))
    return {'project':str(ROOT),'archive_sha256':record['archive_sha256'],
            'units':[str(p) for p in UNITS],
            'packages':[{'path':str(p),'sha256':sha(p),'bytes':p.stat().st_size} for p in PACKAGES],
            'cache_files':cached,'preserved_changed_cache_files':preserved}


report=plan()
if '--apply' not in sys.argv:
    print(json.dumps(report,ensure_ascii=False));sys.exit(0)
assert sys.argv[-1]==report['archive_sha256'],'Local backup acknowledgement missing'
subprocess.run(['systemctl','--user','disable',*[p.name for p in UNITS]],check=True)
for p in UNITS:
    assert not p.is_symlink() and str(ROOT) in p.read_text()
    p.unlink()
subprocess.run(['systemctl','--user','daemon-reload'],check=True)
for item in report['cache_files']:
    p=Path(item['path']);assert p.resolve()==p and sha(p)==item['sha256'];p.unlink()
    parent=p.parent
    while parent!=CACHE and parent.is_relative_to(CACHE):
        try:parent.rmdir()
        except OSError:break
        parent=parent.parent
for item in report['packages']:
    p=Path(item['path']);assert p.resolve()==p and sha(p)==item['sha256'];p.unlink()
# Verified exact project created in this migration; never follow a substituted root.
assert ROOT.resolve()==ROOT and not ROOT.is_symlink()
shutil.rmtree(ROOT)
report['deleted']=True
report['preserved_shared_roots']=['/storage/models','/home/allon/.cache/pip','/home/allon/.cache/torch','/home/allon/.cache/vllm','/home/allon/.nv','/home/allon/.triton']
print(json.dumps(report,ensure_ascii=False))
