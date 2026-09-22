"""Create a separate Zotero profile and synthetic library for native acceptance."""
import argparse
import json
from pathlib import Path
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.test_reading_memory import pdf

root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument("--target", type=Path, default=root / "output/zotero/native-fixture")
parser.add_argument("--no-install-pointer", action="store_true")
args = parser.parse_args()
target = args.target.resolve()
target.mkdir(parents=True, exist_ok=False)
profile, data, plugin = target / "profile", target / "data", target / "plugin"
profile.mkdir()
data.mkdir()
shutil.copytree(root / "deployment/zotero", plugin)
extensions = profile / "extensions"
extensions.mkdir()
if not args.no_install_pointer:
    (extensions / "research-assistant@local").write_text(str(plugin.resolve()), encoding="utf-8")
prefs = {
    "extensions.zotero.dataDir": str(data.resolve()),
    "extensions.zotero.useDataDir": True,
    "extensions.zotero.sync.autoSync": False,
    "extensions.autoDisableScopes": 0,
    "extensions.enabledScopes": 15,
    "extensions.startupScanScopes": 15,
    "extensions.logging.enabled": True,
    "extensions.update.enabled": False,
    "extensions.zoteroWinWordIntegration.skipInstallation": True,
    "extensions.zoteroOpenOfficeIntegration.skipInstallation": True,
    "devtools.debugger.remote-enabled": True,
    "devtools.debugger.prompt-connection": False,
    "devtools.debugger.remote-port": 2832,
    "devtools.debugger.force-local": True,
    "app.update.auto": False,
    "app.update.enabled": False,
    "extensions.zotero.firstRunGuidanceShown.zotero7Banner": True,
    "extensions.zotero.firstRunGuidance": False,
}
(profile / "user.js").write_text("\n".join("user_pref(" + json.dumps(k) + ", " + json.dumps(v) + ");"
                                         for k, v in prefs.items()), encoding="utf-8")
pdf(target / "synthetic.pdf")
fixture = (root / "benchmarks/zotero_native_fixture.js").read_text(encoding="utf-8")
fixture = fixture.replace("__FIXTURE_ROOT__", json.dumps(str(target.resolve())))
(plugin / "fixture.js").write_text(fixture, encoding="utf-8")
bootstrap = (plugin / "bootstrap.js").read_text(encoding="utf-8")
bootstrap = bootstrap.replace(
    "for (const window of Zotero.getMainWindows()) bridgeScope.ResearchBridge.loadWindow(window);",
    "for (const window of Zotero.getMainWindows()) bridgeScope.ResearchBridge.loadWindow(window);\n"
    "  Services.scriptloader.loadSubScript(rootURI + 'fixture.js', bridgeScope);\n"
    "  await bridgeScope.runNativeFixture();",
)
(plugin / "bootstrap.js").write_text(bootstrap, encoding="utf-8")
print(profile)
