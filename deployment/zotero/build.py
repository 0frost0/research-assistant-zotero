"""Build the Zotero extension from this standalone repository.

This file owns the single authoritative asset list (H03: the public plugin
directory must stay buildable on its own). ``scripts/build_zotero_plugin.py``
delegates here instead of repeating the list.
"""
from pathlib import Path
import hashlib
import json
import zipfile

ROOT = Path(__file__).resolve().parent
ASSETS = (
    "manifest.json",
    "bootstrap.js",
    "bridge.js",
    "assistant.xhtml",
    "assistant.css",
    "assistant.js",
    "engine-settings.xhtml",
    "engine-settings.js",
)


def build(output_dir=None):
    """Package the add-on and return ``(path, sha256)``."""
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    missing = [name for name in ASSETS if not (ROOT / name).is_file()]
    if missing:
        raise FileNotFoundError("缺少插件资源，无法打包：" + "、".join(missing))
    target = Path(output_dir) if output_dir else ROOT / "dist"
    target.mkdir(parents=True, exist_ok=True)
    output = target / f"research-assistant-{manifest['version']}.xpi"
    # Fixed timestamps keep the archive byte-identical for unchanged sources,
    # so release checksums stay reproducible across machines.
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in ASSETS:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            # ZIP defaults this field to the build host (Windows=0, Unix=3).
            # Fix it alongside the mode and timestamp for cross-OS releases.
            info.create_system = 3
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, (ROOT / name).read_bytes())
    return output, hashlib.sha256(output.read_bytes()).hexdigest()


if __name__ == "__main__":
    path, checksum = build()
    print(path)
    print("sha256 " + checksum)
