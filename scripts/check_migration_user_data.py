"""Read-only hashes for cutover: private records are never printed."""
import hashlib
import json
import sqlite3
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]


def normalized(value):
    if isinstance(value,bytes):return {'bytes':hashlib.sha256(value).hexdigest()}
    if isinstance(value,str):
        try:return normalized(json.loads(value))
        except (ValueError,TypeError):pass
        for prefix in (str(ROOT),'D:\\workspace\\research_assistant','D:/workspace/research_assistant','/home/allon/research_assistant'):
            value=value.replace(prefix,'<ROOT>')
        return value.replace('\\','/')
    if isinstance(value,list):return [normalized(v) for v in value]
    if isinstance(value,dict):return {k:normalized(v) for k,v in value.items()}
    return value


def main():
    report={}
    for name in ('reading','projects','comparisons','evaluations','tables'):
        file=ROOT/'.data'/(name+'.sqlite3')
        with sqlite3.connect(file.as_uri()+'?mode=ro',uri=True) as con:
            assert con.execute('PRAGMA quick_check').fetchone()[0]=='ok'
            tables=[r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' AND name NOT LIKE 'alignment_%'")]
            report[name]={}
            for table in tables:
                rows=con.execute('SELECT * FROM "'+table.replace('"','""')+'"').fetchall()
                serialized=sorted(json.dumps(normalized(list(r)),ensure_ascii=False,sort_keys=True) for r in rows)
                report[name][table]={'count':len(rows),'sha256':hashlib.sha256('\n'.join(serialized).encode()).hexdigest()}
    report['library']={p.relative_to(ROOT/'library').as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in (ROOT/'library').rglob('*') if p.is_file()}
    print(json.dumps(report,ensure_ascii=False,sort_keys=True))


if __name__=='__main__':main()
