"""Consistent, credential-free workspace backups. Restore only into a new root."""
from contextlib import closing
import json
from pathlib import Path
import shutil
import sqlite3
import tempfile
import zipfile

from research_assistant.knowledge.store import digest, encoded, now

TABLES = {"kb_schema", "knowledge_bases", "objects", "entities", "entity_revisions",
          "memberships", "local_notes", "note_revisions"}
# Created lazily by record_qa_run. Older backups legitimately lack telemetry;
# newer backups must remain restorable after the first successful question.
OPTIONAL_TABLES = {"qa_runs", "parsing_jobs"}


def export_backup(store, destination):
    destination = Path(destination)
    # Stage on the destination filesystem so publication is an atomic rename.
    # A failed ZIP write must not truncate an earlier usable backup.
    with tempfile.TemporaryDirectory(prefix=".research-backup-", dir=destination.parent) as temporary, store.lock:
        database = Path(temporary) / "knowledge.sqlite3"
        with closing(sqlite3.connect(store.path.resolve().as_uri() + "?mode=ro", uri=True)) as source, closing(sqlite3.connect(database)) as target:
            source.backup(target)
            # Rebuildable parser cache is deliberately excluded. Strip host-local
            # image paths from the backup copy, never from the live database.
            for sha, raw in target.execute("SELECT hash,report FROM objects").fetchall():
                report = json.loads(raw)
                report.pop("mineru", None)
                target.execute("UPDATE objects SET report=? WHERE hash=?", (encoded(report), sha))
            if target.execute("SELECT 1 FROM sqlite_master WHERE name='parsing_jobs'").fetchone():
                target.execute("DELETE FROM parsing_jobs")
            target.commit()
            hashes = [row[0] for row in target.execute("SELECT hash FROM objects")]
            # Human-readable notes supplement the authoritative database snapshot.
            notes = [{"id": r[0], "revision": r[1], **json.loads(r[2])}
                     for r in target.execute("SELECT id,revision,payload FROM local_notes")]
            notes.extend({"id": r[0], "revision": r[1], **json.loads(r[2])}
                         for r in target.execute("SELECT id,revision,payload FROM entities WHERE kind IN ('note','annotation')"))
        files = {"knowledge.sqlite3": database}
        for sha in hashes:
            path = store.objects / (sha + ".pdf")
            if not path.is_file() or digest(path.read_bytes()) != sha:
                raise ValueError("附件缺失或损坏，拒绝发布不完整备份。")
            files["objects/" + sha + ".pdf"] = path
        manifest = {"format": "research-zotero-backup", "version": 1, "created_at": now(), "files": {}}
        staged_archive = Path(temporary) / "backup.zip"
        with zipfile.ZipFile(staged_archive, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name, path in files.items():
                content = path.read_bytes()
                manifest["files"][name] = digest(content)
                archive.writestr(name, content)
            for note in notes:
                name = f"notes/{note['id']}-{note['revision']}.md"
                content = ("# " + note.get("title", "") + "\n\n" + note.get("body", "") +
                           "\n\n" + note.get("comment", "") + "\n").encode()
                manifest["files"][name] = digest(content)
                archive.writestr(name, content)
            archive.writestr("manifest.json", encoded(manifest))
        staged_archive.chmod(0o600)
        staged_archive.replace(destination)


def restore_backup(archive_path, root):
    root = Path(root).resolve()
    if root.exists():
        raise ValueError("恢复目标必须是尚不存在的新目录，不能覆盖运行中的知识库。")
    with zipfile.ZipFile(archive_path) as archive, tempfile.TemporaryDirectory() as temporary:
        names = archive.namelist()
        if len(names) != len(set(names)) or len(names) > 20000:
            raise ValueError("备份包含重复文件或文件过多。")
        if sum(info.file_size for info in archive.infolist()) > 2 * 1024**3:
            raise ValueError("备份解压大小超过 2 GB。")
        manifest = json.loads(archive.read("manifest.json"))
        if manifest.get("format") != "research-zotero-backup" or manifest.get("version") != 1:
            raise ValueError("不支持的备份格式。")
        expected = manifest.get("files", {})
        if set(names) != set(expected) | {"manifest.json"}:
            raise ValueError("备份文件清单不一致。")
        for name, sha in expected.items():
            from pathlib import PurePosixPath
            path = PurePosixPath(name)
            if path.is_absolute() or ".." in path.parts or "\\" in name or ":" in name:
                raise ValueError("备份包含非法路径。")
            if name != "knowledge.sqlite3" and path.parts[0] not in {"objects", "notes"}:
                raise ValueError("备份包含未知文件。")
            content = archive.read(name)
            if digest(content) != sha:
                raise ValueError("备份校验失败。")
            destination = Path(temporary) / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(content)
        database = Path(temporary) / "knowledge.sqlite3"
        with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as con:
            tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if (not TABLES.issubset(tables) or tables - TABLES - OPTIONAL_TABLES
                    or con.execute("SELECT 1 FROM sqlite_master WHERE type IN ('view','trigger')").fetchone()):
                raise ValueError("备份数据库结构不兼容。")
            if "qa_runs" in tables:
                columns = [row[1] for row in con.execute("PRAGMA table_info(qa_runs)")]
                if columns != ["id", "created_at", "metrics"]:
                    raise ValueError("问答统计表结构不兼容。")
            if con.execute("PRAGMA quick_check").fetchone()[0] != "ok" or con.execute("PRAGMA foreign_key_check").fetchall():
                raise ValueError("备份数据库完整性校验失败。")
            if con.execute("SELECT MAX(version) FROM kb_schema").fetchone()[0] != 1:
                raise ValueError("备份版本不兼容。")
            for (sha,) in con.execute("SELECT hash FROM objects"):
                filename = "objects/" + sha + ".pdf"
                if expected.get(filename) != sha:
                    raise ValueError("备份缺少原始附件。")
        home = root / ".data/zotero"
        home.mkdir(parents=True, exist_ok=False)
        shutil.copy2(database, home / "knowledge.sqlite3")
        shutil.copytree(Path(temporary) / "objects", home / "objects") if (Path(temporary) / "objects").exists() else (home / "objects").mkdir()
    return root


def main(argv=None):
    """Restore the plugin's exported ZIP without loading models or the web app."""
    import argparse
    parser = argparse.ArgumentParser(description="校验并恢复 Zotero 知识库备份，仅写入尚不存在的新目录")
    parser.add_argument("--archive", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    args = parser.parse_args(argv)
    root = restore_backup(args.archive, args.target)
    database = root / ".data/zotero/knowledge.sqlite3"
    with closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as con:
        counts = {table: con.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                  for table in ("knowledge_bases", "objects", "entities", "local_notes", "note_revisions")}
    print(encoded({"status": "restored", "target": str(root), "counts": counts,
                   "archive_sha256": digest(args.archive.read_bytes()),
                   "credentials_included": False, "model_calls": 0}))


if __name__ == "__main__":
    main()
