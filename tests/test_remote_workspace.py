"""Enforce project-only deployment writes and process ownership before signals."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, Mock

from deployment import download_remote_models as models
from deployment import remote_services as services


class RemoteWorkspaceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        (self.root / '.research-assistant-workspace').touch()
        (self.root / 'runtime/pids').mkdir(parents=True)

    def test_model_download_requires_dedicated_marker(self):
        (self.root / '.research-assistant-workspace').unlink()
        with self.assertRaises(ValueError):
            models.prepare(self.root, 'qwen')

    def test_paths_outside_root_are_rejected_before_any_write(self):
        with self.assertRaises(ValueError):
            models.own_path(self.root, '../shared-model')
        with patch.object(services, 'ROOT', self.root):
            with self.assertRaises(ValueError):
                services.local_path('../shared-cache')

    def test_existing_model_directory_is_never_replaced_for_reuse(self):
        source = self.root / 'shared-source'
        source.mkdir()
        (source / 'config.json').write_text('{}')
        target = self.root / 'models/Qwen3-VL-Embedding-2B'
        target.mkdir(parents=True)
        with self.assertRaises(FileExistsError):
            models.prepare(self.root, 'qwen', source)

    def test_shared_model_symlink_cannot_be_used_as_download_destination(self):
        with tempfile.TemporaryDirectory() as other:
            outside = Path(other).resolve()
            target = self.root / 'models/Qwen3-VL-Embedding-2B'
            target.parent.mkdir()
            try:
                target.symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest('OS does not permit test symlinks')
            with self.assertRaises(ValueError):
                models.prepare(self.root, 'qwen')
            self.assertEqual(list(outside.iterdir()), [])

    def test_all_runtime_caches_are_project_local(self):
        with patch.object(services, 'ROOT', self.root):
            env = services.environment('3')
        for key in ('XDG_CACHE_HOME', 'HF_HOME', 'TORCH_HOME', 'TRITON_CACHE_DIR',
                    'VLLM_CONFIG_ROOT', 'TMPDIR', 'MINERU_TOOLS_CONFIG_JSON'):
            self.assertTrue(Path(env[key]).is_relative_to(self.root), key)
        self.assertEqual(env['HF_HUB_OFFLINE'], '1')
        self.assertEqual(env['PYTHONDONTWRITEBYTECODE'], '1')

    def test_pid_reuse_or_foreign_command_is_rejected(self):
        saved = {'pid': 123, 'identity': {'cwd': str(self.root), 'command': ['original'], 'start_ticks': '1'}}
        (self.root / 'runtime/pids/qwen.json').write_text(json.dumps(saved))
        foreign = {**saved['identity'], 'start_ticks': '2'}
        with patch.object(services, 'ROOT', self.root), patch.object(services, 'process_identity', return_value=foreign):
            with self.assertRaisesRegex(RuntimeError, 'ownership mismatch'):
                services.owned_process('qwen')

    def test_another_listener_is_never_adopted_or_stopped(self):
        probe = Mock()
        probe.connect_ex.return_value = 0
        with patch.object(services, 'ROOT', self.root), patch.object(services.socket, 'socket') as sock:
            sock.return_value.__enter__.return_value = probe
            with patch.object(services.subprocess, 'Popen') as popen:
                with self.assertRaisesRegex(RuntimeError, 'another process'):
                    services.start('qwen', '3')
                popen.assert_not_called()


if __name__ == '__main__':
    unittest.main()
