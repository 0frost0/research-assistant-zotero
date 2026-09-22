async function runNativeFixture() {
  const root = __FIXTURE_ROOT__;
  const path = name => PathUtils.join(root, name);
  const write = async value => Zotero.File.putContentsAsync(path("result.json"), JSON.stringify(value));
  const window = Zotero.getMainWindow();
  const original = { alert: window.alert, confirm: window.confirm, pane: Zotero.getActiveZoteroPane };
  const result = { phase: "startup", menu: !!window.document.getElementById("research-assistant-menu") };
  let workbench;
  try {
    await write(result);
    const expectedData = (root + "/data").replace(/\\/g, "/");
    if (Zotero.DataDirectory.dir.replace(/\\/g, "/") !== expectedData) throw new Error("Not isolated data");
    const collection = new Zotero.Collection();
    collection.libraryID = Zotero.Libraries.userLibraryID;
    collection.name = "Native synthetic collection";
    await collection.saveTx();
    const item = new Zotero.Item("journalArticle");
    item.libraryID = collection.libraryID;
    item.setField("title", "Synthetic Research Evidence");
    item.setCollections([collection.id]);
    await item.saveTx();
    await Zotero.Attachments.importFromFile({ file: path("synthetic.pdf"), parentItemID: item.id });
    const note = new Zotero.Item("note");
    note.libraryID = item.libraryID;
    note.parentID = item.id;
    note.setNote("<p>Synthetic scientific evidence note.</p>");
    await note.saveTx();
    result.phase = "created"; await write(result);
    window.alert = () => {};
    window.confirm = () => true;
    Zotero.getActiveZoteroPane = () => ({ getSelectedCollection: () => collection });
    ResearchBridge.setPref("endpoint", "http://127.0.0.1:8771");
    ResearchBridge.setPref("token", "synthetic-acceptance-token");
    await ResearchBridge.sync(window);
    workbench = ResearchBridge.workbench;
    for (let attempt = 0; attempt < 100 && !workbench?.ResearchAssistantWorkbench?.ready; attempt++) {
      await Zotero.Promise.delay(100);
    }
    await workbench?.ResearchAssistantWorkbench?.ready;
    for (let attempt = 0; attempt < 100; attempt++) {
      const doc = workbench?.document;
      if (doc?.getElementById("baseTitle")?.textContent === collection.name &&
          doc.querySelector("#baseList .base-row")) break;
      await Zotero.Promise.delay(100);
    }
    const doc = workbench?.document;
    const controlIDs = [
      "engineStatus", "startEngine", "configureEngine", "configureConnection",
      "refreshBases", "syncCollection", "baseList", "baseTitle", "archiveBase",
      "exportBackup", "writeBack", "query", "searchButton", "members", "noteList",
    ];
    result.workbench = Boolean(workbench && !workbench.closed);
    result.workbench_title = doc?.querySelector("title")?.textContent || "";
    result.workbench_heading = doc?.querySelector("h1")?.textContent || "";
    result.controls = Object.fromEntries(controlIDs.map(id => [id, Boolean(doc?.getElementById(id))]));
    result.controls_ready = Object.values(result.controls).every(Boolean);
    result.base_list_count = doc?.querySelectorAll("#baseList .base-row").length || 0;
    result.active_base_title = doc?.getElementById("baseTitle")?.textContent || "";
    result.engine_status = doc?.getElementById("engineStatus")?.textContent || "";
    result.notice = doc?.getElementById("notice")?.textContent || "";
    result.startup_error = workbench?.ResearchAssistantWorkbench?.startupError || "";
    result.base_id = ResearchBridge.pref("lastBase");
    const snapshot = await ResearchBridge.request("bases/" + result.base_id, undefined, "GET");
    result.entities = snapshot.entities.length;
    result.kinds = snapshot.entities.map(e => e.kind);
    const created = await ResearchBridge.request("notes", { kb_id: result.base_id, title: "Synthetic topic note", body: "Synthetic confirmed observation.", confirmed: true });
    await ResearchBridge.writeNotes(window);
    await ResearchBridge.writeNotes(window);
    const search = new Zotero.Search();
    search.libraryID = item.libraryID;
    search.addCondition("tag", "is", `research-assistant/${created.id}/${created.revision}`);
    result.writeback_count = (await search.search()).length;
    if (!result.menu || !result.workbench || result.workbench_title !== "科研助手" ||
        result.workbench_heading !== "科研助手" || !result.controls_ready ||
        result.base_list_count < 1 || result.active_base_title !== collection.name ||
        result.entities !== 2 || result.writeback_count !== 1) {
      throw new Error("Acceptance mismatch");
    }
    result.phase = "passed";
  } catch (error) {
    result.phase = "failed";
    result.error = String(error);
    result.stack = error.stack;
  } finally {
    window.alert = original.alert;
    window.confirm = original.confirm;
    Zotero.getActiveZoteroPane = original.pane;
    if (workbench && !workbench.closed) workbench.close();
    await write(result);
  }
}
