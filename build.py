"""Build the Zotero extension from this standalone repository."""
from pathlib import Path
import json
import zipfile


root = Path(__file__).resolve().parent
manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
output = root / "dist" / f"research-assistant-{manifest['version']}.xpi"
output.parent.mkdir(parents=True, exist_ok=True)

with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
    for name in ("manifest.json", "bootstrap.js", "bridge.js"):
        archive.write(root / name, name)

print(output)
