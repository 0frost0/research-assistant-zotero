"""Explicit local administration; never prints or includes tokens in backups."""
import argparse
import os
from pathlib import Path
import secrets
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from research_assistant.knowledge.store import KnowledgeStore
from research_assistant.knowledge.backup import export_backup, restore_backup

parser = argparse.ArgumentParser()
parser.add_argument("action", choices=["init", "export", "restore"])
parser.add_argument("--root", type=Path, required=True)
parser.add_argument("--file", type=Path)
args = parser.parse_args()
if args.action == "init":
    directory = args.root / ".data/zotero"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "connection-token"
    if not path.exists():
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as file:
            file.write(secrets.token_urlsafe(32))
    print("Connection token file ready; token not printed.")
elif args.action == "export":
    if args.file is None or args.file.exists():
        parser.error("--file must name a new backup file")
    export_backup(KnowledgeStore(args.root), args.file)
    print("Backup exported.")
else:
    if args.file is None:
        parser.error("--file is required")
    restore_backup(args.file, args.root)
    print("Backup restored into a new root; configure a new connection token.")
