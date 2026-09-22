"use strict";
const $ = id => document.getElementById(id);
const statusLabels = {todo: "待开始", doing: "进行中", done: "已完成", blocked: "受阻"};
const phaseLabels = {queued: "已排队", drafting: "检索与拟定计划", checking: "规则检查", awaiting_approval: "等待你的确认", applied: "已应用到任务清单", rejected: "已拒绝或放弃", blocked: "规则检查未通过", failed: "执行失败", stale: "草案已过期", interrupted: "执行已中断，可恢复"};
const priorities = {high: "高", medium: "中", low: "低"};
let projects = [], data = null, files = [], selectedProposal = null, pollTimer = null, memoryRevision = null, memoryDirty = false;
const edits = new Map();
function el(tag, text, cls) { const n = document.createElement(tag); if (text !== undefined) n.textContent = text; if (cls) n.className = cls; return n; }
function notice(text = "") { $("notice").textContent = text; }
async function api(path, body) {
  const r = await fetch(path, body === undefined ? {} : {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(body)});
  const value = await r.json(); if (!r.ok) throw new Error(value.error || "请求失败"); return value;
}
function action(fn) { return async event => {
  event?.preventDefault(); notice();
  const control = event?.currentTarget?.tagName === "BUTTON" ? event.currentTarget : event?.submitter;
  if (control) control.disabled = true;
  try { await fn(event); } catch (error) { notice(error.message); } finally {
    if (control?.isConnected) {
      control.disabled = false;
      if (control.id === "generatePlan" && data) control.disabled = data.project.archived || data.proposals.some(r => !["applied", "rejected", "blocked", "stale"].includes(r.phase));
    }
  }
}; }
function fields() { return {name: $("projectName").value, goal: $("goalInput").value, weekly_hours: Number($("projectHours").value), notes: $("memoryInput").value, archived: $("archiveProject").checked, sources: [...$("projectSources").querySelectorAll("input:checked")].map(n => n.value)}; }
function checkbox(parent, value, text, checked = false) { const label = el("label", undefined, "check"), input = el("input"); input.type = "checkbox"; input.value = value; input.checked = checked; label.append(input, el("span", text)); parent.append(label); }
function switchTab(tab) {
  if (tab === "memory" && data && !memoryDirty) renderMemory();
  for (const id of ["tasks", "planning", "memory", "history"]) $(id + "View").hidden = id !== tab;
  document.querySelectorAll("[data-tab]").forEach(b => b.setAttribute("aria-pressed", b.dataset.tab === tab));
}
async function listProjects() { projects = (await api("/api/projects")).projects; renderProjectList(); }
function renderProjectList() {
  const list = $("projectList"); list.replaceChildren();
  projects.filter(p => $("showArchived").checked || !p.archived).forEach(p => {
    const b = el("button", p.name + (p.archived ? " · 已归档" : "")); b.classList.toggle("selected", data?.project.id === p.id);
    b.addEventListener("click", action(() => loadProject(p.id))); list.append(b);
  });
  if (!list.children.length) list.append(el("p", "暂无项目", "muted"));
}
function renderHeader() {
  const p = data.project, hours = p.tasks.filter(t => t.status !== "done").reduce((v, t) => v + t.estimated_hours, 0);
  $("projectTitle").textContent = p.name + (p.archived ? " · 已归档" : "");
  $("projectGoal").textContent = p.goal;
  $("projectStats").textContent = `${p.tasks.filter(t => t.status === "done").length}/${p.tasks.length} 项完成 · 未完成 ${Number(hours.toFixed(2))}/${p.weekly_hours} 小时 · v${p.revision}`;
  $("addTask").disabled = p.archived;
}
function taskArticle(t, taskNames) {
  const article = el("article", undefined, "project-task"); article.append(el("h3", t.title));
  article.append(el("div", `${t.estimated_hours} 小时 · ${priorities[t.priority]}优先级${t.status ? " · " + statusLabels[t.status] : ""}`, "project-task-meta"));
  if (t.purpose) article.append(el("p", t.purpose));
  if (t.dependencies.length) article.append(el("p", "依赖：" + t.dependencies.map(id => taskNames[id] || id).join("、"), "muted"));
  if (t.evidence.length) { const details = el("details"); details.append(el("summary", "计划依据"), el("p", t.evidence.join("\n"))); article.append(details); }
  return article;
}
function renderTasks() {
  const p = data.project, names = Object.fromEntries(p.tasks.map(t => [t.task_id, t.title]));
  $("taskList").replaceChildren();
  p.tasks.filter(t => $("taskFilter").value === "all" || t.status === $("taskFilter").value).forEach(t => {
    const article = taskArticle(t, names), controls = el("div", undefined, "task-editor"), select = el("select"), note = el("textarea"), save = el("button", "保存进展");
    Object.entries(statusLabels).forEach(([v, label]) => { const o = el("option", label); o.value = v; select.append(o); });
    const pending = edits.get(t.task_id); select.value = pending?.status ?? t.status; note.value = pending?.progress_note ?? t.progress_note; note.rows = 2; note.maxLength = 2000;
    select.setAttribute("aria-label", t.title + " 状态"); note.setAttribute("aria-label", t.title + " 进展备注"); note.placeholder = "进展、结果或阻塞原因";
    const remember = () => edits.set(t.task_id, {status: select.value, progress_note: note.value, revision: edits.get(t.task_id)?.revision ?? data.project.revision}); select.addEventListener("change", remember); note.addEventListener("input", remember);
    select.disabled = note.disabled = save.disabled = p.archived;
    save.addEventListener("click", action(async () => {
      data = await api(`/api/projects/${p.id}/tasks/${t.task_id}`, {revision: edits.get(t.task_id)?.revision ?? data.project.revision, status: select.value, progress_note: note.value});
      edits.delete(t.task_id); renderHeader(); renderTasks(); renderPlanning(); renderEvents(); await listProjects();
    })); controls.append(select, note, save); article.append(controls); $("taskList").append(article);
  });
  if (!$("taskList").children.length) $("taskList").append(el("p", "暂无匹配任务", "muted"));
}
function renderMemory() {
  const p = data.project;
  memoryRevision = p.revision;
  memoryDirty = false;
  $("projectName").value = p.name; $("projectHours").value = p.weekly_hours; $("goalInput").value = p.goal; $("memoryInput").value = p.notes; $("archiveProject").checked = p.archived;
  $("projectSources").replaceChildren();
  const available = new Set(files.map(f => f.name));
  [...new Set([...available, ...p.sources])].forEach(name => checkbox($("projectSources"), name, name + (available.has(name) ? "" : " · 资料已缺失"), p.sources.includes(name)));
  if (!$("projectSources").children.length) $("projectSources").append(el("p", "暂无资料", "muted"));
}
function renderEvents() {
  const names = {project_created: "创建项目", project_updated: "更新项目记忆", task_added: "添加任务", task_updated: "更新进展", planning_started: "启动规划", plan_applied: "批准并应用计划", rejected: "拒绝或放弃草案", stale: "草案版本已过期", blocked: "规则检查未通过"};
  $("eventList").replaceChildren(...data.events.map(e => { const li = el("li"); li.append(el("time", new Date(e.at).toLocaleString()), el("span", `${names[e.kind] || e.kind} · ${e.detail}`)); return li; }));
}
function renderPlanning() {
  const p = data.project, runs = data.proposals;
  if (!runs.some(r => r.id === selectedProposal)) selectedProposal = runs[0]?.id;
  $("proposalSelect").replaceChildren(...runs.map(r => { const o = el("option", `${new Date(r.created_at).toLocaleString()} · ${phaseLabels[r.display_phase]}`); o.value = r.id; return o; }));
  $("proposalSelect").value = selectedProposal || "";
  const pending = runs.find(r => !["applied", "rejected", "blocked", "stale"].includes(r.phase));
  $("generatePlan").disabled = p.archived || Boolean(pending);
  $("planningState").textContent = pending ? phaseLabels[pending.display_phase] : "";
  const detail = $("proposalDetail"); detail.replaceChildren();
  const r = runs.find(r => r.id === selectedProposal);
  if (!r) { detail.append(el("p", "暂无规划记录", "muted")); return; }
  detail.append(el("p", phaseLabels[r.display_phase], "workflow-state" + (["blocked", "failed", "stale", "interrupted"].includes(r.display_phase) ? " warning" : "")));
  detail.append(el("p", `项目版本 v${r.base_revision} · 工作流 ${r.id.slice(0, 8)}`, "muted"));
  if (r.error) detail.append(el("p", r.error, "quality"));
  r.issues.forEach(issue => detail.append(el("p", issue, "quality")));
  if (r.draft) {
    detail.append(el("p", r.draft.summary, "draft-summary"));
    const names = Object.fromEntries([...p.tasks, ...r.draft.tasks].map(t => [t.task_id, t.title]));
    r.draft.tasks.forEach(t => detail.append(taskArticle(t, names)));
  }
  const controls = el("div", undefined, "toolbar");
  function command(text, kind, primary = false) {
    const b = el("button", text, primary ? "primary-command" : ""); b.disabled = r.active;
    b.addEventListener("click", action(async () => {
      if (kind === "approve" && !confirm(`确认新增 ${r.draft.tasks.length} 项任务？现有任务不会被替换。`)) return;
      if (kind === "retry" && !$("planConsent").checked) throw new Error("重试前请确认允许外发项目数据。");
      await api("/api/project-proposals/" + r.id, {action: kind, allow_external: $("planConsent").checked}); await refreshProject();
    })); controls.append(b);
  }
  if (r.phase === "awaiting_approval") { command("批准并添加任务", "approve", true); command("拒绝草案", "reject"); }
  if (["failed", "interrupted"].includes(r.display_phase)) { command("恢复执行", "retry"); command("放弃工作流", "discard"); }
  detail.append(controls);
  if (r.evidence.length) {
    const refs = el("div", undefined, "proposal-evidence"); refs.append(el("h3", "本次检索出处"));
    r.evidence.forEach(h => { const a = el("a", h.source + (h.page ? ` · 第 ${h.page} 页` : "")); a.href = "/api/document?source=" + encodeURIComponent(h.source) + (h.page ? "#page=" + h.page : ""); a.target = "_blank"; a.rel = "noopener"; refs.append(a); h.quality_warnings.forEach(w => refs.append(el("p", w, "quality"))); });
    detail.append(refs);
  }
}
async function refreshProject() {
  if (!data) return;
  const id = data.project.id, updated = await api("/api/projects/" + id);
  if (data?.project.id !== id) return;
  data = updated; renderHeader(); renderPlanning(); renderEvents();
  clearTimeout(pollTimer);
  if (data.proposals.some(r => r.active)) pollTimer = setTimeout(action(refreshProject), 1200);
  else { renderTasks(); await listProjects(); }
}
async function loadProject(id) {
  if (data && (edits.size || memoryDirty) && !confirm("切换或刷新项目会丢弃未保存的编辑，是否继续？")) return;
  clearTimeout(pollTimer); edits.clear(); data = await api("/api/projects/" + id); selectedProposal = null;
  $("projectContent").hidden = false; $("emptyProjects").hidden = true; $("planningRequest").value = ""; $("planConsent").checked = false;
  history.replaceState(null, "", "/projects?id=" + id); renderProjectList(); renderHeader(); renderTasks(); renderMemory(); renderPlanning(); renderEvents();
  if (data.proposals.some(r => r.active)) pollTimer = setTimeout(action(refreshProject), 1200);
}
document.querySelectorAll("[data-tab]").forEach(b => b.addEventListener("click", () => switchTab(b.dataset.tab)));
document.querySelectorAll("[data-close]").forEach(b => b.addEventListener("click", () => $(b.dataset.close).close()));
$("newProject").addEventListener("click", () => $("projectDialog").showModal());
$("refreshProject").addEventListener("click", action(() => loadProject(data.project.id)));
$("memoryForm").addEventListener("input", () => { memoryDirty = true; });
$("memoryForm").addEventListener("change", () => { memoryDirty = true; });
$("showArchived").addEventListener("change", renderProjectList);
$("taskFilter").addEventListener("change", renderTasks);
$("proposalSelect").addEventListener("change", () => { selectedProposal = $("proposalSelect").value; renderPlanning(); });
$("createProjectForm").addEventListener("submit", action(async () => {
  const result = await api("/api/projects", {name: $("newName").value, goal: $("newGoal").value, weekly_hours: Number($("newHours").value)});
  $("projectDialog").close(); $("createProjectForm").reset(); await listProjects(); await loadProject(result.project.id); switchTab("memory");
}));
$("memoryForm").addEventListener("submit", action(async () => {
  data = await api("/api/projects/" + data.project.id, {revision: memoryRevision, ...fields()});
  renderHeader(); renderMemory(); renderTasks(); renderPlanning(); renderEvents(); await listProjects(); notice("项目记忆已保存。");
}));
$("addTask").addEventListener("click", () => { $("createTaskForm").reset(); $("newTaskDependencies").replaceChildren(); data.project.tasks.forEach(t => checkbox($("newTaskDependencies"), t.task_id, t.title)); $("taskDialog").showModal(); });
$("createTaskForm").addEventListener("submit", action(async () => {
  data = await api(`/api/projects/${data.project.id}/tasks`, {revision: data.project.revision, title: $("newTaskTitle").value, purpose: $("newTaskPurpose").value, estimated_hours: Number($("newTaskHours").value), priority: $("newTaskPriority").value, dependencies: [...$("newTaskDependencies").querySelectorAll("input:checked")].map(n => n.value)});
  $("taskDialog").close(); renderHeader(); renderTasks(); renderPlanning(); renderEvents(); await listProjects();
}));
$("planningForm").addEventListener("submit", action(async () => {
  if (!$("planConsent").checked) throw new Error("请先确认允许将项目数据发送给 DeepSeek。");
  const r = await api(`/api/projects/${data.project.id}/proposals`, {revision: data.project.revision, request: $("planningRequest").value, allow_external: true}); selectedProposal = r.id; await refreshProject();
}));
async function init() {
  files = (await api("/api/library")).files; await listProjects();
  const id = new URLSearchParams(location.search).get("id"); const p = projects.find(p => p.id === id) || projects.find(p => !p.archived);
  if (p) await loadProject(p.id);
}
init().catch(e => notice(e.message));
