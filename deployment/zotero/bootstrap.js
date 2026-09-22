var bridgeScope;
var chromeHandle;

async function startup({ rootURI }) {
  await Zotero.initializationPromise;
  await Zotero.uiReadyPromise;
  const addonManagerStartup = Cc["@mozilla.org/addons/addon-manager-startup;1"]
    .getService(Ci.amIAddonManagerStartup);
  chromeHandle = addonManagerStartup.registerChrome(
    Services.io.newURI(rootURI + "manifest.json"),
    [["content", "researchassistant", ""]]
  );
  const { Subprocess } = ChromeUtils.importESModule("resource://gre/modules/Subprocess.sys.mjs");
  bridgeScope = { Zotero, Services, IOUtils, Subprocess, URL: Zotero.getMainWindow().URL };
  Services.scriptloader.loadSubScript(rootURI + "bridge.js", bridgeScope);
  await bridgeScope.ResearchBridge.start(rootURI);
  for (const window of Zotero.getMainWindows()) bridgeScope.ResearchBridge.loadWindow(window);
}

function onMainWindowLoad({ window }) {
  bridgeScope?.ResearchBridge.loadWindow(window);
}

function onMainWindowUnload({ window }) {
  bridgeScope?.ResearchBridge.unloadWindow(window);
}

function shutdown() {
  bridgeScope?.ResearchBridge.stop();
  bridgeScope = undefined;
  chromeHandle?.destruct();
  chromeHandle = undefined;
}

function install() {}
function uninstall() {}
