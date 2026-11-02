from pathlib import Path
import json
import tempfile
import unittest

from benchmarks.verify_knowledge_backup import verify
from research_assistant.knowledge.store import KnowledgeStore


class BackupRehearsalTests(unittest.TestCase):
    def test_rehearsal_preserves_live_database_and_versions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "app"
            store = KnowledgeStore(root)
            base = store.create({"connection": "fixture", "library": "user:0",
                                 "collection": "notes", "name": "记忆"})
            note = store.save_note(base["id"], {"body": "我的初始理解"})
            store.save_note(base["id"], {"body": "我的修正理解", "expected_revision": 1}, note["id"])
            before = store.path.read_bytes()
            output = Path(directory) / "drill"
            report = verify(root, output)
            self.assertEqual(store.path.read_bytes(), before)
            self.assertEqual(report["status"], "passed")
            self.assertEqual(report["table_counts"]["note_revisions"], 2)
            self.assertEqual(json.loads((output / "report.json").read_text(encoding="utf-8")), report)
            self.assertEqual(KnowledgeStore(output / "restored").note_history(note["id"]), store.note_history(note["id"]))
            with self.assertRaises(FileExistsError):
                verify(root, output)

    def test_invalid_source_and_live_target_are_not_created_or_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "app"
            output = Path(directory) / "drill"
            with self.assertRaises(FileNotFoundError):
                verify(root, output)
            self.assertFalse(root.exists())
            self.assertFalse(output.exists())
            store = KnowledgeStore(root)
            with self.assertRaises(ValueError):
                verify(root, store.home / "drill")
            self.assertFalse((store.home / "drill").exists())
