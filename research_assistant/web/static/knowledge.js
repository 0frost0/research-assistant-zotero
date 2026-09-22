const $ = id => document.getElementById(id);
const fragment = new URLSearchParams(location.hash.slice(1));
let token = fragment.get("token") || sessionStorage.getItem("knowledge-token") || "";
if (token) sessionStorage.setItem("knowledge-token", token);
history.replaceState(null, "", location.pathname + location.search);
const params = new URLSearchParams(location.search);
let pendingNote = params.get("note");
let bases = [], selected = new Set(params.get("kb") ? [params.get("kb")] : []), active = null, snapshot = null, editing = null, epoch = 0, detailEpoch = 0;
const labels = { document: "原文", original: "原文", note: "外部笔记", annotation: "外部批注", external_note: "外部笔记", external_annotation: "外部批注", user_note: "个人笔记" };
const icons = () => window.lucide?.createIcons();
const notice = message => { $("notice").textContent = message || ""; };
const node = (tag, value, cls) => { const n = document.createElement(tag); n.textContent = value; if (cls) n.className = cls; return n; };
async function api(path, data, binary = false) {
  const response = await fetch("/api/knowledge/" + path, {
    method: data === undefined ? "GET" : "POST",
    headers: { Authorization: "Bearer " + token, "Content-Type": "application/json" },
    body: data === undefined ? undefined : JSON.stringify(data), cache: "no-store",
  });
  if (!response.ok) {
    const value = await response.json();
    throw new Error(value.error || "请求失败");
  }
  return binary ? response.blob() : response.json();
}
function showView(view) {
  for (const name of ["results", "members", "notes"]) $(name + "View").hidden = name !== view;
  for (const button of document.querySelectorAll("[data-view]")) button.setAttribute("aria-selected", String(button.dataset.view === view));
}
function renderBases() {
  $("baseList").replaceChildren();
  for (const base of bases.filter(b => $("showArchived").checked || !b.archived)) {
    const row = node("label", "", "base-row" + (active?.id === base.id ? " active" : ""));
    const check = document.createElement("input");
    check.type = "checkbox"; check.checked = selected.has(base.id); check.disabled = Boolean(base.archived);
    check.setAttribute("aria-label", "检索 " + base.name);
    check.addEventListener("change", () => {
      if (check.checked) selected.add(base.id); else selected.delete(base.id);
      epoch++; $("results").replaceChildren(node("p", "检索范围已更新", "empty"));
    });
    const button = node("span", base.name);
    button.tabIndex = 0;
    const open = event => { event.preventDefault(); selectBase(base).catch(error => notice(error.message)); };
    button.addEventListener("click", open);
    button.addEventListener("keydown", event => { if (event.key === "Enter") open(event); });
    const box = document.createElement("div");
    box.append(button, node("small", `${base.member_count} 条资料 · v${base.revision}${base.archived ? " · 已归档" : ""}`, "muted"));
    row.append(check, box); $("baseList").append(row);
  }
  if (!bases.length) $("baseList").append(node("p", "尚无知识库", "empty"));
}
async function selectBase(base) {
  const generation = ++detailEpoch;
  active = base; snapshot = null;
  $("baseTitle").textContent = base.name;
  $("baseMeta").textContent = `${base.subtree ? "含子分类" : "仅当前分类"} · 版本 ${base.revision}${base.archived ? " · 已归档" : ""}`;
  $("archive").disabled = false;
  $("members").replaceChildren(); $("noteList").replaceChildren();
  renderBases();
  if (base.archived) return;
  const value = await api("bases/" + base.id);
  if (generation !== detailEpoch) return;
  snapshot = value;
  for (const entity of value.entities) {
    const row = document.createElement("tr");
    row.append(node("td", entity.data.title || entity.item_key), node("td", labels[entity.kind]), node("td", String(entity.revision)));
    $("members").append(row);
  }
  renderNotes();
}
function renderNotes() {
  $("noteList").replaceChildren();
  for (const note of snapshot?.notes || []) {
    if (note.data.archived) continue;
    const row = node("article", "", "note-row");
    row.id = "note-" + note.id;
    row.append(node("h3", note.data.title || "未命名笔记"), node("p", note.data.body),
      node("small", `v${note.revision} · ${note.data.confirmed ? "已确认" : "未确认草稿"}`, "muted"));
    const edit = node("button", "编辑");
    edit.addEventListener("click", () => editNote(note)); row.append(edit);
    $("noteList").append(row);
  }
  if (!$("noteList").children.length) $("noteList").append(node("p", "暂无专题笔记", "empty"));
  if (pendingNote && snapshot?.notes.some(note => note.id === pendingNote)) {
    showView("notes");
    document.getElementById("note-" + pendingNote)?.scrollIntoView({ block: "center" });
    pendingNote = null;
  }
}
async function refresh() {
  epoch++;
  $("results").replaceChildren(node("p", "暂无检索结果", "empty"));
  $("resultMeta").textContent = "";
  const data = await api("bases");
  bases = data.bases;
  selected = new Set([...selected].filter(id => bases.some(b => b.id === id && !b.archived)));
  if (!active && !selected.size && bases.some(b => !b.archived)) selected.add(bases.find(b => !b.archived).id);
  active = bases.find(b => b.id === active?.id) || bases.find(b => selected.has(b.id)) || bases[0] || null;
  $("connectionState").textContent = "后端已连接";
  renderBases();
  if (active) await selectBase(active);
}
$("searchForm").addEventListener("submit", async event => {
  event.preventDefault();
  if (!selected.size) return notice("请勾选至少一个知识库。");
  const generation = ++epoch;
  $("results").replaceChildren(node("p", "正在检索…", "empty"));
  $("searchButton").disabled = true; notice("正在检索…"); showView("results");
  try {
    const result = await api("search", { kb_ids: [...selected], query: $("query").value, scope: document.querySelector("[name=scope]:checked").value });
    if (generation !== epoch) return;
    $("results").replaceChildren();
    $("resultMeta").textContent = `${result.results.length} 条结果 · ${result.backend === "tfidf" ? "关键词检索" : "语义检索"}`;
    for (const hit of result.results) {
      const row = node("article", "", "result");
      const top = node("div", "", "result-top");
      top.append(node("span", labels[hit.kind], "badge" + (hit.kind === "original" ? "" : " note")),
                 node("span", hit.title || "未命名资料", "result-title"));
      row.append(top, node("p", hit.quote, "quote"));
      const link = node("a", hit.page ? `回到来源 · 第 ${hit.page} 页` : "查看来源");
      link.href = hit.url;
      row.append(link, node("span", ` · v${hit.revision}`, "muted")); $("results").append(row);
    }
    if (!result.results.length) $("results").append(node("p", "当前范围内没有匹配结果", "empty"));
    notice(result.warning);
  } catch (error) { notice(error.message); }
  finally { $("searchButton").disabled = false; }
});
function editNote(note = null) {
  if (!active || active.archived) return notice("请选择未归档的知识库。");
  editing = { note, kbId: active.id };
  $("noteTitle").value = note?.data.title || ""; $("noteBody").value = note?.data.body || "";
  $("noteConfirmed").checked = note ? note.data.confirmed : true;
  $("noteDialog").showModal();
}
$("noteForm").addEventListener("submit", async event => {
  event.preventDefault(); $("saveNote").disabled = true;
  try {
    await api("notes" + (editing.note ? "/" + editing.note.id : ""), {
      kb_id: editing.kbId, title: $("noteTitle").value, body: $("noteBody").value,
      confirmed: $("noteConfirmed").checked, expected_revision: editing.note?.revision,
    });
    $("noteDialog").close(); await refresh(); showView("notes"); notice("笔记已保存。");
  } catch (error) { notice(error.message); }
  finally { $("saveNote").disabled = false; }
});
$("connectForm").addEventListener("submit", async event => {
  event.preventDefault(); token = $("token").value.trim();
  try { await api("status"); sessionStorage.setItem("knowledge-token", token); $("token").value = ""; $("connectDialog").close(); await refresh(); notice(""); }
  catch (error) { notice(error.message); }
});
$("connection").onclick = () => $("connectDialog").showModal();
$("cancelConnect").onclick = () => $("connectDialog").close();
$("cancelNote").onclick = () => $("noteDialog").close();
$("newNote").onclick = () => editNote();
$("refresh").onclick = () => refresh().catch(error => notice(error.message));
$("showArchived").onchange = renderBases;
$("archive").onclick = async () => {
  if (!active || !confirm(`${active.archived ? "恢复" : "归档"}“${active.name}”？`)) return;
  try { await api("archive/" + active.id, { archived: !active.archived, expected_revision: active.revision }); await refresh(); notice("知识库状态已更新。"); }
  catch (error) { notice(error.message); }
};
$("export").onclick = async () => {
  if (!confirm("导出全部知识库、原文和笔记的恢复包？")) return;
  $("export").disabled = true;
  try {
    const blob = await api("export", undefined, true);
    const url = URL.createObjectURL(blob), link = document.createElement("a");
    link.href = url; link.download = "research-knowledge-backup.zip"; link.click();
    setTimeout(() => URL.revokeObjectURL(url), 10000); notice("备份已导出。");
  } catch (error) { notice(error.message); }
  finally { $("export").disabled = false; }
};
for (const button of document.querySelectorAll("[data-view]")) button.onclick = () => showView(button.dataset.view);
for (const input of document.querySelectorAll("[name=scope]")) input.onchange = () => {
  epoch++; $("results").replaceChildren(node("p", "检索范围已更新", "empty"));
};
$("query").value = params.get("q") || "";
icons();
if (token) refresh().catch(error => { notice(error.message); $("connectDialog").showModal(); });
else $("connectDialog").showModal();
