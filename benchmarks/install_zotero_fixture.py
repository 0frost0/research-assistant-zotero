"""Install only the synthetic add-on through the isolated Zotero debugger."""
import argparse
import json
from pathlib import Path
import socket
import time

project_root = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser()
parser.add_argument(
    "--fixture-root",
    type=Path,
    default=project_root / "output/zotero/native-fixture",
)
parser.add_argument("--addon", type=Path)
args = parser.parse_args()
root = args.fixture_root.resolve()
addon_path = (args.addon or root / "plugin").resolve()
diagnostic = root / "install-result.json"
diagnostic.unlink(missing_ok=True)

with socket.create_connection(("127.0.0.1", 2832), timeout=10) as client:
    client.settimeout(100)

    def receive():
        length = b""
        while not length.endswith(b":"):
            part = client.recv(1)
            if not part:
                raise RuntimeError("Marionette closed")
            length += part
        remaining, chunks = int(length[:-1]), []
        while remaining:
            chunk = client.recv(remaining)
            if not chunk:
                raise RuntimeError("Marionette closed")
            chunks.append(chunk)
            remaining -= len(chunk)
        return json.loads(b"".join(chunks))

    def command(actor, kind, **params):
        payload = json.dumps({"to": actor, "type": kind, **params}).encode()
        client.sendall(str(len(payload)).encode() + b":" + payload)
        while True:
            response = receive()
            if response.get("from") != actor:
                continue
            if response.get("error"):
                raise RuntimeError(json.dumps(response))
            return response

    print("Debugger", receive().get("applicationType"))
    descriptor = command("root", "getProcess", id=0)
    print("Descriptor", descriptor)
    actor = descriptor["processDescriptor"]["actor"]
    target = command(actor, "getTarget")
    print("Target keys", list(target))
    console = target["process"]["consoleActor"]
    script = """
    (async () => {
      const expected = ROOT;
      const resultPath = PathUtils.join(expected, "install-result.json");
      const z = Services.wm.getMostRecentWindow("navigator:browser").Zotero;
      let result;
      try {
        if (z.DataDirectory.dir.replace(/\\\\/g, "/") !== expected.replace(/\\\\/g, "/") + "/data")
          throw new Error("Refusing non-fixture data directory");
        const { AddonManager } = ChromeUtils.importESModule("resource://gre/modules/AddonManager.sys.mjs");
        const file = Cc["@mozilla.org/file/local;1"].createInstance(Ci.nsIFile);
        const addonPath = ADDON_PATH;
        file.initWithPath(addonPath);
        if (!file.exists()) throw new Error("Fixture add-on not found: " + addonPath);
        const addon = await AddonManager.installTemporaryAddon(file);
        if (addon.id !== "research-assistant@local" || !addon.isActive)
          throw new Error("Fixture add-on did not become active");
        result = {
          ok: true,
          id: addon.id,
          active: addon.isActive,
          menu: !!z.getMainWindow().document.getElementById("research-assistant-menu"),
          path: addonPath,
        };
      } catch (error) {
        result = {
          ok: false,
          error: String(error),
          stack: error.stack || "",
          additionalErrors: error.additionalErrors || [],
        };
      }
      await z.File.putContentsAsync(resultPath, JSON.stringify(result));
      return JSON.stringify(result);
    })()
    """.replace("ROOT", json.dumps(str(root))).replace("ADDON_PATH", json.dumps(str(addon_path)))
    result = command(console, "evaluateJSAsync", text=script, **{"await": True})
    print("Evaluation", result)
    if "resultID" in result:
        while True:
            response = receive()
            if response.get("type") == "evaluationResult":
                print("Result", response)
                break

for _ in range(120):
    if diagnostic.exists():
        result = json.loads(diagnostic.read_text(encoding="utf-8"))
        print("Install result", result)
        if not result.get("ok"):
            raise RuntimeError(json.dumps(result, ensure_ascii=False))
        break
    time.sleep(1)
else:
    raise TimeoutError("Timed out waiting for Zotero add-on installation")
