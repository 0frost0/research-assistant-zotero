"""Run on the destination before startup. Does not print private content."""
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

root=Path(__file__).resolve().parents[1]
manifest=json.loads(Path(sys.argv[1]).read_text(encoding='utf-8'))
bad=[]
for relative,sha in manifest['files'].items():
    path=root/relative
    if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest()!=sha:bad.append(relative)
print(json.dumps({'verified_files':len(manifest['files'])-len(bad),'mismatches':bad},ensure_ascii=True))
if bad:raise SystemExit(1)
for relative,counts in manifest['databases'].items():
    with sqlite3.connect((root/relative.replace('\\','/')).as_uri()+'?mode=ro',uri=True) as con:
        assert con.execute('PRAGMA quick_check').fetchone()[0]=='ok'
        for table,count in counts.items():
            assert con.execute('SELECT COUNT(*) FROM "'+table.replace('"','""')+'"').fetchone()[0]==count
print('SQLite counts and integrity match')
