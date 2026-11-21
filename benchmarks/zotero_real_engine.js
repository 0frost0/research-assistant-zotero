async function runRealQuestions() {
  const config = __RUN_CONFIG__;
  const report = {phase: "starting", zotero_version: Zotero.version};
  const bridge = ResearchBridge;
  try {
    if (Zotero.DataDirectory.dir.replace(/\\/g, "/") !== (config.root + "/data").replace(/\\/g, "/"))
      throw new Error("Refusing non-isolated Zotero data directory");
    bridge.setPref("connection", config.connection);
    await bridge.saveEngineSettings({
      engineExecutable: config.engine_python, engineScript: config.engine_script,
      engineWorkdir: PathUtils.join(config.root, "backend"), autoStartEngine: false,
      stopEngineOnExit: true, sshExecutable: "", qwenSshHost: "", qwenServiceUnit: "",
    });
    const status = await bridge.startEngine();
    if (!status.connected || status.runtime !== "go") throw new Error("Expected Go API");
    report.runtime = status.runtime;
    const search = {
      query: config.cases.find(test => test.scope === "notes").query,
      kb_ids: [config.base], scope: "notes", k: 5,
    };
    let answer;
    for (let i = 0; i < 240; i++) {
      answer = await bridge.request("search", search);
      if (answer.status !== "indexing") break;
      await Zotero.Promise.delay(500);
    }
    if (answer.status === "indexing") throw new Error("Personal-memory index preparation timed out");
    report.memory_hits = answer.results.length;
    report.search_backend = answer.backend;
    const snapshot = await bridge.request("bases/" + encodeURIComponent(config.base), undefined, "GET");
    const note = [...snapshot.notes, ...snapshot.entities].find(item =>
      item.data.confirmed && !item.data.archived && item.data.body?.trim().length >= 8);
    if (!note) throw new Error("Requires a confirmed active note");
    const exact = await bridge.request("search", {...search, query: note.data.body.trim().slice(0, 300)});
    if (!exact.results.some(hit => hit.entity_id === note.id && hit.revision === note.revision))
      throw new Error("Exact note search missed its revision");
    report.exact_note_revision_found = true;
    // Match the native stop-on-exit operation, including its forced-kill timeout.
    await bridge.engineProcess.kill(1000);
    await bridge.engineProcess.wait();
    bridge.engineProcess = null;
    for (let i = 0; i < 100 && (await bridge.backendStatus()).connected; i++)
      await Zotero.Promise.delay(100);
    if ((await bridge.backendStatus()).connected) throw new Error("Go API survived launcher exit");
    report.stopped = true;
    report.phase = "completed";
  } catch (error) {
    report.phase = "failed";
    report.error = String(error);
  } finally {
    if (bridge.engineProcess) await bridge.engineProcess.kill(1000).catch(() => {});
    await IOUtils.writeUTF8(PathUtils.join(config.root, "result.json"), JSON.stringify(report));
  }
}
