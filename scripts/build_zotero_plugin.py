"""Package the dependency-free Zotero bridge; no install or library changes."""
from pathlib import Path
import json
import zipfile

root = Path(__file__).resolve().parents[1]
source = root / "deployment/zotero"
manifest = json.loads((source / "manifest.json").read_text())
output = root / "output/zotero" / ("research-assistant-" + manifest["version"] + ".xpi")
output.parent.mkdir(parents=True, exist_ok=True)
assets = (
    "manifest.json",
    "bootstrap.js",
    "bridge.js",
    "assistant.xhtml",
    "assistant.css",
    "assistant.js",
)
with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
    for name in assets:
        archive.write(source / name, name)
print(output)
