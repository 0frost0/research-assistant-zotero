async function runRealQuestions() {
  const config = __RUN_CONFIG__;
  const report = {phase: "starting", zotero_version: Zotero.version, cases: [], model_calls: 0,
    max_model_calls: config.max_model_calls, human_semantic_accuracy: null};
  const write = () => IOUtils.writeUTF8(PathUtils.join(config.root, "result.json"), JSON.stringify(report));
  try {
    if (Zotero.DataDirectory.dir.replace(/\\/g, "/") !== (config.root + "/data").replace(/\\/g, "/"))
      throw new Error("Refusing non-isolated Zotero data directory");
    ResearchBridge.setPref("connection", config.connection);
    ResearchBridge.setPref("endpoint", config.endpoint);
    ResearchBridge.setPref("token", (await IOUtils.readUTF8(PathUtils.join(config.root, "backend", ".data", "zotero", "connection-token"))).trim());
    ResearchBridge.setPref("lastBase", config.base);
    const win = ResearchBridge.open();
    for (let i = 0; i < 200 && !win?.ResearchAssistantWorkbench?.ready; i++) await Zotero.Promise.delay(100);
    await win.ResearchAssistantWorkbench.ready;
    const doc = win.document;
    for (let i = 0; i < 200 && !doc.querySelector("#baseList .base-row"); i++) await Zotero.Promise.delay(100);
    for (const test of config.cases) {
      const ceiling = test.scope === "notes" ? 2 : 4;
      if (report.model_calls + ceiling > config.max_model_calls) throw new Error("Call budget exhausted");
      // Trigger and await actual multimodal index construction before generation.
      let ready;
      for (let i = 0; i < 600; i++) {
        ready = await ResearchBridge.request("search", {query: test.query, kb_ids: [config.base], scope: test.scope, k: 5, text_only: true});
        if (ready.status !== "indexing") break;
        await Zotero.Promise.delay(500);
      }
      if (ready.status === "indexing") throw new Error("Index preparation timed out");
      const before = new Set((await ResearchBridge.request("jobs", undefined, "GET")).jobs.map(j => j.job_id));
      doc.getElementById("newConversation").click();
      doc.querySelector(`[name="scope"][value="${test.scope}"]`).click();
      doc.getElementById("query").value = test.query;
      const start = Date.now();
      doc.getElementById("askButton").click();
      let job;
      for (let i = 0; i < 600; i++) {
        const jobs = (await ResearchBridge.request("jobs", undefined, "GET")).jobs;
        job = jobs.find(j => !before.has(j.job_id) && j.question === test.query);
        if (job && !["queued", "running"].includes(job.state) && !doc.getElementById("askButton").disabled) break;
        await Zotero.Promise.delay(500);
      }
      if (!job || ["queued", "running"].includes(job.state)) {
        if (job) await ResearchBridge.request("jobs/" + job.job_id, {action: "cancel"});
        throw new Error("Native ask did not complete");
      }
      job = await ResearchBridge.request("jobs/" + job.job_id, undefined, "GET");
      const answer = job.result || {};
      const trace = answer.trace || {};
      report.model_calls += trace.model_calls || 0;
      report.cases.push({id: test.id, question: test.query, scope: test.scope, job_id: job.job_id,
        state: job.state, status: answer.status, claims: (answer.claims || []).length,
        model_calls: trace.model_calls, retrieval_calls: trace.retrieval_calls,
        backend: trace.retrieval_backend, elapsed_ms: Date.now() - start,
        rendered_claims: doc.querySelectorAll(".answer-claim").length,
        revisions: answer.revisions, error: job.error});
      report.phase = "running";
      await write();
    }
    report.phase = "completed";
  } catch (error) {
    report.phase = "failed";
    report.error = String(error);
  }
  await write();
}
