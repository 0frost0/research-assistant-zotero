"""Old server isolated Qdrant program; no model downloads or environment updates."""
import hashlib
import os
import socket
import subprocess
import tarfile
import urllib.request
from pathlib import Path

ROOT=Path('/home/dingone/dst/research_assistant/run/qdrant-rollback-20260914')
ROOT.mkdir(exist_ok=True)
archive=ROOT/'qdrant-musl.tar.gz'
url='https://github.com/qdrant/qdrant/releases/download/v1.19.1/qdrant-x86_64-unknown-linux-musl.tar.gz'
sha='70a40529e2ebe0a2787d574d3a2e28437cfe94f26f24fa419f6ac57b4ae817c9'
if not archive.exists():urllib.request.urlretrieve(url,archive)
assert hashlib.sha256(archive.read_bytes()).hexdigest()==sha
with tarfile.open(archive) as package:
    member=next(m for m in package if m.isfile() and Path(m.name).name=='qdrant')
    binary=ROOT/'qdrant';binary.write_bytes(package.extractfile(member).read());binary.chmod(0o700)
with socket.socket() as s:
    if s.connect_ex(('127.0.0.1',6333))==0:raise SystemExit('Port 6333 occupied; no other service will be replaced')
with (ROOT/'service.log').open('ab') as log:
    env=dict(os.environ,QDRANT__SERVICE__HOST='127.0.0.1',QDRANT__STORAGE__STORAGE_PATH=str(ROOT/'storage'))
    p=subprocess.Popen([str(binary),'--disable-telemetry'],cwd=ROOT,env=env,stdout=log,stderr=log,stdin=subprocess.DEVNULL,start_new_session=True)
(ROOT/'service.pid').write_text(str(p.pid))
print('Project Qdrant PID:',p.pid)
