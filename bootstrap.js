var bridgeScope;

async function startup({ rootURI }) {
  await Zotero.initializationPromise;
  await Zotero.uiReadyPromise;
  bridgeScope = { Zotero, Services, IOUtils, URL: Zotero.getMainWindow().URL };
  Services.scriptloader.loadSubScript(rootURI + "bridge.js", bridgeScope);
  await bridgeScope.ResearchBridge.start();
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
}

function install() {}
function uninstall() {}
