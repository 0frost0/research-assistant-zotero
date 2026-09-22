"""User-owned Linux services; shared model directories are never modified."""
import hashlib
import json
import os
import sqlite3
import tarfile
import urllib.request
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
OLD='D:/workspace/research_assistant'


def relocated(value):
    if isinstance(value,str):
        normalized=value.replace('\\','/')
        if normalized.startswith(OLD+'/'):return str(ROOT)+normalized[len(OLD):]
    if isinstance(value,list):return [relocated(v) for v in value]
    if isinstance(value,dict):return {k:relocated(v) for k,v in value.items()}
    return value


def paths():
    count=0
    with sqlite3.connect(ROOT/'.data/evidence.sqlite3') as con:
        sources=[r[0] for r in con.execute('SELECT source FROM documents')]
    identity=ROOT/'.data/path_identity.json'
    if not identity.exists():
        identity.write_text(json.dumps({'original_root':OLD.replace('/','\\'),'sources':sources}),encoding='utf-8')
    for path in (ROOT/'.cache').rglob('*.json'):
        try:data=json.loads(path.read_text(encoding='utf-8'))
        except (ValueError,UnicodeError):continue
        updated=relocated(data)
        if updated!=data:
            path.write_text(json.dumps(updated,ensure_ascii=False),encoding='utf-8');count+=1
    with sqlite3.connect(ROOT/'.data/evidence.sqlite3') as con:
        for table,key in [('evidence','evidence_id'),('visual_assets','visual_id')]:
            for ident,value in con.execute(f'SELECT {key},image_path FROM {table} WHERE image_path IS NOT NULL').fetchall():
                updated=relocated(value)
                if updated!=value:
                    con.execute(f'UPDATE {table} SET image_path=? WHERE {key}=?',(updated,ident));count+=1
    # Only operational paths in job payloads; note revisions are never rewritten.
    with sqlite3.connect(ROOT/'.data/reading.sqlite3') as con:
        for ident,payload in con.execute('SELECT id,payload FROM translation_jobs').fetchall():
            data=json.loads(payload);updated=relocated(data)
            if updated!=data:con.execute('UPDATE translation_jobs SET payload=? WHERE id=?',(json.dumps(updated,ensure_ascii=False),ident));count+=1
    print('Relocated operational path records:',count)


def binary():
    folder=ROOT/'runtime/qdrant'
    folder.mkdir(parents=True,exist_ok=True)
    archive=folder/'qdrant-1.19.1.tar.gz'
    sha='eef986e769d4d3e806dd2d546e1b4ecdd416211e54d34b4ed764fac7c58e1085'
    if not archive.exists():
        request=urllib.request.Request('https://github.com/qdrant/qdrant/releases/download/v1.19.1/qdrant-x86_64-unknown-linux-gnu.tar.gz',headers={'User-Agent':'research-assistant-installer'})
        temp=archive.with_suffix('.part')
        with urllib.request.urlopen(request,timeout=60) as response,temp.open('wb') as output:
            while chunk:=response.read(1024*1024):output.write(chunk)
        temp.replace(archive)
    if hashlib.sha256(archive.read_bytes()).hexdigest()!=sha:raise ValueError('Qdrant binary checksum mismatch')
    with tarfile.open(archive) as tar:
        member=next(m for m in tar.getmembers() if m.isfile() and Path(m.name).name=='qdrant')
        content=tar.extractfile(member).read()
        target=folder/'qdrant'
        if not target.exists() or hashlib.sha256(target.read_bytes()).digest()!=hashlib.sha256(content).digest():
            pending=folder/'qdrant.new'
            pending.write_bytes(content)
            pending.chmod(0o700)
            pending.replace(target)
    (folder/'qdrant').chmod(0o700)


def units():
    folder=Path.home()/'.config/systemd/user'
    folder.mkdir(parents=True,exist_ok=True)
    specs={
      'web':(f'{ROOT}/.venv/bin/python -B {ROOT}/web_app.py',{
        'RESEARCH_ASSISTANT_MINERU_PYTHON':str(ROOT/'.gpu_env/bin/python'),
        'RESEARCH_ASSISTANT_QDRANT_URL':'http://127.0.0.1:6333','NO_PROXY':'127.0.0.1,localhost',
        'PATH':str(ROOT/'runtime/ocr/usr/bin')+':/usr/local/bin:/usr/bin:/bin',
        'LD_LIBRARY_PATH':str(ROOT/'runtime/ocr/usr/lib/x86_64-linux-gnu'),
        'TESSDATA_PREFIX':str(ROOT/'runtime/ocr'),
        'HF_HUB_OFFLINE':'1','TRANSFORMERS_OFFLINE':'1'}),
      'qdrant':(f'{ROOT}/runtime/qdrant/qdrant --disable-telemetry',{
        'QDRANT__SERVICE__HOST':'127.0.0.1','QDRANT__STORAGE__STORAGE_PATH':str(ROOT/'runtime/qdrant/storage')}),
      'embedding':(f'{ROOT}/.gpu_env/bin/python -u {ROOT}/deployment/embedding/remote_embedding_service.py',{
        'CUDA_VISIBLE_DEVICES':'0','HF_HUB_OFFLINE':'1','TRANSFORMERS_OFFLINE':'1','TOKENIZERS_PARALLELISM':'false'}),
      'mineru':(f'{ROOT}/.gpu_env/bin/mineru-vllm-server --model {ROOT}/models/OpenDataLab--MinerU2.5-Pro-2605-1.2B/snapshots/master --host 127.0.0.1 --port 30000 --gpu-memory-utilization 0.35 --max-model-len 8192',{
        'CUDA_VISIBLE_DEVICES':'1','HF_HUB_OFFLINE':'1','TRANSFORMERS_OFFLINE':'1','TOKENIZERS_PARALLELISM':'false'}),
    }
    for name,(command,env) in specs.items():
        path=folder/f'research-assistant-{name}.service'
        if path.exists() and str(ROOT) not in path.read_text():raise ValueError('Existing unrelated service: '+name)
        text=f'[Unit]\nDescription=Research assistant {name}\n\n[Service]\nType=simple\nWorkingDirectory={ROOT}\nExecStart={command}\nRestart=on-failure\nRestartSec=10\nTimeoutStopSec=20\nUMask=0077\n'
        text+=''.join(f'Environment="{k}={v}"\n' for k,v in env.items())
        text+='\n[Install]\nWantedBy=default.target\n'
        path.write_text(text,encoding='utf-8')
    print('Four user services configured; no shared models modified')


if __name__=='__main__':
    if os.name=='nt':raise SystemExit('Linux only')
    paths();binary();units()
