"""Read-only verification before deleting the new-server migration."""
import hashlib
import json
import tarfile
from pathlib import Path

folder=Path(__file__).resolve().parents[1]/'output/rollback-20260914'
manifest=json.loads((folder/'manifest.json').read_text(encoding='utf-8'))
archive=folder/'project-data.tar.gz'
assert hashlib.sha256(archive.read_bytes()).hexdigest()==manifest['archive_sha256']
seen=set()
with tarfile.open(archive) as package:
    for m in package:
        if not m.isfile():continue
        assert hashlib.sha256(package.extractfile(m).read()).hexdigest()==manifest['files'][m.name],m.name
        seen.add(m.name)
assert seen==set(manifest['files'])
print(json.dumps({'verified_files':len(seen),'archive_sha256':manifest['archive_sha256']}))
