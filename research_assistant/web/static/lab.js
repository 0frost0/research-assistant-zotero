"use strict";
const $ = id => document.getElementById(id);
const labels = {all: "全部", text: "文字", image: "图片", mixed: "混合"};
const reviewStatus = {consistent: "证据一致", needs_revision: "建议修订标准证据", needs_review: "需重点复核", negative_control: "无答案对照", self_match_only: "仅自匹配检查"};
let run = null, caseId = null, currentTable = null, reviewer = "", pollTimer = null;
let tableOffset = 0;
const params = new URLSearchParams(location.search);
function el(tag, text, cls) { const n = document.createElement(tag); if (text !== undefined) n.textContent = text; if (cls) n.className = cls; return n; }
function notice(message = "") { $("notice").textContent = message; }
async function api(path, data) {
  const response = await fetch(path, data === undefined ? {} : {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify(data)});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || "请求失败");
  return result;
}
function action(fn) { return async event => { try { notice(); await fn(event); } catch (error) { notice(error.message); } }; }
function pdfLink(source, page) {
  const isPdf = source.toLowerCase().endsWith(".pdf");
  const link = el("a", source + (isPdf && page ? " · PDF 第 " + page + " 页" : ""));
  link.href = "/api/document?source=" + encodeURIComponent(source) + (isPdf && page ? "#page=" + page : "");
  link.target = "_blank"; link.rel = "noopener"; return link;
}
function preview(id) {
  const image = el("img"); image.src = "/api/evidence-image/" + encodeURIComponent(id); image.alt = "图像证据";
  image.loading = "lazy"; image.addEventListener("error", () => image.replaceWith(el("p", "图像已失效或不可用，请核对原文", "muted"))); return image;
}
function grid(columns, rows) {
  const table = el("table"), head = el("tr"), body = el("tbody");
  columns.forEach(name => head.append(el("th", name))); const thead = el("thead"); thead.append(head); table.append(thead);
  rows.forEach(row => { const tr = el("tr"); row.forEach(v => tr.append(el("td", v == null ? "未标注" : String(v)))); body.append(tr); });
  table.append(body); return table;
}
function fmt(v) { return v == null ? "未统计" : (v * 100).toFixed(1) + "%"; }
function renderMetrics() {
  if (!run) return;
  const mode = $("rankingMode").value, scope = $("metricScope").value;
  const metrics = run.summary[scope];
  $("metrics").replaceChildren(grid(["期望证据", "问题数", "Recall@1", "Recall@3", "Recall@5", "Recall@10", "MRR@10", "页码召回@10", "上下文召回@10", "人工均分", "人工已评/返回", "AI 均分", "AI 相关率 ≥2", "AI 已评/返回"],
    Object.entries(metrics).map(([group, m]) => {
      const h = (scope === "human_approved_development" ? run.human_approved_relevance : run.human_relevance)[mode][group];
      const a = scope === "draft_development" ? run.ai_relevance?.[mode]?.[group] : null;
      return [labels[group], m.count, ...[1, 3, 5, 10].map(k => fmt(m[mode]?.["recall@" + k])),
        m[mode]?.["mrr@10"]?.toFixed(3) ?? "未统计", fmt(m[mode]?.["page_recall@10"]),
        fmt(m[mode]?.["context_recall@10"]),
        scope === "self_match_probe" ? "不计入" : h.mean_grade?.toFixed(2) ?? "未标注",
        scope === "self_match_probe" ? "不计入" : `${h.judged}/${h.returned}`,
        a?.mean_grade?.toFixed(2) ?? "未统计", fmt(a?.relevant_fraction_among_judged), a ? `${a.judged}/${a.returned}` : "不计入"];
    })));
  const h = run.human_relevance[mode].all, negative = run.summary.negative_controls;
  $("qualityState").textContent = `尚未确认达到科研检索要求 · 标准答案人工确认 ${run.approvals.length}/${run.cases.length} · 人工相关率(≥2，仅已评证据) ${fmt(h.relevant_fraction_among_judged)} · 标注覆盖 ${fmt(h.coverage)} · 无答案对照仍返回证据 ${negative.returned_evidence_cases}/${negative.count}`;
  const ai = run.ai_review, state = $("aiReviewState"); state.replaceChildren();
  if (ai) {
    const coverage = run.ai_relevance.coverage, flags = ai.case_reviews.filter(c => c.needs_human_review).length;
    state.append(el("h2", "AI 辅助标注"), el("p", `${ai.reviewer} · ${coverage.all.judged}/${coverage.all.pairs} 对证据 · ${ai.case_reviews.length} 道题 · 重点复核 ${flags} 题`, "ai-note"));
    state.append(el("p", `自匹配检查 ${coverage.self_match_probe.judged}/${coverage.self_match_probe.pairs} 对 · 无答案对照 ${coverage.negative_controls.judged}/${coverage.negative_controls.pairs} 对 · 均不计入开发正例相关率`, "muted"));
    const details = el("details"); details.append(el("summary", "标注依据与局限"), el("p", ai.method));
    ai.limitations.forEach(item => details.append(el("p", item)));
    ai.source_checks.forEach(item => details.append(el("p", item)));
    state.append(details);
  } else state.append(el("p", "自动评测结果 · 未进行新的 AI 或人工审阅", "muted"));
  const comparison = $("policyComparison"); comparison.replaceChildren();
  if (run.policy_comparison) {
    const c = run.policy_comparison, baseline = c.baseline_summary.draft_development;
    comparison.append(el("h2", "固定开发集 · 同批候选对照"));
    const scroll = el("div", undefined, "scroll");
    scroll.append(grid(["期望证据", "旧策略 Recall@10", "当前命中 Recall@10", "当前上下文召回@10"],
      Object.entries(run.summary.draft_development).map(([group, current]) => [labels[group],
        fmt(baseline[group].displayed?.["recall@10"]), fmt(current.displayed?.["recall@10"]),
        fmt(current.displayed?.["context_recall@10"])])));
    comparison.append(scroll, el("p", c.regressions.length ? "上下文召回退步题：" + c.regressions.join("、") : "未发现相对旧策略的逐题上下文召回退步 · 非独立质量验收", "quality"));
  }
}
function renderCases() {
  if (!run) return;
  const cases = run.cases.filter(c => $("caseFilter").value === "all" || c.query_type === $("caseFilter").value);
  $("caseList").replaceChildren();
  cases.forEach(c => {
    const flagged = run.ai_review?.case_reviews.some(r => r.case_id === c.id && r.needs_human_review);
    const b = el("button", `${c.id} · ${labels[c.query_type]}${c.split === "self_match_probe" ? " · 自匹配" : ""}${flagged ? " · 待复核" : ""}\n${c.question || "图片输入"}`);
    b.classList.toggle("selected", c.id === caseId); b.addEventListener("click", () => { caseId = c.id; renderCases(); renderCase(); }); $("caseList").append(b);
  });
}
function renderCase() {
  if (!run) return;
  const c = run.cases.find(x => x.id === caseId); if (!c) return;
  const detail = $("caseDetail"); detail.replaceChildren(el("h2", `${c.id} · ${c.question || "图片输入"}`));
  detail.append(el("p", `期望证据：${labels[c.query_type]} · 输入：${labels[c.input_modality]} · ${c.split === "self_match_probe" ? "自匹配检查，不计入质量结论" : "开发集草案"}`, "muted"));
  const aiCase = run.ai_review?.case_reviews.find(r => r.case_id === c.id);
  if (aiCase) detail.append(el("h3", "AI 复核意见 · " + reviewStatus[aiCase.status]), el("p", aiCase.note, "ai-note"));
  if (c.query_image_id) detail.append(preview(c.query_image_id));
  const bar = el("div", undefined, "toolbar"), nameLabel = el("label", "审阅人"), name = el("input");
  name.className = "reviewer"; name.value = reviewer; name.maxLength = 80; name.setAttribute("aria-label", "审阅人");
  name.addEventListener("input", () => reviewer = name.value); nameLabel.append(name); bar.append(nameLabel);
  const approved = run.approvals.some(a => a.case_id === c.id);
  const approve = el("button", approved ? "撤销标准答案确认" : "确认标准答案");
  approve.addEventListener("click", action(async () => {
    if (!reviewer.trim()) throw new Error("请填写审阅人");
    if (!confirm(approved ? "撤销这道题的标准答案确认？" : "已核对原文，并确认期望证据充分且正确？")) return;
    run = await api("/api/evaluations/" + run.run_id, {action: approved ? "revoke_gold" : "approve_gold", case_id: c.id, reviewer}); renderAll();
  })); bar.append(approve); detail.append(bar, el("h3", "期望证据"));
  if (!c.expected.length) detail.append(el("p", "资料库不应提供该问题的直接证据"));
  c.expected.forEach(g => {
    const entry = el("div", undefined, "expected"); entry.append(pdfLink(g.source, g.page));
    entry.append(el("p", `${labels[g.modality]} · 原始块 ${g.block_index} · ${g.any_of.length} 个等价证据 ID`, "muted"));
    const anchor = el("details"), summary = el("summary", "证据锚点与 ID"); anchor.append(summary, el("p", g.anchor), el("p", g.any_of.join("\n"))); entry.append(anchor);
    if (g.modality === "image") entry.append(preview(g.any_of[0])); detail.append(entry);
  });
  detail.append(el("h3", "检索结果"));
  const hits = c.rankings[$("rankingMode").value];
  if (!hits.length) detail.append(el("p", "没有达到阈值的结果"));
  hits.forEach((hit, i) => {
    const entry = el("article", undefined, "hit"), match = c.expected.some(g => g.any_of.includes(hit.point_id));
    const contextMatch = c.expected.some(g => g.any_of.some(id => hit.context_evidence_ids?.includes(id)));
    entry.append(el("h3", `#${i + 1} · ${labels[hit.modality]} · ${hit.score.toFixed(3)}${match ? " · 命中期望证据" : contextMatch ? " · 上下文包含期望片段" : ""}`), pdfLink(hit.source, hit.page));
    if (hit.modality === "image") entry.append(preview(hit.point_id)); entry.append(el("p", hit.text));
    if (hit.quality_warnings?.length) entry.append(el("p", hit.quality_warnings.join(" "), "quality"));
    if (hit.retrieved_text && hit.retrieved_text !== hit.text) {
      const raw = el("details"); raw.append(el("summary", hit.modality === "image" ? "未核验图表转写" : "原始命中片段"), el("p", hit.retrieved_text)); entry.append(raw);
    }
    const ai = run.ai_review?.judgments.find(j => j.case_id === c.id && j.point_id === hit.point_id);
    if (ai) entry.append(el("p", `AI 评分 ${ai.grade}/3 · ${ai.note}`, "ai-note"));
    const old = run.judgments.find(j => j.case_id === c.id && j.point_id === hit.point_id);
    const controls = el("div", undefined, "toolbar"), grade = el("select"); grade.setAttribute("aria-label", "结果 " + (i + 1) + " 相关性");
    [["", "未标注"], [0, "0 无关"], [1, "1 弱相关"], [2, "2 有用"], [3, "3 直接证据"]].forEach(([v, text]) => { const o = el("option", text); o.value = v; grade.append(o); });
    grade.value = old ? old.grade : "";
    const note = el("input"); note.placeholder = "审阅备注"; note.setAttribute("aria-label", "审阅备注"); note.className = "grade-note"; note.maxLength = 1000; note.value = old?.note || "";
    const save = el("button", "保存评分"); save.addEventListener("click", action(async () => {
      if (!reviewer.trim() || grade.value === "") throw new Error("请填写审阅人并选择评分");
      run = await api("/api/evaluations/" + run.run_id, {case_id: c.id, point_id: hit.point_id, grade: Number(grade.value), reviewer, note: note.value}); renderMetrics(); save.textContent = "已保存";
    })); controls.append(grade, note, save); entry.append(controls); detail.append(entry);
  });
}
function renderAll() { renderMetrics(); renderCases(); renderCase(); }
async function loadRun(id) {
  run = await api("/api/evaluations/" + encodeURIComponent(id)); caseId = run.cases[0]?.id;
  $("runMeta").textContent = `${run.dataset_id} · ${run.created_at} · ${run.model.split("@")[0]} · ${run.dimension} 维 · ${run.settings?.retrieval_policy?.version || "legacy-v1-cap3"} · ${run.duration_seconds}s`;
  $("exportRun").disabled = false; renderAll();
}
async function refreshRuns(loadLatest = false) {
  const data = await api("/api/evaluations");
  $("runSelect").replaceChildren(...data.runs.map(r => { const o = el("option", r.created_at); o.value = r.run_id; return o; }));
  if (run) $("runSelect").value = run.run_id;
  if (loadLatest && data.runs.length) { $("runSelect").value = data.runs[0].run_id; await loadRun(data.runs[0].run_id); }
  else if (!data.runs.length) $("runMeta").textContent = `${data.dataset_id} · ${data.case_count} 个问题 · 尚未运行`;
  const job = data.job;
  $("runEval").disabled = job.status === "running";
  $("jobState").textContent = job.status === "running" ? `${job.done}/${job.total}` : job.status === "failed" ? "评测失败" : "";
  if (job.status === "failed") notice(job.error);
  if (job.status === "running") { clearTimeout(pollTimer); pollTimer = setTimeout(action(() => refreshRuns(true)), 2000); }
}
async function loadTables() {
  const source = $("tableSource").value || params.get("source") || "";
  const data = await api("/api/tables?q=" + encodeURIComponent($("tableQuery").value) + "&source=" + encodeURIComponent(source) + "&offset=" + tableOffset);
  const options = [el("option", "全部"), ...data.stats.sources.map(s => { const o = el("option", s.source); o.value = s.source; return o; })]; options[0].value = "";
  $("tableSource").replaceChildren(...options); $("tableSource").value = source; params.delete("source");
  $("tableStats").textContent = `${data.stats.tables} 张表 · ${data.stats.cells} 个单元格 · 缺失结构 ${data.stats.parse_status.missing_html || 0}`;
  $("tablePage").textContent = `第 ${Math.floor(tableOffset / 50) + 1} 页 · 本页 ${data.tables.length} 张`;
  $("tablePrev").disabled = tableOffset === 0; $("tableNext").disabled = !data.has_more;
  $("tableList").replaceChildren();
  data.tables.forEach(t => { const button = el("button", `${t.source} · p.${t.page}\n${t.caption || "无标题表格"} · ${t.row_count}×${t.column_count} · ${t.parse_status}`);
    button.dataset.tableId = t.table_id; button.addEventListener("click", action(() => showTable(t.table_id))); $("tableList").append(button); });
  const selected = data.tables.find(t => String(t.page) === params.get("page")) || data.tables[0];
  if (selected) await showTable(selected.table_id);
  else { currentTable = null; $("cellSql").disabled = true; $("tableDetail").replaceChildren(el("p", "没有匹配的表格")); }
}
async function showTable(id) {
  const t = await api("/api/tables/" + id); currentTable = t; $("cellSql").disabled = false;
  document.querySelectorAll("[data-table-id]").forEach(b => b.classList.toggle("selected", b.dataset.tableId === id));
  const detail = $("tableDetail"); detail.replaceChildren(el("h2", t.caption || "无标题表格"), pdfLink(t.source, t.page));
  detail.append(el("p", `${t.row_count} 行 × ${t.column_count} 列 · ${t.parse_status} · ${t.table_id}`, "muted"));
  if (t.parse_status !== "ok") detail.append(el("p", t.warning || "表格结构缺失或不规则；请核对原图", "quality"));
  if (t.cells.length) {
    const scroll = el("div", undefined, "scroll"), table = el("table"), rows = Array.from({length: t.row_count}, () => el("tr"));
    t.cells.forEach(c => { const cell = el(c.is_header ? "th" : "td", c.text); cell.rowSpan = c.row_span; cell.colSpan = c.col_span;
      cell.title = `row_index=${c.row_index}, column_index=${c.column_index}, numeric_value=${c.numeric_value}`; rows[c.row_index].append(cell); });
    table.append(...rows); scroll.append(table); detail.append(scroll);
  }
  if (t.footnote) detail.append(el("p", t.footnote, "muted"));
  if (t.image_path) { const d = el("details"), image = el("img"); image.src = "/api/tables/" + t.table_id + "/image"; image.alt = "原始表格截图"; image.className = "table-preview"; d.append(el("summary", "原始表格截图"), image); detail.append(d); }
}
async function switchTab(tables) { $("tablesView").hidden = !tables; $("evaluationView").hidden = tables; $("evalTab").setAttribute("aria-pressed", !tables); $("tableTab").setAttribute("aria-pressed", tables); if (tables) await loadTables(); }
$("evalTab").addEventListener("click", action(() => switchTab(false)));
$("tableTab").addEventListener("click", action(() => switchTab(true)));
$("runEval").addEventListener("click", action(async () => { await api("/api/evaluations", {}); await refreshRuns(); }));
$("runSelect").addEventListener("change", action(e => loadRun(e.target.value)));
$("caseFilter").addEventListener("change", renderCases);
$("rankingMode").addEventListener("change", renderAll);
$("metricScope").addEventListener("change", renderMetrics);
$("tableSearch").addEventListener("submit", action(async e => { e.preventDefault(); tableOffset = 0; await loadTables(); }));
$("tablePrev").addEventListener("click", action(async () => { tableOffset = Math.max(0, tableOffset - 50); await loadTables(); }));
$("tableNext").addEventListener("click", action(async () => { tableOffset += 50; await loadTables(); }));
$("sqlForm").addEventListener("submit", action(async e => { e.preventDefault(); const result = await api("/api/tables/sql", {sql: $("sql").value}); $("sqlResults").replaceChildren(grid(result.columns, result.rows)); $("sqlState").textContent = `${result.rows.length} 行${result.truncated ? " · 已截断" : ""}`; }));
$("cellSql").addEventListener("click", () => { if (currentTable) $("sql").value = `SELECT row_index, column_index, row_span, col_span, text, numeric_value\nFROM table_cells\nWHERE table_id = '${currentTable.table_id}'\nORDER BY row_index, column_index`; });
$("exportRun").addEventListener("click", () => { const blob = new Blob([JSON.stringify(run, null, 2)], {type: "application/json"}), url = URL.createObjectURL(blob), a = el("a"); a.href = url; a.download = `retrieval-${run.run_id}.json`; a.click(); setTimeout(() => URL.revokeObjectURL(url), 1000); });
action(async () => { await refreshRuns(true); if (params.get("tab") === "tables") await switchTab(true); })();
