"""Rehearse a knowledge backup on a new private directory, without model calls.

The source database is opened read-only. SQLite's backup API fixes the point
in time; comparisons use that snapshot, not a changing live database.
"""
import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import threading
from types import SimpleNamespace
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from research_assistant.knowledge.backup import export_backup, restore_backup, TABLES, OPTIONAL_TABLES
from research_assistant.knowledge.store import digest, now


def verify(root: Path, output: Path):
    home = root.resolve() / ".data/zotero"
    database = home / "knowledge.sqlite3"
    if not database.is_file():
        raise FileNotFoundError("知识库不存在；不会创建空库来替代真实数据。")
    output = output.resolve()
    if output.is_relative_to(home):
        raise ValueError("演练目录必须在运行数据目录之外。")
    output.mkdir(mode=0o700, parents=True, exist_ok=False)
    report = {"status": "failed", "created_at": now(), "model_calls": 0,
              "scope": "zotero_knowledge_only", "output": str(output)}
    try:
        # Do not instantiate KnowledgeStore: its constructor initializes schema.
        source = SimpleNamespace(path=database, objects=home / "objects", lock=threading.RLock())
        archive_path = output / "knowledge.zip"
        export_backup(source, archive_path)
        restored = restore_backup(archive_path, output / "restored")
        recovered = restored / ".data/zotero"
        with zipfile.ZipFile(archive_path) as archive:
            manifest = json.loads(archive.read("manifest.json"))
            db_hash = digest((recovered / "knowledge.sqlite3").read_bytes())
            if db_hash != manifest["files"]["knowledge.sqlite3"]:
                raise ValueError("恢复的数据库与备份快照不同。")
            object_count = 0
            for name, expected in manifest["files"].items():
                if name.startswith("objects/"):
                    if digest((recovered / name).read_bytes()) != expected:
                        raise ValueError("恢复的 PDF 与备份快照不同。")
                    object_count += 1
            forbidden = {".env", "connection-token", "qa_jobs.sqlite3"}
            if any(Path(name).name in forbidden for name in manifest["files"]):
                raise ValueError("知识库备份包含范围外文件。")
        with closing(sqlite3.connect((recovered / "knowledge.sqlite3").as_uri() + "?mode=ro", uri=True)) as con:
            present = {row[0] for row in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            counts = {table: con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
                      for table in sorted(present & (TABLES | OPTIONAL_TABLES))}
        report.update(status="passed", archive_sha256=digest(archive_path.read_bytes()),
                      database_snapshot_sha256=db_hash, table_counts=counts,
                      verified_pdf_objects=object_count, exact_database_match=True,
                      credentials_included=False)
        return report
    except Exception as exc:
        report["error_type"] = type(exc).__name__
        raise
    finally:
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main():
    parser = argparse.ArgumentParser(description="只读导出并在全新目录演练 Zotero 知识库恢复")
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.root, args.output), ensure_ascii=False))


if __name__ == "__main__":
    main()
