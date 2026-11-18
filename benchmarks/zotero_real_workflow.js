async function runRealQuestions() {
  const config = __RUN_CONFIG__;
  const report = {phase: "starting", zotero_version: Zotero.version, model_calls: 0,
    fixture_note_is_acceptance_marker: true, human_semantic_accuracy: null};
  const write = () => IOUtils.writeUTF8(PathUtils.join(config.root, "result.json"), JSON.stringify(report));
  const wait = async (check, label, attempts = 200) => {
    for (let i = 0; i < attempts; i++) { if (await check()) return; await Zotero.Promise.delay(100); }
    throw new Error("Timed out: " + label);
  };
  const main = Zotero.getMainWindow();
  const original = {confirm: main.confirm, alert: main.alert, request: ResearchBridge.request};
  let win;
  try {
    if (Zotero.DataDirectory.dir.replace(/\\/g, "/") !== (config.root + "/data").replace(/\\/g, "/"))
      throw new Error("Refusing non-isolated Zotero data directory");
    ResearchBridge.setPref("connection", config.connection);
    ResearchBridge.setPref("endpoint", config.endpoint);
    ResearchBridge.setPref("token", (await IOUtils.readUTF8(PathUtils.join(config.root, "backend", ".data", "zotero", "connection-token"))).trim());
    main.confirm = () => true;
    main.alert = () => {};
    let uploads = 0, parseResponse;
    ResearchBridge.request = async function(action, payload, method) {
      const value = await original.request.call(this, action, payload, method);
      if (action.startsWith("objects/") && method === "PUT") uploads++;
      if (action === "parse") parseResponse = value;
      return value;
    };
    const collection = new Zotero.Collection();
    collection.libraryID = Zotero.Libraries.userLibraryID;
    collection.name = "Isolated Go workflow acceptance";
    await collection.saveTx();
    const item = new Zotero.Item("journalArticle");
    item.libraryID = collection.libraryID;
    item.setField("title", "Real library PDF — isolated workflow copy");
    item.setCollections([collection.id]);
    await item.saveTx();
    await Zotero.Attachments.importFromFile({file: PathUtils.join(config.root, "workflow-paper.pdf"), parentItemID: item.id});
    const note = new Zotero.Item("note");
    note.libraryID = item.libraryID;
    note.parentID = item.id;
    note.setNote("<p>Isolated workflow acceptance marker; not research evidence.</p>");
    await note.saveTx();
    await Zotero.getActiveZoteroPane().collectionsView.selectByID("C" + collection.id);
    await ResearchBridge.sync(main);
    const base = ResearchBridge.pref("lastBase");
    const snapshot = await ResearchBridge.request("bases/" + base, undefined, "GET");
    report.uploads = uploads;
    report.synced_entities = snapshot.entities.length;
    report.synced_pdf = snapshot.entities.some(e => e.kind === "document" && e.data.hash === config.workflow_sha);
    report.synced_note = snapshot.entities.some(e => e.kind === "note");
    if (uploads !== 1 || !report.synced_pdf || !report.synced_note) throw new Error("Sync/upload contract failed");
    win = ResearchBridge.workbench;
    await wait(() => Boolean(win?.ResearchAssistantWorkbench?.ready), "workbench ready");
    await win.ResearchAssistantWorkbench.ready;
    const doc = win.document;
    await wait(() => doc.getElementById("baseTitle").textContent === collection.name, "synced base");
    doc.getElementById("newNote").click();
    doc.getElementById("noteTitle").value = "Workflow revision marker";
    doc.getElementById("noteBody").value = "Acceptance marker revision one; not research evidence.";
    doc.getElementById("noteConfirmed").checked = true;
    doc.getElementById("saveNote").click();
    await wait(() => doc.getElementById("noteForm").hidden, "save note");
    let saved = (await ResearchBridge.request("bases/" + base, undefined, "GET")).notes.find(n => n.data.title === "Workflow revision marker");
    if (!saved) throw new Error("Native note missing");
    await ResearchBridge.request("notes/" + saved.id, {kb_id: base, expected_revision: saved.revision,
      title: saved.data.title, body: "Acceptance marker revision two; not research evidence.", confirmed: true});
    doc.getElementById("refreshBases").click();
    await wait(() => doc.getElementById("noteList").textContent.includes("Acceptance marker revision two"), "refreshed note revision");
    [...doc.querySelectorAll("#noteList button")].find(b => b.textContent === "历史").click();
    await wait(() => doc.getElementById("noteList").textContent.includes("历史版本（2）"), "note history rendering");
    report.note_history_rendered = true;
    const history = await ResearchBridge.request("history/" + saved.id, undefined, "GET");
    report.note_revisions = history.map(n => n.revision).sort();
    report.phase = "parsing"; await write();
    doc.getElementById("parseSelected").click();
    await wait(() => Boolean(parseResponse), "parse submission");
    const jobs = parseResponse.jobs;
    report.parsing_submissions = jobs.length;
    if (jobs.length !== 1) throw new Error("Expected one PDF parse submission");
    if (jobs[0].cached) {
      report.parsing_status = "succeeded";
      report.parsing_cached = true;
    } else {
      await wait(async () => {
        const state = (await ResearchBridge.request("parsing", undefined, "GET")).jobs.find(j => j.id === jobs[0].id);
        report.parsing_status = state?.status;
        return state && !["queued", "running"].includes(state.status);
      }, "MinerU parsing", 12000);
      report.parsing_cached = false;
    }
    if (report.parsing_status !== "succeeded") throw new Error("MinerU parsing failed");
    report.phase = "completed";
  } catch (error) {
    report.phase = "failed";
    report.error = String(error);
  } finally {
    ResearchBridge.request = original.request;
    main.confirm = original.confirm;
    main.alert = original.alert;
  }
  await write();
}
