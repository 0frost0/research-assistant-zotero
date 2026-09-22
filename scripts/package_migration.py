"""Private migration snapshot. SQLite uses backup API, never raw live WAL copies."""
import hashlib
import json
import os
import sqlite3
import tarfile
import tempfile
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
SKIP={'.alignment_env','.translation_env','.benchmark_envs','.venv','.embedding_deps',
      '.git','.playwright-cli','.ruff_cache','__pycache__'}


def main():
    stage=Path(tempfile.mkdtemp(prefix='research_migration_'))
    archive=stage/'project.tar'
    files={}; databases={}
    with tarfile.open(archive,'w') as tar:
        for path in ROOT.rglob('*'):
            relative=path.relative_to(ROOT)
            if not path.is_file() or any(p in SKIP for p in relative.parts) or path.suffix in ('.pid','.pyc') or path.name.endswith(('-wal','-shm')):
                continue
            source=path
            if path.suffix in ('.sqlite3','.sqlite','.db'):
                backup=stage/'databases'/relative
                backup.parent.mkdir(parents=True,exist_ok=True)
                with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as src, sqlite3.connect(backup) as dst:
                    src.backup(dst)
                    if dst.execute('PRAGMA quick_check').fetchone()[0]!='ok':raise ValueError('Invalid database snapshot')
                    tables=[r[0] for r in dst.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
                    databases[str(relative)]= {t:dst.execute('SELECT COUNT(*) FROM "'+t.replace('"','""')+'"').fetchone()[0] for t in tables}
                source=backup
            files[relative.as_posix()]=hashlib.sha256(source.read_bytes()).hexdigest()
            info=tar.gettarinfo(str(source),arcname=relative.as_posix())
            info.mode=0o600
            with source.open('rb') as stream:tar.addfile(info,stream)
        # Existing model credentials are needed by the migrated app, not displayed.
        if '.env' not in files:
            env=next((p for p in (ROOT.parent/'.env',ROOT.parent/'langchain/.env') if p.exists()),None)
            if env:
                info=tar.gettarinfo(str(env),arcname='.env');info.mode=0o600
                with env.open('rb') as stream:tar.addfile(info,stream)
                files['.env']=hashlib.sha256(env.read_bytes()).hexdigest()
    manifest=stage/'manifest.json'
    manifest.write_text(json.dumps({'files':files,'databases':databases,'archive_hash':hashlib.sha256(archive.read_bytes()).hexdigest()},ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps({'directory':str(stage),'files':len(files),'bytes':archive.stat().st_size,'databases':databases},ensure_ascii=True))


if __name__=='__main__':main()
