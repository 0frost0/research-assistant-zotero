async function runNativeFixture() {
  const root = __FIXTURE_ROOT__;
  const qaQuestion = "Does retrieval need careful validation?";
  const path = name => PathUtils.join(root, name);
  const write = async value => Zotero.File.putContentsAsync(path("result.json"), JSON.stringify(value));
  const searchReady = async payload => {
    for (let attempt = 0; attempt < 240; attempt++) {
      const value = await ResearchBridge.request("search", payload);
      if (value.status !== "indexing") return value;
      await Zotero.Promise.delay(500);
    }
    throw new Error("Fixture index did not become ready");
  };
  const window = Zotero.getMainWindow();
  const original = { alert: window.alert, confirm: window.confirm, pane: Zotero.getActiveZoteroPane };
  const result = { phase: "startup", zotero_version: Zotero.version,
    menu: !!window.document.getElementById("research-assistant-menu") };
  let workbench;
  try {
    await write(result);
    // Assert the actual native settings window, not merely a mocked openDialog.
    const settingsPromise = ResearchBridge.configureEngine(window);
    const settings = ResearchBridge.settingsWindow;
    for (let attempt = 0; attempt < 100 && !settings?.document?.getElementById('engineWorkdir'); attempt++) {
      await Zotero.Promise.delay(100);
    }
    result.unified_settings = ['engineWorkdir', 'engineExecutable', 'engineScript', 'qwenSshHost', 'qwenServiceUnit']
      .every(id => !!settings?.document?.getElementById(id));
    if (!result.unified_settings) throw new Error('Unified settings fields missing');
    const tokenRoot = PathUtils.join(root, 'token-fixture');
    const tokenDir = PathUtils.join(tokenRoot, '.data', 'zotero');
    await IOUtils.makeDirectory(tokenDir, {createAncestors: true});
    await IOUtils.writeUTF8(PathUtils.join(tokenDir, 'connection-token'), 'synthetic-path-token');
    ResearchBridge.setPref('engineWorkdir', tokenRoot);
    ResearchBridge.setPref('endpoint', 'http://127.0.0.1:8765');
    await ResearchBridge.readLocalToken();
    result.native_token_path = ResearchBridge.pref('token') === 'synthetic-path-token';
    if (!result.native_token_path) throw new Error('Native token file read failed');
    const sshExecutable = await ResearchBridge.resolveSshExecutable();
    const sshVersion = await Subprocess.call({command: sshExecutable, arguments: ['-V'], stderr: 'stdout'});
    const versionOutput = await sshVersion.stdout.readString();
    const versionExit = await sshVersion.wait();
    result.native_ssh_executable = versionExit.exitCode === 0 && versionOutput.includes('OpenSSH');
    if (!result.native_ssh_executable) throw new Error('Native SSH executable check failed');
    settings.document.getElementById('suggest').click();
    if (!settings.document.getElementById('engineWorkdir').value.includes('research-assistant-zotero-github')) {
      throw new Error('Recommended settings not filled');
    }
    settings.document.getElementById('cancel').click();
    if (await settingsPromise !== false) throw new Error('Cancel should not save settings');
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
    const pane = Zotero.getActiveZoteroPane();
    await pane.collectionsView.selectByID("C" + collection.id);
    const selectedCollections = typeof pane.getSelectedCollections === "function"
      ? pane.getSelectedCollections() : [pane.getSelectedCollection()];
    result.native_collection_selection = selectedCollections.length === 1 && selectedCollections[0].id === collection.id;
    if (!result.native_collection_selection) throw new Error("Native collection selection failed");
    ResearchBridge.setPref("connection", "fixture");
    ResearchBridge.setPref("endpoint", "http://127.0.0.1:__FIXTURE_PORT__");
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
    // The workbench has its own content-window confirm; stub it in the
    // isolated fixture so the explicit retry confirmation is deterministic.
    if (doc?.defaultView) doc.defaultView.confirm = () => true;
    const controlIDs = [
      "engineStatus", "startEngine", "configureEngine", "configureConnection",
      "refreshBases", "syncCollection", "baseList", "baseTitle", "archiveBase",
      "exportBackup", "qaHistory", "newConversation", "writeBack", "query", "searchButton", "askButton", "members", "noteList",
    ];
    result.workbench = Boolean(workbench && !workbench.closed);
    result.workbench_title = doc?.querySelector("title")?.textContent || "";
    result.workbench_heading = doc?.querySelector("h1")?.textContent || "";
    result.controls = Object.fromEntries(controlIDs.map(id => [id, Boolean(doc?.getElementById(id))]));
    result.controls_ready = Object.values(result.controls).every(Boolean);
    doc.getElementById("newConversation").click();
    result.new_conversation_notice = doc.querySelector("#notice")?.textContent || "";
    result.base_list_count = doc?.querySelectorAll("#baseList .base-row").length || 0;
    result.active_base_title = doc?.getElementById("baseTitle")?.textContent || "";
    result.engine_status = doc?.getElementById("engineStatus")?.textContent || "";
    result.notice = doc?.getElementById("notice")?.textContent || "";
    result.startup_error = workbench?.ResearchAssistantWorkbench?.startupError || "";
    result.base_id = ResearchBridge.pref("lastBase");
    const snapshot = await ResearchBridge.request("bases/" + result.base_id, undefined, "GET");
    result.entities = snapshot.entities.length;
    result.kinds = snapshot.entities.map(e => e.kind);
    result.initial_memory_hint = doc.getElementById("scopeHint").textContent;
    if (!result.initial_memory_hint.includes("1 条可检索个人记录")) {
      throw new Error("Native Zotero note was not counted in the opened library");
    }
    // Check the native Zotero note before creating any local assistant note.
    // It must stay in the personal-memory lane and retain a Zotero item URL.
    let externalSearch;
    for (let attempt = 0; attempt < 240; attempt++) {
      externalSearch = await ResearchBridge.request("search", {
        kb_ids: [result.base_id], scope: "notes", k: 5,
        query: "Synthetic scientific evidence note"
      });
      if (externalSearch.status !== "indexing") break;
      await Zotero.Promise.delay(500);
    }
    if (externalSearch.status === "indexing") throw new Error("External note index did not become ready");
    const externalHit = externalSearch.results?.find(hit => hit.kind === "external_note");
    result.external_note_hits = externalSearch.results?.filter(hit => hit.kind === "external_note").length || 0;
    result.external_note_url = externalHit?.url || "";
    // Seed one confirmed personal record so the default "文献与笔记" query
    // exercises both source lanes in the isolated fixture.
    await ResearchBridge.request("notes", {
      kb_id: result.base_id, type: "understanding", title: "Synthetic understanding",
      body: "我记录：检索需要谨慎核验。Retrieval needs careful validation. scientific evidence", confirmed: true,
      anchors: [{source: snapshot.entities.find(e => e.kind === "document")?.data?.hash + ".pdf",
                 page: 1, quote: "Scientific evidence from original paper.", kind: "original",
                 url: `zotero://open-pdf/library/items/${snapshot.entities.find(e => e.kind === "document")?.data?.key}?page=1`}]
    });
    doc.getElementById("refreshBases").click();
    for (let attempt = 0; attempt < 100; attempt++) {
      if (doc.getElementById("scopeHint").textContent.includes("2 条可检索个人记录")) break;
      await Zotero.Promise.delay(100);
    }
    result.memory_hint_after_create = doc.getElementById("scopeHint").textContent;
    if (!result.memory_hint_after_create.includes("2 条可检索个人记录")) {
      throw new Error("Local note count did not refresh alongside the native note");
    }
    doc.getElementById("query").value = qaQuestion;
    doc.getElementById("askButton").click();
    for (let attempt = 0; attempt < __QA_WAIT_ATTEMPTS__; attempt++) {
      if (doc.querySelector(".answer-claim") && !doc.getElementById("askButton").disabled) break;
      await Zotero.Promise.delay(100);
    }
    result.qa_claims = doc.querySelectorAll(".answer-claim").length;
    result.qa_quote = [...doc.querySelectorAll(".answer-claim blockquote")]
      .some(node => node.textContent.toLowerCase().includes("validation"));
    result.qa_sections = [...doc.querySelectorAll(".answer-section-title")].map(n => n.textContent);
    result.qa_usage = doc.getElementById("resultMeta").textContent;
    // The async job is durable; the plugin must expose it without resubmitting
    // the model request, and must be able to reopen the stored answer.
    doc.getElementById("qaHistory").click();
    for (let attempt = 0; attempt < 100; attempt++) {
      if (doc.querySelector(".history-row")) break;
      await Zotero.Promise.delay(100);
    }
    result.qa_history_rows = doc.querySelectorAll(".history-row").length;
    result.qa_history_question = doc.querySelector(".history-row p")?.textContent || "";
    const historyOpen = doc.querySelector(".history-row .source-button");
    historyOpen?.click();
    for (let attempt = 0; attempt < 100; attempt++) {
      if (doc.querySelector(".answer-claim")) break;
      await Zotero.Promise.delay(100);
    }
    result.qa_history_reopened = Boolean(doc.querySelector(".answer-claim"));
    const launch = Zotero.launchURL;
    try {
      Zotero.launchURL = url => { result.qa_source_url = url; };
      doc.querySelector(".answer-claim .source-button")?.click();
      const noteTitle = [...doc.querySelectorAll(".answer-section-title")]
        .find(node => node.textContent === "你的个人记录");
      const noteClaim = noteTitle?.nextElementSibling;
      const noteButtons = noteClaim ? [...noteClaim.querySelectorAll(".source-button")] : [];
      Zotero.launchURL = url => { result.qa_note_source_url = url; };
      noteButtons[noteButtons.length - 1]?.click();
    } finally { Zotero.launchURL = launch; }
    const feedbackButton = doc.querySelector('.qa-feedback button[data-rating="helpful"]');
    result.qa_feedback_button = Boolean(feedbackButton);
    feedbackButton?.click();
    for (let attempt = 0; attempt < 100; attempt++) {
      if (doc.querySelector(".qa-feedback")?.dataset.saved === "true") break;
      await Zotero.Promise.delay(100);
    }
    result.qa_feedback_saved = doc.querySelector(".qa-feedback")?.dataset.saved === "true";
    const draftButton = [...doc.querySelectorAll("button")]
      .find(button => button.textContent === "保存为 AI 草稿");
    result.ai_draft_button = Boolean(draftButton);
    draftButton?.click();
    for (let attempt = 0; attempt < 100; attempt++) {
      if (draftButton?.dataset.saved === "true" || draftButton?.dataset.error) break;
      await Zotero.Promise.delay(100);
    }
    result.ai_draft_error = draftButton?.dataset.error || "";
    const afterDraft = await ResearchBridge.request("bases/" + result.base_id, undefined, "GET");
    const draft = afterDraft.notes.find(note => note.data.author_source === "ai");
    result.ai_draft_anchors = draft?.data.anchors?.length || 0;
    result.ai_draft_saved = Boolean(draft && !draft.data.confirmed &&
      draft.data.source_status === "linked" && result.ai_draft_anchors > 0);
    if (draft) {
      const draftSearch = await searchReady({kb_ids: [result.base_id], scope: "notes", query: draft.data.body, k: 20});
      result.ai_draft_excluded_before_confirmation = !draftSearch.results.some(hit => hit.entity_id === draft.id);
      const confirmedDraft = await ResearchBridge.request("notes/" + draft.id, {
        kb_id: result.base_id, title: draft.data.title, body: "用户核对后的合成结论",
        type: draft.data.type, confirmed: true, anchors: draft.data.anchors,
        expected_revision: draft.revision,
      });
      result.ai_draft_confirmed = confirmedDraft.author_source === "ai" &&
        confirmedDraft.confirmed && confirmedDraft.revision === draft.revision + 1 &&
        confirmedDraft.source_status === "linked" && confirmedDraft.anchors?.length === result.ai_draft_anchors;
    }
    // Versioned personal memory: read the latest revision, then exclude archive.
    const memory = await ResearchBridge.request("notes", {kb_id: result.base_id, confirmed: true,
      title: "Versioned synthetic memory", body: "Personal synthetic memory: the trial size is five cases."});
    const updatedMemory = await ResearchBridge.request("notes/" + memory.id, {kb_id: result.base_id,
      expected_revision: memory.revision, confirmed: true, title: memory.title,
      body: "Personal synthetic memory: the trial size is seven cases."});
    const memoryResult = await ResearchBridge.request("ask", {kb_ids: [result.base_id], scope: "notes",
      query: "In my personal synthetic memory, what is the trial size?"});
    result.memory_latest_revision = memoryResult.claims?.some(claim => claim.citations.some(cite =>
      cite.entity_id === memory.id && cite.revision === updatedMemory.revision &&
      cite.support_quote.includes("seven cases"))) || false;
    result.memory_model_visible = Boolean(memoryResult.trace?.model);
    result.memory_timing_visible = Number.isInteger(memoryResult.trace?.elapsed_ms);
    await ResearchBridge.request("notes/" + memory.id, {kb_id: result.base_id,
      expected_revision: updatedMemory.revision, archived: true,
      title: updatedMemory.title, body: updatedMemory.body, confirmed: true});
    const archivedSearch = await searchReady({kb_ids: [result.base_id], scope: "notes", k: 20,
      query: "Personal synthetic memory trial size"});
    result.memory_archive_excluded = !archivedSearch.results.some(hit => hit.entity_id === memory.id);
    if (__SUFFICIENCY_FIXTURE__) {
      result.phase = "sufficiency"; await write(result);
      doc.querySelector('[name="scope"][value="original"]').click();
      doc.getElementById("newConversation").click();
      const gate = doc.getElementById("sufficiencyGate");
      result.sufficiency_default_off = gate.checked === false;
      gate.checked = true;
      const gateRun = async question => {
        doc.getElementById("query").value = question;
        doc.getElementById("askButton").click();
        for (let attempt = 0; attempt < __QA_WAIT_ATTEMPTS__; attempt++) {
          if (!doc.getElementById("askButton").disabled) break;
          await Zotero.Promise.delay(100);
        }
        const jobs = await ResearchBridge.request("jobs", undefined, "GET");
        const job = jobs.jobs.find(j => j.question === question && j.state === "succeeded");
        return job ? await ResearchBridge.request("jobs/" + job.job_id, undefined, "GET") : null;
      };
      const passed = await gateRun(qaQuestion);
      result.sufficiency_enabled_in_request = passed?.result?.trace?.sufficiency_gate === "experimental-model-v1";
      result.sufficiency_answered = passed?.result?.status === "answered" &&
        passed?.result?.trace?.calls?.map(c => c.role).join(",") === "sufficiency,answer,review";
      result.sufficiency_trace_visible = doc.getElementById("results").textContent.includes("未经独立校准");
      const rejected = await gateRun("fixture missing condition");
      result.sufficiency_rejected_before_generation = rejected?.result?.status === "no_evidence" &&
        rejected.result.claims.length === 0 && rejected.result.trace.calls.length === 2 &&
        rejected.result.trace.calls.every(c => c.role === "sufficiency") &&
        rejected.result.trace.supplementary_retrievals === 1;
      gate.checked = false;
      if (!result.sufficiency_default_off || !result.sufficiency_enabled_in_request ||
          !result.sufficiency_answered || !result.sufficiency_trace_visible ||
          !result.sufficiency_rejected_before_generation) throw new Error("Native sufficiency contract failed");
    }
    if (__VISUAL_FIXTURE__) {
      result.phase = "visual"; await write(result);
      doc.querySelector('[name="scope"][value="original"]').click();
      doc.getElementById("newConversation").click();
      doc.getElementById("multimodalEvidence").checked = true;
      const visualQuestion = "Which bar is taller in the synthetic two-bar chart? Reply with one visual claim and set its text and observation to exactly blue or orange.";
      doc.getElementById("query").value = visualQuestion;
      doc.getElementById("askButton").click();
      for (let attempt = 0; attempt < __QA_WAIT_ATTEMPTS__; attempt++) {
        if (doc.querySelector(".visual-observation") && !doc.getElementById("askButton").disabled) break;
        await Zotero.Promise.delay(100);
      }
      const observation = doc.querySelector(".visual-observation");
      result.visual_label = observation?.textContent || "";
      const visualClaim = observation?.closest(".answer-claim");
      result.visual_not_exact_quote = Boolean(visualClaim && !visualClaim.querySelector("blockquote"));
      result.visual_no_source_draft = Boolean(visualClaim && ![...visualClaim.querySelectorAll("button")]
        .some(button => button.textContent === "保存为 AI 草稿"));
      [...visualClaim?.querySelectorAll("button") || []].find(b => b.textContent === "查看引用图片")?.click();
      for (let attempt = 0; attempt < 100; attempt++) {
        const image = doc.querySelector("img.visual-evidence");
        if (image?.complete && image.naturalWidth > 0) break;
        await Zotero.Promise.delay(100);
      }
      const image = doc.querySelector("img.visual-evidence");
      result.visual_native_image_decoded = Boolean(image?.complete && image.naturalWidth === 240 && image.naturalHeight === 180);
      const jobs = (await ResearchBridge.request("jobs", undefined, "GET")).jobs;
      const visualJob = jobs.find(job => job.question === visualQuestion);
      const saved = visualJob && await ResearchBridge.request("jobs/" + visualJob.job_id, undefined, "GET");
      const citation = saved?.result?.claims?.[0]?.citations?.[0];
      result.visual_durable_provenance = citation?.citation_type === "visual" &&
        citation?.observation_source === "model_derived" && !!citation?.visual_asset?.image_hash;
      result.visual_no_persisted_pixels = Boolean(saved && !JSON.stringify(saved).includes("data:image"));
      result.visual_model = saved?.result?.trace?.model;
      result.visual_model_calls = saved?.result?.trace?.model_calls;
      result.visual_status = saved?.result?.status;
      result.visual_source_matches_pdf = citation?.source_id === snapshot.entities.find(e => e.kind === "document")?.data?.hash + ".pdf";
      doc.getElementById("qaHistory").click();
      for (let attempt = 0; attempt < 100; attempt++) {
        if ([...doc.querySelectorAll(".history-row")].some(row => row.querySelector("p")?.textContent === visualQuestion)) break;
        await Zotero.Promise.delay(100);
      }
      const visualRow = [...doc.querySelectorAll(".history-row")].find(row => row.querySelector("p")?.textContent === visualQuestion);
      visualRow?.querySelector(".source-button")?.click();
      for (let attempt = 0; attempt < 100; attempt++) {
        if (doc.querySelector(".visual-observation")) break;
        await Zotero.Promise.delay(100);
      }
      result.visual_history_reopened = !!doc.querySelector(".visual-observation");
      doc.getElementById("multimodalEvidence").checked = false;
      if (!result.visual_label.includes("非原文摘录") || !result.visual_label.includes("blue") ||
          !result.visual_not_exact_quote || !result.visual_no_source_draft || !result.visual_native_image_decoded ||
          !result.visual_durable_provenance || !result.visual_no_persisted_pixels || !result.visual_history_reopened ||
          !result.visual_source_matches_pdf || result.visual_model_calls !== 2 || result.visual_status !== "answered") {
        throw new Error("Native synthetic visual acceptance mismatch");
      }
    }
    // Exercise the explicit failed-job recovery path after the normal answer,
    // source and AI-draft assertions have completed. The fixture server raises
    // only for this synthetic question; no provider request is made.
    doc.querySelector('[name="scope"][value="original"]').click();
    if (doc.getElementById("askButton").textContent !== "依据论文回答") {
      throw new Error("Answer scope button did not update");
    }
    doc.getElementById("query").value = "fixture failure";
    result.retry_stage = "before_failure_ask"; await write(result);
    doc.getElementById("askButton").click();
    for (let attempt = 0; attempt < 200; attempt++) {
      if (!doc.getElementById("askButton").disabled) break;
      await Zotero.Promise.delay(100);
    }
    result.retry_stage = "after_failure_ask"; await write(result);
    for (let attempt = 0; attempt < 100; attempt++) {
      const jobs = (await ResearchBridge.request("jobs", undefined, "GET")).jobs || [];
      if (jobs.some(job => job.question === "fixture failure" && job.state === "failed")) break;
      await Zotero.Promise.delay(100);
    }
    result.retry_stage = "after_failed_job_poll"; await write(result);
    doc.getElementById("qaHistory").click();
    for (let attempt = 0; attempt < 100; attempt++) {
      if ([...doc.querySelectorAll(".history-row")].some(row =>
          row.querySelector("p")?.textContent === "fixture failure" &&
          row.textContent.includes("失败"))) break;
      await Zotero.Promise.delay(100);
    }
    result.retry_stage = "after_history_failed_row"; await write(result);
    const historyRows = [...doc.querySelectorAll(".history-row")];
    result.history_failure_rows = historyRows.filter(row =>
      row.querySelector("p")?.textContent === "fixture failure").length;
    result.history_row_texts = historyRows.map(row => row.textContent);
    const failedRow = historyRows
      .find(row => row.querySelector("p")?.textContent === "fixture failure");
    result.failed_row_button_texts = [...failedRow?.querySelectorAll("button") || []]
      .map(button => button.textContent);
    result.failed_history_retry_button = result.failed_row_button_texts.includes("手动重试");
    await write(result);
    result.retry_stage = "before_retry_click"; await write(result);
    [...failedRow?.querySelectorAll("button") || []]
      .find(button => button.textContent === "手动重试")?.click();
    if (result.failed_history_retry_button) {
      for (let attempt = 0; attempt < 100; attempt++) {
        const jobs = (await Promise.race([
          ResearchBridge.request("jobs", undefined, "GET"),
          new Promise((_, reject) => Zotero.Promise.delay(3000).then(() =>
            reject(new Error("retry job poll timeout"))))
        ])).jobs || [];
        if (jobs.some(job => job.question === "fixture failure" && job.retry_count === 1)) {
          result.failed_history_retry_created = true;
          break;
        }
        await Zotero.Promise.delay(100);
      }
    }
    result.retry_stage = "after_retry_attempt"; await write(result);
    const isZoteroSource = url => url?.startsWith("zotero://open-pdf/") || url?.startsWith("zotero://select/");
    if (!result.qa_claims || !result.qa_quote || !result.qa_source_url?.startsWith("zotero://open-pdf/") ||
        !isZoteroSource(result.qa_note_source_url)) {
      throw new Error("QA UI/source flow failed");
    }
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
        result.entities !== 2 || result.writeback_count !== 1 ||
        !result.external_note_hits || !result.external_note_url?.startsWith("zotero://select/") ||
        !result.new_conversation_notice.includes("清除当前追问上下文") ||
        !result.qa_sections.includes("论文原文依据") || !result.qa_sections.includes("你的个人记录") ||
        !result.controls.qaHistory || !result.qa_history_rows ||
        !result.qa_history_question.includes(qaQuestion) || !result.qa_history_reopened ||
        !result.qa_feedback_button || !result.qa_feedback_saved ||
        !result.failed_history_retry_button || !result.failed_history_retry_created ||
        !result.ai_draft_button || !result.ai_draft_saved || !result.ai_draft_confirmed ||
        !result.ai_draft_excluded_before_confirmation || !result.memory_latest_revision ||
        !result.memory_model_visible || !result.memory_timing_visible || !result.memory_archive_excluded) {
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
