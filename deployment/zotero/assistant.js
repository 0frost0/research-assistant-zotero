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
let searchEpoch = 0;

function node(tag, text = "", className = "") {
  const element = document.createElement(tag);
  element.textContent = text;
  if (className) element.className = className;
  return element;
}

function notice(message = "") {
  $("notice").textContent = message;
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

async function selectBase(base) {
  active = base;
  snapshot = null;
  editing = null;
  $("noteForm").hidden = true;
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
  snapshot = await bridge.request("bases/" + base.id, undefined, "GET");
  renderMembers();
  renderNotes();
}

function renderMembers() {
  $("members").replaceChildren();
  for (const entity of snapshot?.entities || []) {
    const row = document.createElement("tr");
    row.append(
      node("td", entity.data.title || entity.item_key),
      node("td", labels[entity.kind] || entity.kind),
      node("td", String(entity.revision))
    );
    $("members").append(row);
  }
}

function renderNotes() {
  $("noteList").replaceChildren();
  for (const note of snapshot?.notes || []) {
    if (note.data.archived) continue;
    const row = node("article", "", "note-row");
    row.append(
      node("h3", note.data.title || "未命名笔记"),
      node("p", note.data.body),
      node("div", `v${note.revision} · ${note.data.confirmed ? "已确认" : "未确认草稿"}`, "muted")
    );
    const edit = node("button", "编辑");
    edit.type = "button";
    edit.addEventListener("click", () => editNote(note));
    row.append(edit);
    $("noteList").append(row);
  }
  if (!$("noteList").children.length) {
    $("noteList").append(node("p", "暂无专题笔记。", "empty"));
  }
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
  active = bases.find(base => base.id === active?.id)
    || bases.find(base => selected.has(base.id))
    || bases[0]
    || null;
  renderBases();
  if (active) await selectBase(active);
  notice("");
}

function editNote(note = null) {
  if (!active || active.archived) {
    notice("请先选择一个未归档的知识库。");
    return;
  }
  editing = note;
  $("noteEditorTitle").textContent = note ? "编辑专题笔记" : "新建专题笔记";
  $("noteTitle").value = note?.data.title || "";
  $("noteBody").value = note?.data.body || "";
  $("noteConfirmed").checked = note ? note.data.confirmed : true;
  $("noteForm").hidden = false;
  $("noteTitle").focus();
}

async function runSearch(event) {
  event.preventDefault();
  if (!selected.size) {
    notice("请勾选至少一个知识库。");
    return;
  }
  const generation = ++searchEpoch;
  setBusy($("searchButton"), true, "检索中");
  $("results").replaceChildren(node("p", "正在检索…", "empty"));
  showView("results");
  try {
    const result = await bridge.request("search", {
      kb_ids: [...selected],
      query: $("query").value,
      scope: document.querySelector("[name=scope]:checked").value,
    });
    if (generation !== searchEpoch) return;
    $("results").replaceChildren();
    $("resultMeta").textContent =
      `${result.results.length} 条结果 · ${result.backend === "tfidf" ? "关键词检索" : "语义检索"}`;
    for (const hit of result.results) {
      const row = node("article", "", "result");
      const heading = document.createElement("div");
      heading.append(
        node("span", labels[hit.kind] || hit.kind, "badge" + (hit.kind === "original" ? "" : " note")),
        node("span", hit.title || "未命名资料", "result-title")
      );
      row.append(heading, node("p", hit.quote, "quote"));
      const source = node("button", hit.page ? `回到 Zotero 第 ${hit.page} 页` : "查看来源", "source-button");
      source.type = "button";
      source.addEventListener("click", () => {
        try {
          if (hit.url?.startsWith("zotero://")) bridge.openSource(hit.url);
          else if (hit.kb_id && hit.entity_id) {
            const base = bases.find(value => value.id === hit.kb_id);
            if (base) selectBase(base).then(() => showView("notes"));
          }
        } catch (error) {
          notice(error.message);
        }
      });
      row.append(source, node("span", ` · v${hit.revision}`, "muted"));
      $("results").append(row);
    }
    if (!result.results.length) {
      $("results").append(node("p", "当前范围内没有匹配结果。", "empty"));
    }
    notice(result.warning || "");
  } catch (error) {
    notice(error.message);
  } finally {
    setBusy($("searchButton"), false);
  }
}

$("searchButton").dataset.label = "检索";
$("searchForm").addEventListener("submit", runSearch);
$("refreshBases").addEventListener("click", () => refresh().catch(error => notice(error.message)));
$("showArchived").addEventListener("change", renderBases);
$("newNote").addEventListener("click", () => editNote());
$("cancelNote").addEventListener("click", () => {
  editing = null;
  $("noteForm").hidden = true;
});
$("noteForm").addEventListener("submit", async event => {
  event.preventDefault();
  $("saveNote").disabled = true;
  try {
    await bridge.request("notes" + (editing ? "/" + editing.id : ""), {
      kb_id: active.id,
      title: $("noteTitle").value,
      body: $("noteBody").value,
      confirmed: $("noteConfirmed").checked,
      expected_revision: editing?.revision,
    });
    $("noteForm").hidden = true;
    editing = null;
    await refresh();
    showView("notes");
    notice("笔记已保存。");
  } catch (error) {
    notice(error.message);
  } finally {
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
$("syncCollection").addEventListener("click", async () => {
  $("syncCollection").disabled = true;
  try {
    await bridge.sync();
    await refresh();
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
$("configureEngine").addEventListener("click", async () => {
  try {
    if (await bridge.configureEngine()) notice("Python 引擎设置已保存。");
  } catch (error) {
    notice(error.message);
  }
});
$("startEngine").addEventListener("click", async () => {
  $("startEngine").disabled = true;
  notice("正在启动 Python 科研引擎…");
  try {
    await bridge.ensureBackend(true);
    await refresh();
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
  });
}

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
