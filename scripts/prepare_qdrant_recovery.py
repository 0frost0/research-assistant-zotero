"""Recover ONLY the stopped Qdrant storage from the verified local backup."""
import hashlib
import json
import io
import tarfile
from pathlib import Path

folder=Path(__file__).resolve().parents[1]/'output/rollback-20260914'
record=json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
count=0
with tarfile.open(folder/'project-data.tar.gz') as source,tarfile.open(folder/'qdrant-storage.tar.gz','w:gz') as target:
    for m in source:
        if not m.isfile() or not m.name.startswith('runtime/qdrant/storage/'):continue
        data=source.extractfile(m).read()
        assert hashlib.sha256(data).hexdigest()==record['files'][m.name]
        m.name=m.name[len('runtime/qdrant/'):]
        # PAX path overrides TarInfo.name when the original archive used long paths.
        m.pax_headers={k:v for k,v in m.pax_headers.items() if k not in ('path','linkpath')}
        target.addfile(m,io.BytesIO(data))
        count+=1
print(json.dumps({'storage_files':count,'sha256':hashlib.sha256((folder/'qdrant-storage.tar.gz').read_bytes()).hexdigest()}))
