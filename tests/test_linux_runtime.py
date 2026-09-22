"""Virtual environments must keep their executable path, including symlinks."""
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from research_assistant.ingestion.mineru_extractor import MinerUExtractor


class LinuxRuntimeTests(unittest.TestCase):
    @unittest.skipIf(os.name == 'nt', 'POSIX venv symlinks')
    def test_mineru_preserves_venv_python(self):
        with tempfile.TemporaryDirectory() as folder:
            executable=Path(folder)/'venv/bin/python'
            executable.parent.mkdir(parents=True)
            executable.symlink_to(sys.executable)
            with patch.dict(os.environ,{'RESEARCH_ASSISTANT_MINERU_PYTHON':str(executable)}):
                client=MinerUExtractor()
            self.assertEqual(client.python_executable,executable)
            self.assertNotEqual(client.python_executable,executable.resolve())
            self.assertTrue(client.configured)
