async function runRealQuestions() {
  const config = __RUN_CONFIG__;
  const report = {phase: "starting", zotero_version: Zotero.version};
  const write = () => IOUtils.writeUTF8(PathUtils.join(config.root, "result.json"), JSON.stringify(report));
  async function until(label, read, timeout = 30000) {
    const start = Date.now();
    while (Date.now() - start < timeout) {
      const value = await read();
      if (value) return value;
      await Zotero.Promise.delay(100);
    }
    throw new Error("Timed out: " + label);
  }
  try {
    if (Zotero.DataDirectory.dir.replace(/\\/g, "/") !== (config.root + "/data").replace(/\\/g, "/"))
      throw new Error("Refusing non-isolated Zotero data directory");
    ResearchBridge.setPref("connection", config.connection);
    ResearchBridge.setPref("endpoint", config.endpoint);
    ResearchBridge.setPref("lastBase", config.base);
    ResearchBridge.setPref("token", (await IOUtils.readUTF8(PathUtils.join(config.root, "backend", ".data", "zotero", "connection-token"))).trim());
    const win = ResearchBridge.open();
    await until("workbench", () => !!win.ResearchAssistantWorkbench?.ready);
    await win.ResearchAssistantWorkbench.ready;
    const doc = win.document;
    const el = id => doc.getElementById(id);
    const jobs = async () => (await ResearchBridge.request("jobs", undefined, "GET")).jobs;
    const detail = id => ResearchBridge.request("jobs/" + encodeURIComponent(id), undefined, "GET");
    const row = id => doc.querySelector('[data-job-id="' + id + '"]');
    await until("library", () => doc.querySelector("#baseList .base-row"));
    const initial = await jobs();
    const interrupted = initial.find(j => j.state === "interrupted" && j.retry_count < 3);
    const answered = initial.find(j => j.state === "succeeded" && !j.feedback);
    if (!interrupted || !answered) throw new Error("Requires an interrupted job and an unrated completed job");
    async function openAnswer() {
      el("qaHistory").click();
      (await until("history result", () => row(answered.job_id)?.querySelector("button"))).click();
      return until("feedback panel", () => doc.querySelector(".qa-feedback"));
    }
    // Exercise persistence in the private disposable copy only. This rating is a UI test input.
    (await openAnswer()).querySelector('[data-rating="partial"]').click();
    await until("saved feedback", () => doc.querySelector('.qa-feedback[data-saved="true"]'));
    const reopened = await openAnswer();
    if (reopened.dataset.saved !== "true" || reopened.querySelector("button") ||
        !reopened.textContent.includes("部分有帮助") || (await detail(answered.job_id)).feedback?.rating !== "partial")
      throw new Error("Feedback did not survive history reopen");
    report.feedback_reopened = true;
    await write();

    el("newConversation").click();
    doc.querySelector('[name="scope"][value="original"]').click();
    el("query").value = config.cases[0].query;
    el("askButton").click();
    const active = await until("active ask", () => !el("cancelAskButton").hidden && win.__activeAskJobId);
    el("cancelAskButton").click();
    await until("cancelled job", async () => (await detail(active)).state === "cancelled");
    await until("ask controls restored", () => !el("askButton").disabled);
    if (el("resultMeta").textContent !== "问答已停止") throw new Error("Cancelled ask still shows progress");
    el("qaHistory").click();
    await until("cancelled history", () => row(active)?.textContent.includes("用户已停止"));
    report.cancelled_in_history = true;
    await write();

    const confirm = win.confirm;
    let confirmed = 0;
    win.confirm = () => { confirmed++; return true; };
    try {
      row(interrupted.job_id).querySelector("button").click();
    } finally {
      win.confirm = confirm;
    }
    const existing = new Set(initial.map(j => j.job_id));
    const child = await until("manual retry", async () => (await jobs()).find(j => !existing.has(j.job_id) && j.retry_of === interrupted.job_id));
    if (confirmed !== 1 || child.retry_count !== interrupted.retry_count + 1 ||
        (await detail(interrupted.job_id)).state !== "interrupted") throw new Error("Retry lineage mismatch");
    const terminal = await until("retry completion", async () => {
      const job = await detail(child.job_id);
      return !["queued", "running"].includes(job.state) && job;
    }, 300000);
    report.retry_state = terminal.state;
    report.retry_answer_status = terminal.result?.status;
    if (terminal.state !== "succeeded") throw new Error("Retry job failed");
    report.phase = "completed";
  } catch (error) {
    report.phase = "failed";
    report.error = String(error);
  }
  await write();
}
