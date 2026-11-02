"""Package the dependency-free Zotero bridge; no install or library changes.

The asset list lives in ``deployment/zotero/build.py`` so the standalone plugin
directory stays independently buildable (H03) without duplicating the manifest
of packaged files in two places.
"""
from pathlib import Path
import importlib.util

root = Path(__file__).resolve().parents[1]
source = root / "deployment/zotero"
spec = importlib.util.spec_from_file_location("zotero_plugin_build", source / "build.py")
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)

output, checksum = builder.build(root / "output/zotero")
print(output)
print("sha256 " + checksum)
