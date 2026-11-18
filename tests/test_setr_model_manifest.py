import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest

from benchmarks.verify_setr_model import fingerprint


class SetRModelManifestTests(unittest.TestCase):
    def model(self, root, *, offsets=(0, 4), indexed_key="weight"):
        (root / "config.json").write_text(json.dumps({"model_type": "qwen3_5"}))
        (root / "model.safetensors.index.json").write_text(json.dumps({
            "weight_map": {indexed_key: "model-00001.safetensors"}}))
        header = json.dumps({"weight": {"dtype": "F32", "shape": [1], "data_offsets": offsets}}).encode()
        shard = root / "model-00001.safetensors"
        shard.write_bytes(struct.pack("<Q", len(header)) + header + struct.pack("<f", 1.0))
        return shard

    def test_manifest_records_local_identity_without_claiming_upstream_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shard = self.model(root)
            result = fingerprint(root)
            self.assertEqual(result["shards"], 1)
            self.assertTrue(result["safetensors_ranges_verified"])
            self.assertIsNone(result["upstream_revision"])
            self.assertEqual(result["files"][shard.name], hashlib.sha256(shard.read_bytes()).hexdigest())

    def test_truncated_shard_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shard = self.model(root)
            shard.write_bytes(shard.read_bytes()[:-1])
            with self.assertRaises(ValueError):
                fingerprint(root)

    def test_index_key_missing_from_shard_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.model(root, indexed_key="missing")
            with self.assertRaises(ValueError):
                fingerprint(root)

    def test_shard_path_escape_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.model(root)
            (root / "model.safetensors.index.json").write_text(json.dumps({
                "weight_map": {"weight": "../shared.safetensors"}}))
            with self.assertRaises(ValueError):
                fingerprint(root)


if __name__ == "__main__":
    unittest.main()
