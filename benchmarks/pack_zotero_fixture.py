"""Refresh only the explicitly isolated native acceptance add-on."""
import argparse
from pathlib import Path
import json
import shutil
import zipfile

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument("--target", type=Path, default=root / "output/zotero/native-fixture")
args = parser.parse_args()
target = args.target.resolve()
assets = ("manifest.json", "bridge.js", "assistant.xhtml", "assistant.css", "assistant.js")
for name in assets:
    shutil.copy2(root / "deployment/zotero" / name, target / "plugin" / name)
fixture = (root / "benchmarks/zotero_native_fixture.js").read_text(encoding="utf-8")
fixture = fixture.replace("__FIXTURE_ROOT__", json.dumps(str(target.resolve())))
(target / "plugin/fixture.js").write_text(fixture, encoding="utf-8")
bootstrap = (root / "deployment/zotero/bootstrap.js").read_text(encoding="utf-8")
bootstrap = bootstrap.replace(
    "for (const window of Zotero.getMainWindows()) bridgeScope.ResearchBridge.loadWindow(window);",
    "for (const window of Zotero.getMainWindows()) bridgeScope.ResearchBridge.loadWindow(window);\n"
    "  Services.scriptloader.loadSubScript(rootURI + 'fixture.js', bridgeScope);\n"
    "  await bridgeScope.runNativeFixture();",
)
(target / "plugin/bootstrap.js").write_text(bootstrap, encoding="utf-8")
xpi = target / "research-assistant-fixture.xpi"
with zipfile.ZipFile(xpi, "w", zipfile.ZIP_DEFLATED) as z:
    for name in (*assets, "bootstrap.js", "fixture.js"):
        z.write(target / "plugin" / name, name)
print(f"Isolated fixture XPI ready: {xpi}")
