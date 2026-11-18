"""Release provenance checks use an isolated repository, never GitHub."""
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from scripts import archive_github_releases as archive


class ArchiveTests(unittest.TestCase):
    def test_historical_tag_preserves_bytes_and_rejects_changed_tree(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            subprocess.run(["git", "init", "-q", str(root)], check=True)
            for key, value in [("user.name", "Fixture"), ("user.email", "fixture@example.invalid")]:
                subprocess.run(["git", "-C", str(root), "config", key, value], check=True)
            folder = root / "archive"
            source = folder / "source/deployment/zotero"
            source.mkdir(parents=True)
            manifest = source / "manifest.json"
            manifest.write_bytes(b'{"version":"0.1.0"}\r\n')
            (folder / "source/ARCHIVE.md").write_text("Retained plugin only", encoding="utf-8")
            with patch.object(archive, "ROOT", root):
                commit = archive.archive_tag(folder, "0.1.0")
                self.assertEqual(archive.archive_tag(folder, "0.1.0"), commit)
                self.assertEqual(archive.git("show", commit + ":deployment/zotero/manifest.json"), manifest.read_bytes())
                self.assertFalse((root / ".git/index").exists())
                manifest.write_text(json.dumps({"version": "changed"}), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "does not match"):
                    archive.archive_tag(folder, "0.1.0")

    def test_publish_refuses_dirty_or_unpushed_source_before_credentials(self):
        for outputs in ([b" M source.py\n"], [b"", b"local", b"remote"]):
            with self.subTest(outputs=outputs), patch.object(archive, "git", side_effect=outputs), patch.object(archive.subprocess, "run") as run:
                with self.assertRaises(ValueError):
                    archive.publish(Path("unused"))
                run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
