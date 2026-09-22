"""Restore verified storage into the dedicated rollback Qdrant, never others."""
import os
import signal
import tarfile
import time
from pathlib import Path

ROOT=Path('/home/dingone/dst/research_assistant/run/qdrant-rollback-20260914')
assert ROOT.resolve()==ROOT and not ROOT.is_symlink()
pid=int((ROOT/'service.pid').read_text())
proc=Path(f'/proc/{pid}')
if proc.exists():
    assert str(ROOT/'qdrant').encode() in (proc/'cmdline').read_bytes().split(b'\0')
    assert (proc/'cwd').resolve()==ROOT
    os.kill(pid,signal.SIGTERM)
    for _ in range(100):
        if not proc.exists() or (proc/'stat').read_text().split()[2]=='Z':break
        time.sleep(.1)
    else:raise SystemExit('Own Qdrant did not stop; storage untouched')
archive=ROOT/'qdrant-storage.tar.gz'
with tarfile.open(archive) as package:
    members=package.getmembers()
    for m in members:
        p=Path(m.name)
        assert not p.is_absolute() and '..' not in p.parts and p.parts[0]=='storage' and m.isfile()
    previous=ROOT/'storage-incomplete'
    assert not previous.exists()
    if (ROOT/'storage').exists():(ROOT/'storage').rename(previous)
    for m in members:
        p=ROOT/m.name;p.parent.mkdir(parents=True,exist_ok=True)
        with package.extractfile(m) as src,p.open('wb') as dst:
            import shutil
            shutil.copyfileobj(src,dst)
print('Stopped-storage backup restored; incomplete rebuild preserved')
