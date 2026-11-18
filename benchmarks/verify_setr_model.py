"""Read-only fingerprint of an existing local teacher; never download weights.

Checks index shard presence, safetensors byte ranges and records SHA256 of all
loadable artifacts. This establishes local identity, not upstream provenance.
"""
import argparse
import hashlib
import json
from pathlib import Path
import struct


def fingerprint(root):
    root = Path(root).resolve(strict=True)
    config = json.loads((root / "config.json").read_bytes())
    index = json.loads((root / "model.safetensors.index.json").read_bytes())
    shards = sorted(set(index["weight_map"].values()))
    headers = {}
    for name in shards:
        if Path(name).name != name:
            raise ValueError("shard index must contain only local filenames")
        p = root / name
        with p.open("rb") as f:
            size_bytes = f.read(8)
            if len(size_bytes) != 8:
                raise ValueError(f"incomplete shard: {name}")
            n = struct.unpack("<Q", size_bytes)[0]
            if not 0 < n <= min(100_000_000, p.stat().st_size - 8):
                raise ValueError(f"invalid safetensors header: {name}")
            header = json.loads(f.read(n))
        payload = p.stat().st_size - n - 8
        spans = []
        for key, tensor in header.items():
            if key == "__metadata__":
                continue
            start, end = tensor["data_offsets"]
            if not 0 <= start <= end <= payload:
                raise ValueError(f"incomplete tensor in {name}: {key}")
            spans.append((start, end))
        cursor = 0
        for start, end in sorted(spans):
            if start != cursor:
                raise ValueError(f"noncontiguous tensor data: {name}")
            cursor = end
        if cursor != payload:
            raise ValueError(f"unexpected shard size: {name}")
        headers[name] = header
    for key, name in index["weight_map"].items():
        if key not in headers[name]:
            raise ValueError(f"index tensor missing: {key}")
    names = sorted({*shards, *(p.name for p in root.iterdir()
                               if p.is_file() and p.suffix in {".json", ".txt", ".jinja"})})
    files = {}
    for name in names:
        with (root / name).open("rb") as f:
            files[name] = hashlib.file_digest(f, "sha256").hexdigest()
        print(json.dumps({"fingerprinted": name}), flush=True)
    return {"model_name": root.name, "source": "existing shared local weights, read-only reuse",
            "local_path": str(root), "upstream_revision": None,
            "provenance_note": "Local SHA256 identity; no upstream revision claimed without verification",
            "model_type": config.get("model_type"), "architectures": config.get("architectures"),
            "quantized": bool(config.get("quantization_config")), "shards": len(shards),
            "weight_bytes": sum((root / s).stat().st_size for s in shards),
            "safetensors_ranges_verified": True, "files": files}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--model", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    if a.output.exists():
        raise FileExistsError(a.output)
    if a.model.resolve() in a.output.resolve().parents:
        raise ValueError("manifest must be written outside shared model directory")
    result = fingerprint(a.model)
    a.output.parent.mkdir(parents=True, exist_ok=True)
    with a.output.open("x", encoding="utf-8") as f:
        json.dump(result, f, indent=2)
    print(json.dumps({k: v for k, v in result.items() if k != "files"}), flush=True)


if __name__ == "__main__":
    main()
