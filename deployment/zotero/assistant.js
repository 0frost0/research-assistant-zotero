const $ = id => document.getElementById(id);
const args = window.arguments?.[0] || {};
const bridge = args.bridge;
const labels = {
  document: "原文",
  original: "原文",
  note: "外部笔记",
  annotation: "外部批注",
  external_note: "外部笔记",
  external_annotation: "外部批注",
  user_note: "专题笔记",
};

let bases = [];
let selected = new Set();
let active = null;
let snapshot = null;
let editing = null;
let editorBaseline = "";
let savingNote = false;
let citationEpoch = 0;

function editorValue() {
  return JSON.stringify([$("noteType").value, $("noteTitle").value,
    $("noteBody").value, $("noteConfirmed").checked]);
}

function hasUnsavedNote() {
  return !$("noteForm").hidden && editorBaseline !== "" && editorValue() !== editorBaseline;
}

function mayReplaceEditor() {
  if (savingNote) { notice("笔记正在保存，请稍候。"); return false; }
  return !hasUnsavedNote() || window.confirm("笔记尚未保存。确定放弃本次修改吗？取消可返回继续编辑。");
}

window.addEventListener("beforeunload", event => {
  if (hasUnsavedNote() || savingNote) {
    event.preventDefault();
    event.returnValue = "笔记尚未保存";
  }
});
let searchEpoch = 0;
let comparisonSelected = new Set();
let pendingAnchors = [];
let activeNoteCount = null;
let baseLoadEpoch = 0;
// A workbench session has one bounded follow-up context. The server also
// scopes it by selected knowledge-base IDs before it can influence retrieval.
function newConversationId() {
  return `zotero-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}
let conversationId = newConversationId();

function node(tag, text = "", className = "") {
  const element = document.createElement(tag);
  element.textContent = text;
  if (className) element.className = className;
  return element;
}

function notice(message = "") {
  $("notice").textContent = message;
}
let serviceConfigBusy = false;
async function openServiceConfig() {
  if (serviceConfigBusy) return;
  serviceConfigBusy = true;
  const panel = $("serviceConfigPanel");
  panel.hidden = false;
  $("serviceConfigFields").disabled = true;
  $("serviceLlmKey").value = "";
  $("serviceEmbeddingKey").value = "";
  try {
    const config = await bridge.request("services", undefined, "GET");
    for (const [section, prefix] of [["llm", "serviceLlm"], ["embedding", "serviceEmbedding"]]) {
      $(prefix + "BaseUrl").value = config[section].base_url || "";
      $(prefix + "Model").value = config[section].model || (section === "embedding" ? "qwen3-vl-embedding" : "");
      $(prefix + "Key").placeholder = config[section].has_api_key ? "已配置；留空保留" : "请输入密钥";
    }
    $("serviceMineruMode").value = config.mineru.mode || 'remote';
    $("serviceEmbeddingDimension").value = config.embedding.dimension || 2048;
    $("serviceEmbeddingRevision").value = config.embedding.revision || "1";
    $("serviceMineruUrl").value = config.mineru.url || "";
    $("serviceMineruTimeout").value = config.mineru.timeout;
    $("serviceMineruPython").value = config.mineru.python || "";
    $("serviceMineruAuto").checked = config.mineru.auto_parse === true;
    $("serviceConfigFields").disabled = false;
    $("serviceLlmBaseUrl").focus();
    notice("已读取配置；用于后续任务，保存不代表连接验证通过。");
  } catch (error) {
    notice(error.message); // Load failure keeps the form disabled: no accidental overwrite.
  } finally {
    serviceConfigBusy = false;
  }
}

function validateServiceUrl(value, label) {
  let url;

  try {
    url = new URL(value.trim());
  } catch (_) {
    throw new Error(`${label}不是有效 URL。`);
  }

  const localHosts = [
    "127.0.0.1",
    "localhost",
    "[::1]",
  ];

  const isLocal =
    localHosts.includes(url.hostname);

  const isAllowedProtocol =
    url.protocol === "https:" ||
    (url.protocol === "http:" && isLocal);

  if (
    url.username ||
    url.password ||
    url.search ||
    url.hash ||
    !isAllowedProtocol
  ) {
    throw new Error(
      `${label}必须使用 HTTPS；` +
      `本机调试才允许 HTTP，` +
      `且不能包含用户名、密码、查询参数或 fragment。`
    );
  }

  return url.href.replace(/\/$/, "");
}

// Keep the visible scope explanation aligned with the request payload. This
// matters because "文献与笔记" is a deliberately separated answer mode:
// personal notes must never be presented as paper facts.
function updateScopeCopy() {
  const scope = document.querySelector("[name=scope]:checked")?.value || "both";
  // The opened library is not necessarily the checkbox-selected search scope.
  const baseLabel = `当前查看的“${active?.name || "知识库"}”`;
  const noteStatus = activeNoteCount === 0
    ? `${baseLabel}暂无已确认个人记录，可同步 Zotero 笔记或创建专题笔记。`
    : activeNoteCount == null
      ? ""
      : `${baseLabel}有 ${activeNoteCount} 条可检索个人记录。`;
  const copy = {
    both: {
      button: "文献与笔记分开回答",
      hint: `论文原文与个人笔记分开回答；最多补检索一次。个人记录不会升级为论文事实。${noteStatus ? ` ${noteStatus}` : ""}`,
    },
    original: {
      button: "依据论文回答",
      hint: `只检索所选库的论文原文；回答会附带原文证据和页码。${noteStatus ? ` ${noteStatus}` : ""}`,
    },
    notes: {
      button: "根据我的笔记回答",
      hint: `只检索当前有效、已确认的个人记录；结果会保留笔记 ID、revision 和来源。${noteStatus ? ` ${noteStatus}` : ""}`,
    },
  }[scope];
  $("askButton").textContent = copy.button;
  $("scopeHint").textContent = copy.hint + " 回答会将所选范围的证据发送给已配置模型。";
}

function showView(view) {
  for (const name of ["results", "members", "notes"]) {
    $(name + "View").hidden = name !== view;
  }
  for (const button of document.querySelectorAll("[data-view]")) {
    button.setAttribute("aria-selected", String(button.dataset.view === view));
  }
}

function setBusy(button, busy, label) {
  button.disabled = busy;
  if (label) button.textContent = busy ? label : button.dataset.label;
}

async function updateEngineStatus() {
  const status = await bridge.backendStatus();
  $("engineStatus").textContent = status.connected
    ? `Python 已连接 · ${status.backend}`
    : "Python 未连接";
  $("engineStatus").className = "status " + (status.connected ? "online" : "offline");
  return status;
}

function renderBases() {
  $("baseList").replaceChildren();
  for (const base of bases.filter(item => $("showArchived").checked || !item.archived)) {
    const row = node("div", "", "base-row" + (active?.id === base.id ? " active" : ""));
    const check = document.createElement("input");
    check.type = "checkbox";
    check.checked = selected.has(base.id);
    check.disabled = Boolean(base.archived);
    check.setAttribute("aria-label", "检索 " + base.name);
    check.addEventListener("change", () => {
      if (check.checked) selected.add(base.id);
      else selected.delete(base.id);
      searchEpoch++;
      $("results").replaceChildren(node("p", "检索范围已更新", "empty"));
    });
    const content = document.createElement("div");
    const name = node("div", base.name, "base-name");
    name.tabIndex = 0;
    const open = () => selectBase(base).catch(error => notice(error.message));
    name.addEventListener("click", open);
    name.addEventListener("keydown", event => {
      if (event.key === "Enter" || event.key === " ") open();
    });
    content.append(
      name,
      node("div", `${base.member_count} 条资料 · v${base.revision}${base.archived ? " · 已归档" : ""}`, "muted")
    );
    row.append(check, content);
    $("baseList").append(row);
  }
  if (!$("baseList").children.length) {
    $("baseList").append(node("p", "尚无知识库，请先同步 Zotero 分类。", "empty"));
  }
}

async function selectBase(base, {preserveEditor = false} = {}) {
  if (!preserveEditor && !mayReplaceEditor()) return false;
  const generation = ++baseLoadEpoch;
  active = base;
  snapshot = null;
  activeNoteCount = null;
  updateScopeCopy();
  if (!preserveEditor) {
    editing = null;
    editorBaseline = "";
    $("noteForm").hidden = true;
  }
  $("members").replaceChildren();
  $("noteList").replaceChildren(node("p", "正在载入个人记录…", "empty"));
  $("compareButton").disabled = true;
  $("baseTitle").textContent = base.name;
  $("baseMeta").textContent =
    `${base.subtree ? "含子分类" : "仅当前分类"} · 版本 ${base.revision}${base.archived ? " · 已归档" : ""}`;
  $("archiveBase").disabled = false;
  $("archiveBase").textContent = base.archived ? "恢复" : "归档";
  renderBases();
  if (base.archived) {
    $("members").replaceChildren();
    $("noteList").replaceChildren(node("p", "该知识库已归档。", "empty"));
    return;
  }
  const loaded = await bridge.request("bases/" + base.id, undefined, "GET");
  // Rapid library switching can complete requests out of order. Never show
  // another library's notes beneath the current title or edit them there.
  if (generation !== baseLoadEpoch) return;
  snapshot = loaded;
  // Snapshot kinds differ from search hit kinds (external_note/user_note).
  // Count only confirmed records with text, matching index eligibility.
  const external = (snapshot.entities || []).filter(entity =>
    ["note", "annotation"].includes(entity.kind) && entity.data?.confirmed === true
    && `${entity.data.body || ""}\n${entity.data.comment || ""}`.trim());
  const local = (snapshot.notes || []).filter(note => note.data?.confirmed === true
    && !note.data.archived && (note.data.body || "").trim());
  activeNoteCount = external.length + local.length;
  updateScopeCopy();
  renderMembers();
  renderNotes();
}

function renderMembers() {
  $("members").replaceChildren();
  for (const entity of snapshot?.entities || []) {
    const row = document.createElement("tr");
    const select = document.createElement("td");
    if (entity.data.kind === "document" && entity.data.hash) {
      const check = document.createElement("input");
      check.type = "checkbox";
      const source = entity.data.hash + ".pdf";
      check.checked = comparisonSelected.has(source);
      check.setAttribute("aria-label", "选择用于多文献对照");
      check.addEventListener("change", () => {
        if (check.checked) comparisonSelected.add(source);
        else comparisonSelected.delete(source);
        $("compareButton").disabled = comparisonSelected.size < 2 || comparisonSelected.size > 4;
      });
      select.append(check);
    }
    row.append(
      select,
      node("td", entity.data.title || entity.item_key),
      node("td", labels[entity.kind] || entity.kind),
      node("td", String(entity.revision))
    );
    $("members").append(row);
  }
  const available = new Set((snapshot?.entities || [])
    .filter(entity => entity.data.kind === "document" && entity.data.hash)
    .map(entity => entity.data.hash + ".pdf"));
  comparisonSelected = new Set([...comparisonSelected].filter(source => available.has(source)));
  $("compareButton").disabled = comparisonSelected.size < 2 || comparisonSelected.size > 4;
}

function renderComparison(result) {
  $("results").replaceChildren();
  const panel = node("article", "", "answer-panel");
  panel.append(node("h3", result.status === "ok" ? "多文献对照完成" : "多文献对照部分完成"));
  panel.append(node("p", result.summary || "本轮未形成可靠的跨文献结论。"));
  if (result.profiles?.length) {
    panel.append(node("h4", "按论文整理的研究条件（AI 草稿，点击证据核对）"));
    for (const profile of result.profiles) {
      const title = result.source_titles?.[profile.source] || profile.source;
      const details = document.createElement("details");
      details.append(node("summary", title));
      const fields = [["研究对象", profile.population], ["方法", profile.method],
        ["数据集/设置", profile.setting], ["指标", profile.metric],
        ["结果", profile.result], ["适用条件", profile.condition], ["局限", profile.limitation]];
      for (const [label, value] of fields) if (value) details.append(node("p", `${label}：${value}`));
      panel.append(details);
    }
  }
  if (result.comparisons?.length) {
    panel.append(node("h4", "跨论文结论"));
    const relation = {agreement: "共识", difference: "差异", incomparable: "不可直接比较"};
    for (const claim of result.comparisons) {
      const row = node("div", "", "answer-claim");
      row.append(node("p", `${claim.aspect} · ${relation[claim.relation] || claim.relation}`));
      row.append(node("p", claim.statement));
      for (const ref of claim.evidence_ids || []) {
        const evidence = (result.documents || []).flatMap(doc => doc.evidence || []).find(item => item.ref === ref);
        if (!evidence) continue;
        const link = node("button", `${evidence.title || evidence.source} · 第 ${evidence.page || "?"} 页`, "source-button");
        link.type = "button";
        link.addEventListener("click", () => evidence.url && bridge.openSource(evidence.url));
        row.append(link, node("blockquote", evidence.quote, "quote"));
      }
      row.append(node("p", "复核意见：" + (claim.review?.reason || "待核实"), "muted"));
      panel.append(row);
    }
  }
  for (const warning of [...(result.warnings || []), ...(result.limitations || [])]) {
    panel.append(node("p", warning, "muted"));
  }
  panel.append(node("p", `模型调用 ${result.calls}/${result.call_budget} · ${result.telemetry?.summary?.known_tokens?.total_tokens || "未知"} token`, "muted"));
  $("results").append(panel);
  $("resultMeta").textContent = "多文献对照 · 已保留按篇证据与复核状态";
}

function renderNotes() {
  $("noteList").replaceChildren();
  const typeLabels = {understanding: "理解", question: "疑问", idea: "想法"};
  const selectedType = $("noteTypeFilter").value;
  for (const note of snapshot?.notes || []) {
    if (note.data.archived && !$('showArchivedNotes').checked) continue;
    // “全部”不限制类型；其他选项只展示类型匹配的笔记。
    if (selectedType !== "all" && note.data.type !== selectedType) continue;
    const row = node("article", "", "note-row");
    row.append(
      node("h3", `${typeLabels[note.data.type] || "记录"} · ${note.data.title || "未命名笔记"}`),
      node("p", note.data.body),
      node("div", `v${note.revision} · ${note.data.confirmed ? "已确认" : "未确认草稿"} · ${note.data.author_source === "ai" ? "AI 辅助" : "用户记录"} · ${note.data.source_status === "linked" ? `来源 ${(note.data.anchors || []).length} 条${note.data.source_verified ? " · 摘录已核对" : " · 摘录待核对"}` : "未关联来源"}`, "muted")
    );
    for (const anchor of note.data.anchors || []) {
      const source = node("button", `回到来源${anchor.page ? ` · 第 ${anchor.page} 页` : ""}`, "source-button");
      source.type = "button";
      source.title = anchor.quote || "来源摘录";
      source.addEventListener("click", () => {
        if (anchor.url?.startsWith("zotero://")) bridge.openSource(anchor.url);
        else notice(anchor.page ? `来源摘录位于第 ${anchor.page} 页；当前没有可用的 Zotero 跳转链接。` : "当前来源没有可用的跳转链接。");
      });
      row.append(source);
    }
    const edit = node("button", "编辑");
    edit.type = "button";
    edit.addEventListener("click", () => editNote(note));
    const history = node("button", "历史");
    history.type = "button";
    history.addEventListener("click", () => showNoteHistory(note, row));
    const archive = node("button", note.data.archived ? "恢复" : "归档");
    archive.type = "button";
    archive.addEventListener("click", async () => {
      try {
        await bridge.request("notes/" + note.id, {kb_id: active.id, title: note.data.title,
          body: note.data.body, confirmed: note.data.confirmed, archived: !note.data.archived,
          anchors: note.data.anchors || [], expected_revision: note.revision});
        await refresh();
        showView("notes");
        notice(note.data.archived ? "笔记已恢复。" : "笔记已归档，不再默认参与检索。");
      } catch (error) { notice(error.message); }
    });
    row.append(edit, history, archive);
    $("noteList").append(row);
  }
  if (!$("noteList").children.length) {
    $("noteList").append(node("p", "暂无专题笔记。", "empty"));
  }
}

async function showNoteHistory(note, row) {
  try {
    const history = await bridge.request("history/" + note.id, undefined, "GET");
    const details = document.createElement("details");
    details.open = true;
    details.append(node("summary", `历史版本（${history.length}）`));
    for (const item of history) details.append(node("p", `v${item.revision} · ${item.updated_at || ""}\n${item.body}`, "quote"));
    row.append(details);
  } catch (error) { notice(error.message); }
}

async function refresh() {
  searchEpoch++;
  const status = await updateEngineStatus();
  if (!status.connected) {
    notice(status.error || "Python 科研引擎尚未连接。");
    return;
  }
  const data = await bridge.request("bases", undefined, "GET");
  bases = data.bases;
  selected = new Set([...selected].filter(id => bases.some(base => base.id === id && !base.archived)));
  if (!selected.size) {
    const recent = bases.find(base => base.id === bridge.pref("lastBase") && !base.archived)
      || bases.find(base => !base.archived);
    if (recent) selected.add(recent.id);
  }
  const next = bases.find(base => base.id === active?.id)
    || bases.find(base => selected.has(base.id))
    || bases[0]
    || null;
  renderBases();
  // Refresh list data without resetting the form or its expected_revision.
  // A concurrent edit must conflict at save time, not silently overwrite it.
  if (next) await selectBase(next, {preserveEditor: next.id === active?.id});
  notice("");
}

function editNote(note = null, anchors = []) {
  if (!mayReplaceEditor()) return;
  if (!active || active.archived) {
    notice("请先选择一个未归档的知识库。");
    return;
  }
  editing = note;
  pendingAnchors = structuredClone(note?.data.anchors || anchors || []);
  $("noteEditorTitle").textContent = note ? "编辑专题笔记" : "新建专题笔记";
  $("noteType").value = note?.data.type || "understanding";
  $("noteTitle").value = note?.data.title || "";
  $("noteBody").value = note?.data.body || "";
  $("noteConfirmed").checked = note ? note.data.confirmed : true;
  $("noteSources").textContent = pendingAnchors.length
    ? `来源锚点：${pendingAnchors.map(a => `${a.page ? `第 ${a.page} 页` : "记录"} · ${a.quote.slice(0, 70)}`).join("；")}`
    : "尚未关联论文原文；保存后只能作为未关联的个人记录。";
  $("noteForm").hidden = false;
  editorBaseline = editorValue();
  if (pendingAnchors.length) notice(`本条笔记将保留 ${pendingAnchors.length} 条来源锚点。`);
  $("noteTitle").focus();
}

async function openNoteCitation(cite) {
  const generation = ++citationEpoch;
  const panel = $("noteReference");
  panel.hidden = false;
  panel.replaceChildren(node("p", "正在读取引用版本…"));
  showView("notes");
  try {
    if (!cite.entity_id || cite.revision == null) throw new Error("旧引用缺少笔记 ID 或版本，无法精确定位；请保留回答中的摘录核对。");
    const history = await bridge.request("history/" + encodeURIComponent(cite.entity_id), undefined, "GET");
    if (generation !== citationEpoch) return;
    const item = history.find(value => Number(value.revision) === Number(cite.revision));
    if (!item || (cite.kb_id && item.kb_id !== cite.kb_id)) throw new Error("未找到引用的笔记版本，未用最新版本替代。");
    const latest = history.reduce((a, b) => Number(a.revision) > Number(b.revision) ? a : b);
    panel.replaceChildren(node("h3", `${item.title || "专题笔记"} · 引用版本 v${item.revision}`),
      node("p", `笔记 ${item.id} · 只读历史 · ${item.author_source === "ai" ? "AI 辅助记录" : "用户记录"}`, "muted"),
      node("p", item.body, "quote"));
    if (Number(latest.revision) !== Number(item.revision) || latest.archived) {
      panel.append(node("p", `当前版本为 v${latest.revision}${latest.archived ? "（已归档）" : ""}；这里保留回答当时使用的记录，不代表最新观点。`, "muted"));
    }
    for (const anchor of item.anchors || []) {
      panel.append(node("blockquote", anchor.quote || "", "quote"));
      const link = node("button", `核对原文${anchor.page ? ` · 第 ${anchor.page} 页` : ""}`, "source-button");
      link.type = "button";
      link.addEventListener("click", () => anchor.url?.startsWith("zotero://")
        ? bridge.openSource(anchor.url) : notice("此来源没有可用的 Zotero 链接，请按摘录和页码核对。"));
      panel.append(link);
    }
    panel.scrollIntoView?.({block: "nearest"});
  } catch (error) {
    if (generation === citationEpoch) panel.replaceChildren(node("p", error.message, "muted"));
  }
}

async function runSearch(event) {
  event.preventDefault();
  if (!selected.size) {
    notice("请勾选至少一个知识库。");
    return;
  }
  const generation = ++searchEpoch;
  const asking = event.submitter?.id === "askButton";
  const started = Date.now();
  let ticker;
  let jobId = null;
  const cancelButton = $("cancelAskButton");
  $("askButton").disabled = true;
  setBusy($("searchButton"), true, "检索中");
  $("results").replaceChildren(node("p", asking ? "正在检索并核对原文依据…" : "正在检索…", "empty"));
  if (asking) ticker = window.setInterval(() => {
    if (generation === searchEpoch) $("resultMeta").textContent =
      `检索与支持性核对 · ${Math.floor((Date.now() - started) / 1000)} 秒 · 最多补检索一次`;
  }, 1000);
  showView("results");
  try {
    const searchPayload = {
      kb_ids: [...selected],
      query: $("query").value,
      scope: document.querySelector("[name=scope]:checked").value,
      ...(asking ? {conversation_id: conversationId} : {}),
    };
    let result = await bridge.request(asking ? "ask-async" : "search", searchPayload);
    // Index construction has its own status; it is not a failed/empty search.
    while (!asking && result.status === "indexing") {
      if (generation !== searchEpoch) return;
      $("resultMeta").textContent = `准备语义索引 · ${Math.floor((Date.now() - started) / 1000)} 秒`;
      $("results").replaceChildren(node("p", result.warning, "empty"));
      if (Date.now() - started > 180000) {
        notice("索引仍在后台准备，已完成向量会保留；稍后点击检索即可继续。");
        return;
      }
      await new Promise(resolve => window.setTimeout(resolve, 2000));
      result = await bridge.request("search", searchPayload);
    }
    if (asking) {
      jobId = result.job_id;
      window.__activeAskJobId = jobId;
      cancelButton.hidden = false;
      cancelButton.disabled = false;
      while (result.state === "queued" || result.state === "running") {
        if (generation !== searchEpoch) return;
        $("resultMeta").textContent = `问答任务${result.state === "queued" ? "排队中" : "执行中"} · ${Math.floor((Date.now() - started) / 1000)} 秒`;
        await new Promise(resolve => window.setTimeout(resolve, 1000));
        result = await bridge.request("jobs/" + encodeURIComponent(result.job_id), undefined, "GET");
      }
      if (result.state !== "succeeded") throw new Error(result.error || "问答任务未完成，可稍后重新提交。");
      result = result.result;
    }
    if (generation !== searchEpoch) return;
    if (ticker) { window.clearInterval(ticker); ticker = null; }
    $("results").replaceChildren();
    if (asking) {
      renderAnswer(result, jobId);
      result.results = result.evidence;
      result.warning = result.warnings.join("；");
    }
    if (!asking) {
    $("resultMeta").textContent =
      `${result.results.length} 条结果 · ${result.backend === "tfidf" ? "关键词检索" : "语义检索"}`;
    }
    for (const hit of result.results) {
      const row = node("article", "", "result");
      if (hit.evidence_id) row.id = hit.evidence_id;
      const heading = document.createElement("div");
      heading.append(
        node("span", labels[hit.kind] || hit.kind, "badge" + (hit.kind === "original" ? "" : " note")),
        node("span", hit.title || "未命名资料", "result-title")
      );
      row.append(heading, node("p", hit.quote, "quote"));
      if (hit.modality === "image") row.append(node("p", "图像命中：下方文字为图注或定位信息，请打开原文核对图中细节。", "muted"));
      const source = node("button", hit.page ? `回到 Zotero 第 ${hit.page} 页` : "查看来源", "source-button");
      source.type = "button";
      source.addEventListener("click", () => {
        try {
          if (hit.kind === "user_note") openNoteCitation(hit);
          else if (hit.url?.startsWith("zotero://")) bridge.openSource(hit.url);
          else if (hit.kb_id && hit.entity_id) {
            const base = bases.find(value => value.id === hit.kb_id);
            if (base) selectBase(base).then(() => showView("notes"));
          }
        } catch (error) {
          notice(error.message);
        }
      });
      row.append(source, node("span", ` · v${hit.revision}`, "muted"));
      if (hit.quote && hit.kind === "original" && (hit.source_id || hit.source)) {
        const remember = node("button", "写入专题笔记", "source-button");
        remember.type = "button";
        remember.addEventListener("click", () => {
          const anchor = {source: hit.source_id || hit.source, page: hit.page || null,
            quote: hit.quote, url: hit.url || null, kind: hit.kind || "original"};
          editNote(null, [anchor]);
          showView("notes");
        });
        row.append(remember);
      }
      $("results").append(row);
    }
    if (!result.results.length) {
      $("results").append(node("p", "当前范围内没有匹配结果。", "empty"));
    }
    notice(result.warning || "");
  } catch (error) {
    if (generation === searchEpoch) {
      notice(error.message);
      $("resultMeta").textContent = "本次请求未完成";
      $("results").replaceChildren(node("p", "未获得答案，可检查连接后手动重试。", "empty"));
    }
  } finally {
    if (ticker) window.clearInterval(ticker);
    cancelButton.hidden = true;
    cancelButton.disabled = false;
    if (window.__activeAskJobId === jobId) window.__activeAskJobId = null;
    $("askButton").disabled = false;
    setBusy($("searchButton"), false);
  }
}

$("cancelAskButton").addEventListener("click", async () => {
  const job = window.__activeAskJobId;
  if (!job) return;
  $("cancelAskButton").disabled = true;
  try {
    await bridge.request("jobs/" + encodeURIComponent(job), {action: "cancel"}, "POST");
    searchEpoch++;
    notice("已停止问答；已经发送的模型请求可能仍计费。历史中会保留 cancelled 状态。 ");
  } catch (error) {
    $("cancelAskButton").disabled = false;
    notice(error.message);
  }
});

async function renderQAHistory() {
  try {
    const payload = await bridge.request("jobs", undefined, "GET");
    const jobs = Array.isArray(payload.jobs) ? payload.jobs : [];
    const labels = {
      queued: "排队中",
      running: "执行中",
      succeeded: "已完成",
      failed: "失败",
      interrupted: "服务重启中断",
      cancelled: "用户已停止",
    };
    const panel = node("article", "", "answer-panel");
    panel.append(node("h3", "问答历史"));
    panel.append(node("p", "历史记录保存在本地；失败或中断任务不会自动重试。", "muted"));
    if (!jobs.length) {
      panel.append(node("p", "暂无问答历史。", "empty"));
    }
    for (const job of jobs) {
      const row = node("div", "", "history-row");
      row.append(
        node("p", job.question || "未记录问题"),
        node("p", `${labels[job.state] || job.state} · ${job.updated_at || job.created_at || ""}`, "muted")
      );
      if (job.error) row.append(node("p", job.error, "muted"));
      if (job.state === "succeeded") {
        const open = node("button", "打开结果", "source-button");
        open.type = "button";
        open.addEventListener("click", async () => {
          open.disabled = true;
          try {
            const detail = await bridge.request("jobs/" + encodeURIComponent(job.job_id), undefined, "GET");
            if (!detail.result) throw new Error("该任务没有可恢复的回答结果。 ");
            $("results").replaceChildren();
            renderAnswer(detail.result, detail.job_id);
            showView("results");
            notice("已恢复历史回答。 ");
          } catch (error) {
            notice(error.message);
          } finally {
            open.disabled = false;
          }
        });
        row.append(open);
      } else if (job.state === "failed" || job.state === "interrupted") {
        const retry = node("button", "手动重试", "source-button");
        retry.type = "button";
        retry.addEventListener("click", async () => {
          if (!window.confirm("只会重新提交一次明确的问题，可能再次产生模型费用；是否继续？")) return;
          retry.disabled = true;
          try {
            await bridge.request("jobs/" + encodeURIComponent(job.job_id), {action: "retry"}, "POST");
            notice("已创建新的问答任务；不会自动重复提交。 ");
            await renderQAHistory();
          } catch (error) {
            retry.disabled = false;
            notice(error.message);
          }
        });
        row.append(retry);
      }
      panel.append(row);
    }
    $("results").replaceChildren(panel);
    $("resultMeta").textContent = `${jobs.length} 条持久化问答任务`;
    showView("results");
  } catch (error) {
    notice(error.message);
  }
}

function renderAnswer(result, jobId = null) {
  const statuses = { answered: "已生成有据回答", partial: "仅能回答部分问题", no_evidence: "现有证据不足",
    model_unavailable: "请配置回答模型", unavailable: "服务暂不可用", timeout: "已超时停止", indexing: "语义索引准备中" };
  const panel = node("article", "", "answer-panel");
  panel.append(node("h3", statuses[result.status] || result.status));
  if (jobId) {
    const feedback = node("div", "", "qa-feedback");
    feedback.append(node("p", "可选反馈：只用于后续评测候选，不会修改笔记或自动生成标准答案。", "muted"));
    for (const [rating, label] of [["helpful", "有帮助"], ["partial", "部分有帮助"],
                                   ["incorrect", "不可靠"], ["correct_no_answer", "正确拒答"]]) {
      const button = node("button", label, "source-button");
      button.type = "button";
      button.dataset.rating = rating;
      button.addEventListener("click", async () => {
        [...feedback.querySelectorAll("button")].forEach(item => { item.disabled = true; });
        try {
          await bridge.request("jobs/" + encodeURIComponent(jobId), {action: "feedback", rating}, "POST");
          feedback.dataset.saved = "true";
          feedback.replaceChildren(node("p", "反馈已保存为评测候选，不会改变个人笔记。", "muted"));
        } catch (error) {
          [...feedback.querySelectorAll("button")].forEach(item => { item.disabled = false; });
          notice(error.message);
        }
      });
      feedback.append(button);
    }
    panel.append(feedback);
  }
  const sections = result.sections || [{scope: result.scope || "original", label: "回答", result}];
  for (const section of sections) {
    const value = section.result || section;
    const title = node("h4", section.label || (section.scope === "notes" ? "你的个人记录" : "论文原文依据"), "answer-section-title");
    panel.append(title);
    for (const claim of value.claims || []) {
      const row = node("div", "", "answer-claim");
      row.append(node("p", claim.text));
      for (const cite of claim.citations || []) {
        const isNote = section.scope === "notes" || cite.kind === "user_note";
        const citeLabel = isNote
          ? `个人记录 · v${cite.revision || "?"}${cite.source_verified ? " · 摘录已核对" : " · 摘录待核对"}`
          : `${cite.title || "论文原文"} · ${cite.page ? `第 ${cite.page} 页` : "记录"}`;
        const link = node("button", citeLabel, "source-button");
        link.type = "button";
        link.addEventListener("click", () => {
          if (cite.kind === "user_note") openNoteCitation(cite);
          else if (cite.url?.startsWith("zotero://")) bridge.openSource(cite.url);
          else if (cite.kb_id) {
            const base = bases.find(value => value.id === cite.kb_id);
            if (base) selectBase(base).then(() => showView("notes"));
          }
        });
        row.append(link, node("blockquote", cite.support_quote, "quote"));
        if (isNote) {
          for (const anchor of cite.anchors || []) {
            const source = node("button", `回到原文${anchor.page ? ` · 第 ${anchor.page} 页` : ""}`, "source-button");
            source.type = "button";
            source.title = anchor.quote || "个人记录关联的原文摘录";
            source.addEventListener("click", () => {
              if (anchor.url?.startsWith("zotero://")) bridge.openSource(anchor.url);
              else notice(anchor.page ? `原文来源位于第 ${anchor.page} 页；当前没有可用的 Zotero 跳转链接。` : "这条记录没有可用的原文跳转链接。");
            });
            row.append(source);
          }
        }
      }
      // 论文回答可以沉淀为个人记忆，但只能先保存为未确认 AI 草稿。
      // 用户必须在“专题笔记”侧栏中核对并确认，避免模型结论直接进入默认检索。
      if (section.scope === "original" && claim.text && value.scope !== "notes") {
        const saveDraft = node("button", "保存为 AI 草稿", "source-button");
        saveDraft.type = "button";
        saveDraft.addEventListener("click", async () => {
          saveDraft.disabled = true;
          try {
            const anchors = (claim.citations || []).map(cite => ({
              source: cite.source_id,
              page: cite.page,
              quote: cite.support_quote || cite.quote,
              url: cite.url,
              kind: "original",
            })).filter(anchor => typeof anchor.source === "string" && anchor.source.endsWith(".pdf") && anchor.quote);
            if (!anchors.length || anchors.length !== (claim.citations || []).length) {
              throw new Error("原文来源不完整，未保存草稿；请重新检索并核对引用。");
            }
            await bridge.request("notes", {
              kb_id: active?.id,
              type: "understanding",
              title: "问答整理（待核对）",
              body: claim.text,
              confirmed: false,
              author_source: "ai",
              anchors,
            });
            await refresh();
            showView("notes");
            saveDraft.dataset.saved = "true";
            saveDraft.textContent = "已保存为 AI 草稿";
            notice("已保存为 AI 草稿；请核对原文后再确认。 ");
          } catch (error) {
            saveDraft.dataset.error = error.message || "unknown";
            notice(error.message);
          } finally {
            saveDraft.disabled = saveDraft.dataset.saved === "true";
          }
        });
        row.append(saveDraft);
      }
      panel.append(row);
    }
    for (const gap of value.gaps || []) panel.append(node("p", "证据缺口：" + gap, "muted"));
    if (value.review_label) panel.append(node("p", value.review_label, "muted"));
  }
  for (const gap of result.gaps || []) panel.append(node("p", "证据缺口：" + gap, "muted"));
  if (!result.sections) panel.append(node("p", result.review_label, "muted"));
  const details = node("details");
  details.append(node("summary", "检索过程与用量"));
  for (const step of result.trace.steps) {
    const scopeLabel = step.scope === "notes" ? "个人记录" : step.scope === "original" ? "论文原文" : "回答";
    details.append(node("p", step.stage === "retrieve"
      ? `${scopeLabel} · 第 ${step.round} 轮${step.reused ? "复用证据" : "检索"}：${step.query} · ${step.candidates} 候选 / ${step.selected} 入选`
      : `${scopeLabel} · 第 ${step.round} 轮核对：${step.draft_claims} 草稿 / ${step.accepted} 支持`));
  }
  const callStatus = {succeeded: "成功", timeout: "超时", error: "异常", invalid_output: "输出未通过校验", cancelled: "已取消"};
  for (const call of result.trace.calls || []) {
    const usage = call.usage === "known" ? "用量已知" : "用量未知";
    details.append(node("p", `模型调用 ${call.role} · ${callStatus[call.status] || call.status} · ${call.elapsed_ms || 0} ms · ${usage}`));
  }
  details.append(node("p", `输入 ${result.trace.input_tokens} / 输出 ${result.trace.output_tokens} token` +
    (result.trace.unknown_usage_calls ? `；${result.trace.unknown_usage_calls} 次调用用量未知（未按零计费）` : "")));
  if (result.trace.conversation_context_used) {
    details.append(node("p", `追问上下文：使用同一知识库最近 ${result.trace.conversation_turns || 0} 轮导航信息；未作为论文证据。`));
  }
  details.append(node("p", `回答模型：${result.trace.model || "未配置"} · 复核模型：${result.trace.review_model || result.trace.model || "未配置"}`));
  panel.append(details);
  $("results").append(panel);
  $("resultMeta").textContent = `${result.trace.model_calls} 次模型调用 · 补检索 ${result.trace.rewrite_rounds} 次 · 纠正 ${result.trace.correction_rounds || 0} 轮` +
    (result.trace.conversation_context_used ? ` · 追问上下文 ${result.trace.conversation_turns} 轮` : "") +
    ` · ${(result.trace.elapsed_ms / 1000).toFixed(1)} 秒`;
}

$("searchButton").dataset.label = "检索";
$("searchForm").addEventListener("submit", runSearch);
$("qaHistory").addEventListener("click", () => renderQAHistory());
$("newConversation").addEventListener("click", () => {
  conversationId = newConversationId();
  $("resultMeta").textContent = "已开始新会话；后续追问不会读取上一会话上下文。";
  notice("已清除当前追问上下文。论文和个人笔记不会被删除。 ");
});
$("compareButton").addEventListener("click", async () => {
  const sources = [...comparisonSelected];
  if (sources.length < 2 || sources.length > 4) {
    notice("请在“资料与批注”中选择 2 到 4 篇论文。打开该页后勾选第一列。 ");
    return;
  }
  if (!$('query').value.trim()) {
    notice("请先输入要比较的研究问题。 ");
    $('query').focus();
    return;
  }
  if (!window.confirm("允许将所选论文的检索片段发送给已配置模型进行多文献对照吗？")) return;
  const button = $("compareButton");
  button.disabled = true;
  $("results").replaceChildren(node("p", "正在按篇取证、并行分析并复核…", "empty"));
  showView("results");
  try {
    const result = await bridge.request("compare", {
      allow_external: true, kb_ids: active ? [active.id] : [], sources,
      question: $("query").value, k: 5,
    });
    renderComparison(result);
    notice("");
  } catch (error) {
    notice(error.message);
    $("results").replaceChildren(node("p", "本次对照未完成，可检查连接后重试。", "empty"));
  } finally {
    button.disabled = false;
  }
});
$("refreshBases").addEventListener("click", () => refresh().catch(error => notice(error.message)));
$("showArchived").addEventListener("change", renderBases);
$("showArchivedNotes").addEventListener("change", renderNotes);
$("noteTypeFilter").addEventListener("change", renderNotes);
$("newNote").addEventListener("click", () => editNote());
$("cancelNote").addEventListener("click", () => {
  if (!mayReplaceEditor()) return;
  editing = null;
  editorBaseline = "";
  pendingAnchors = [];
  $("noteForm").hidden = true;
});
$("noteForm").addEventListener("submit", async event => {
  event.preventDefault();
  if (savingNote) return;
  savingNote = true;
  for (const id of ["noteType", "noteTitle", "noteBody", "noteConfirmed"]) $(id).disabled = true;
  $("saveNote").disabled = true;
  try {
    await bridge.request("notes" + (editing ? "/" + editing.id : ""), {
      kb_id: active.id,
      type: $("noteType").value,
      title: $("noteTitle").value,
      body: $("noteBody").value,
      confirmed: $("noteConfirmed").checked,
      anchors: pendingAnchors,
      expected_revision: editing?.revision,
    });
    $("noteForm").hidden = true;
    editing = null;
    editorBaseline = "";
    pendingAnchors = [];
    await refresh();
    showView("notes");
    notice("笔记已保存。");
  } catch (error) {
    notice(error.message);
  } finally {
    savingNote = false;
    for (const id of ["noteType", "noteTitle", "noteBody", "noteConfirmed"]) $(id).disabled = false;
    $("saveNote").disabled = false;
  }
});
$("archiveBase").addEventListener("click", async () => {
  if (!active || !window.confirm(`${active.archived ? "恢复" : "归档"}“${active.name}”？`)) return;
  try {
    await bridge.request("archive/" + active.id, {
      archived: !active.archived,
      expected_revision: active.revision,
    });
    await refresh();
    notice("知识库状态已更新。");
  } catch (error) {
    notice(error.message);
  }
});
let parsingTimer = null;
async function refreshParsing() {
  if (parsingTimer !== null) { window.clearTimeout(parsingTimer); parsingTimer = null; }
  try {
    const data = await bridge.request('parsing', undefined, 'GET');
    const labels = {queued: '排队', running: '解析中', succeeded: '完成', failed: '失败', interrupted: '已中断'};
    const panel = $('parsingStatus');
    panel.replaceChildren();
    for (const job of data.jobs.slice(0, 20)) {
      panel.append(node('p', `${job.sha.slice(0, 10)} · ${labels[job.status] || job.status}${job.error ? ' · ' + job.error : ''}`));
    }
    if (!data.jobs.length) panel.append(node('p', '暂无精析任务。'));
    if (data.jobs.some(job => ['queued', 'running'].includes(job.status))) {
      parsingTimer = window.setTimeout(refreshParsing, 3000);
    }
  } catch (error) { $('parsingStatus').textContent = error.message; }
}
window.addEventListener('unload', () => { if (parsingTimer !== null) window.clearTimeout(parsingTimer); });
$('refreshParsing').addEventListener('click', refreshParsing);
$('parseSelected').addEventListener('click', async () => {
  if (!selected.size) { notice('请勾选要精析的知识库。'); return; }
  $('parseSelected').disabled = true;
  try {
    await bridge.request('parse', {kb_ids: [...selected]});
    notice('已提交精析。失败或中断后再次点击可重试；完成产物会复用。远端模式会发送 PDF。');
    await refreshParsing();
  } catch (error) { notice(error.message); }
  finally { $('parseSelected').disabled = false; }
});
$("syncCollection").addEventListener("click", async () => {
  $("syncCollection").disabled = true;
  try {
    await bridge.sync();
    await refresh();
    await refreshParsing();
  } catch (error) {
    notice(error.message);
  } finally {
    $("syncCollection").disabled = false;
  }
});
$("writeBack").addEventListener("click", async () => {
  $("writeBack").disabled = true;
  try {
    await bridge.writeNotes();
    notice("已完成 Zotero 笔记写回检查。");
  } catch (error) {
    notice(error.message);
  } finally {
    $("writeBack").disabled = false;
  }
});
$("exportBackup").addEventListener("click", async () => {
  $("exportBackup").disabled = true;
  try {
    if (await bridge.exportBackup(window)) notice("备份已保存。");
  } catch (error) {
    notice(error.message);
  } finally {
    $("exportBackup").disabled = false;
  }
});
$("configureConnection").addEventListener("click", async () => {
  try {
    await bridge.configure();
    await refresh();
  } catch (error) {
    notice(error.message);
  }
});
$("configureServices").addEventListener(
  "click",
  () => openServiceConfig()
);

$("cancelServiceConfig").addEventListener(
  "click",
  () => {
    if (serviceConfigBusy) return;
    $("serviceLlmKey").value = "";
    $("serviceEmbeddingKey").value = "";
    $("serviceConfigPanel").hidden = true;
  }
);

$("serviceConfigForm").addEventListener(
  "submit",
  async event => {
    event.preventDefault();
    if (serviceConfigBusy || $("serviceConfigFields").disabled) return;
    serviceConfigBusy = true;
    $("serviceConfigFields").disabled = true;
    try {
      const mineruMode =
        $("serviceMineruMode").value;

      validateServiceUrl(
        $("serviceLlmBaseUrl").value,
        "问答模型 API 地址"
      );

      validateServiceUrl(
        $("serviceEmbeddingBaseUrl").value,
        "Qwen API 地址"
      );

      if (mineruMode === "remote") {
        validateServiceUrl(
          $("serviceMineruUrl").value,
          "MinerU 地址"
        );
      }

      const timeout = Number(
        $("serviceMineruTimeout").value
      );

      if (
        !Number.isInteger(timeout) ||
        timeout < 60 ||
        timeout > 7200
      ) {
        throw new Error(
          "MinerU 超时必须是 60–7200 秒的整数。"
        );
      }

      const payload = {
        llm: {
          base_url: validateServiceUrl($('serviceLlmBaseUrl').value, '问答模型 API 地址'),
          model: $('serviceLlmModel').value.trim(),
          api_key: $('serviceLlmKey').value.trim(),
        },
        embedding: {
          base_url: validateServiceUrl($('serviceEmbeddingBaseUrl').value, 'Qwen API 地址'),
          model: $('serviceEmbeddingModel').value.trim(),
          api_key: $('serviceEmbeddingKey').value.trim(),
          dimension: Number($('serviceEmbeddingDimension').value),
          revision: $('serviceEmbeddingRevision').value.trim(),
        },
        mineru: {
          mode: mineruMode,
          url: mineruMode === 'remote'
            ? validateServiceUrl($('serviceMineruUrl').value, 'MinerU 地址') : '',
          timeout,
          python: $('serviceMineruPython').value.trim(),
          auto_parse: $('serviceMineruAuto').checked,
        },
      };
      if (!payload.llm.model || !payload.embedding.model) {
        throw new Error('请填写问答模型和嵌入模型名称。');
      }
      await bridge.request('services', payload);
      notice('已保存，后续问答、检索和精析使用新配置。已运行任务保留原配置；首次编码所选资料可能产生费用。');
      $('serviceLlmKey').value = '';
      $('serviceEmbeddingKey').value = '';
      $('serviceConfigPanel').hidden = true;
    } catch (error) {
      notice(error.message);
    } finally {
      serviceConfigBusy = false;
      $("serviceConfigFields").disabled = false;
    }
  }
);
$("configureEngine").addEventListener("click", async () => {
  try {
    if (await bridge.configureEngine()) notice("Python 引擎设置已保存。");
  } catch (error) {
    notice(error.message);
  }
});
$("startEngine").addEventListener("click", async () => {
  $("startEngine").disabled = true;
  notice("正在准备本机数据库、Qwen 连接和科研助手；首次启动 Docker 可能需要数分钟…");
  try {
    await bridge.startAllServices();
    await refresh();
    notice("本地科研助手已连接；MinerU 在精析时启动。Qwen、MinerU 的实际模型可用性需在使用时确认。");
  } catch (error) {
    notice(error.message);
    await updateEngineStatus();
  } finally {
    $("startEngine").disabled = false;
  }
});
for (const button of document.querySelectorAll("[data-view]")) {
  button.addEventListener("click", () => showView(button.dataset.view));
}
for (const input of document.querySelectorAll("[name=scope]")) {
  input.addEventListener("change", () => {
    searchEpoch++;
    $("results").replaceChildren(node("p", "检索范围已更新", "empty"));
    updateScopeCopy();
  });
}
updateScopeCopy();

window.ResearchAssistantWorkbench = {
  ready: null,
  startupError: "",
  setQuery(query = "") {
    if (!query) return;
    $("query").value = query;
    $("query").focus();
  },
};

$("query").value = args.query || "";
window.ResearchAssistantWorkbench.ready = refresh().catch(error => {
  window.ResearchAssistantWorkbench.startupError = error.message;
  notice(error.message);
});
