"""The shipped XPI should not depend on the build host's ZIP platform flag."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

source = Path(__file__).resolve().parents[1] / "deployment/zotero/build.py"
spec = importlib.util.spec_from_file_location("plugin_builder_test", source)
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class ZoteroBuildTests(unittest.TestCase):
    def test_cross_platform_archive_metadata_and_payloads_are_identical(self):
        native_info = zipfile.ZipInfo
        with tempfile.TemporaryDirectory() as directory:
            artifacts = []
            for platform in (0, 3):
                class PlatformInfo(native_info):
                    def __init__(self, *args, **kwargs):
                        super().__init__(*args, **kwargs)
                        self.create_system = platform
                # Override only the platform-dependent constructor default.
                with patch.object(builder.zipfile, "ZipInfo", PlatformInfo):
                    artifacts.append(builder.build(Path(directory) / str(platform)))
            self.assertEqual(artifacts[0][1], artifacts[1][1])
            self.assertEqual(artifacts[0][0].read_bytes(), artifacts[1][0].read_bytes())
            with zipfile.ZipFile(artifacts[0][0]) as archive:
                self.assertEqual(archive.namelist(), list(builder.ASSETS))
                for name in builder.ASSETS:
                    self.assertEqual(archive.read(name), (builder.ROOT / name).read_bytes())
                    self.assertEqual(archive.getinfo(name).create_system, 3)


if __name__ == "__main__":
    unittest.main()
