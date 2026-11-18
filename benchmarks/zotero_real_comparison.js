async function runRealQuestions() {
  const config = __RUN_CONFIG__;
  const report = {phase: "starting", zotero_version: Zotero.version, max_model_calls: 4,
    human_semantic_accuracy: null};
  const write = () => IOUtils.writeUTF8(PathUtils.join(config.root, "result.json"), JSON.stringify(report));
  let win, originalConfirm, originalRequest;
  try {
    if (Zotero.DataDirectory.dir.replace(/\\/g, "/") !== (config.root + "/data").replace(/\\/g, "/"))
      throw new Error("Refusing non-isolated Zotero data directory");
    ResearchBridge.setPref("connection", config.connection);
    ResearchBridge.setPref("endpoint", config.endpoint);
    ResearchBridge.setPref("token", (await IOUtils.readUTF8(PathUtils.join(config.root, "backend", ".data", "zotero", "connection-token"))).trim());
    ResearchBridge.setPref("lastBase", config.base);
    win = ResearchBridge.open();
    for (let i = 0; i < 200 && !win?.ResearchAssistantWorkbench?.ready; i++) await Zotero.Promise.delay(100);
    await win.ResearchAssistantWorkbench.ready;
    const doc = win.document;
    for (let i = 0; i < 200 && doc.querySelectorAll('#members input[type="checkbox"]').length < 2; i++)
      await Zotero.Promise.delay(100);
    const rows = [...doc.querySelectorAll("#members tr")];
    for (const title of ["MOTOR", "RePrompT"]) {
      const row = rows.find(row => row.textContent.toLowerCase().includes(title.toLowerCase()));
      if (!row?.querySelector('input[type="checkbox"]')) throw new Error("Expected paper missing: " + title);
      row.querySelector('input[type="checkbox"]').click();
    }
    const question = "MOTOR和RePrompT使用的数据集与研究任务有什么区别？这些实验结果能直接比较吗？";
    let ready;
    for (let i = 0; i < 600; i++) {
      ready = await ResearchBridge.request("search", {query: question, kb_ids: [config.base], scope: "original", k: 5, text_only: true});
      if (ready.status !== "indexing") break;
      await Zotero.Promise.delay(500);
    }
    if (ready.status === "indexing") throw new Error("Index preparation timed out");
    // Observe the real bridge response without substituting data or model calls.
    originalRequest = ResearchBridge.request;
    let result, error, requests = 0;
    ResearchBridge.request = async function(action, ...args) {
      if (action === "compare" && ++requests > 1) throw new Error("Comparison request budget exhausted");
      try {
        const value = await originalRequest.call(this, action, ...args);
        if (action === "compare") result = value;
        return value;
      } catch (caught) { if (action === "compare") error = String(caught); throw caught; }
    };
    originalConfirm = win.confirm;
    win.confirm = () => true;
    doc.getElementById("query").value = question;
    const start = Date.now();
    const button = doc.getElementById("compareButton");
    if (button.disabled) throw new Error("Native comparison button disabled");
    button.click();
    for (let i = 0; i < 420 && !result && !error; i++) await Zotero.Promise.delay(500);
    if (!result) throw new Error(error || "Native comparison deadline exceeded");
    if (result.calls > report.max_model_calls) throw new Error("Model call budget exceeded");
    // Full response stays in ignored private output for local citation auditing.
    await IOUtils.writeUTF8(PathUtils.join(config.root, "comparison-private.json"), JSON.stringify(result));
    report.status = result.status;
    report.model_calls = result.calls;
    report.requests = requests;
    report.analyses = result.analyses.map(a => ({status: a.status, findings: a.findings.length}));
    report.profiles = result.profiles.length;
    report.comparisons = result.comparisons.length;
    report.withheld = result.withheld.length;
    report.review_rounds = result.review_rounds;
    report.warnings = result.warnings;
    report.elapsed_ms = Date.now() - start;
    report.rendered = doc.querySelector("#results h3")?.textContent.includes("多文献对照") || false;
    if (!report.rendered) throw new Error("Native result did not render");
    report.phase = "completed";
  } catch (error) {
    report.phase = "failed";
    report.error = String(error);
  } finally {
    if (originalRequest) ResearchBridge.request = originalRequest;
    if (win && originalConfirm) win.confirm = originalConfirm;
  }
  await write();
}
