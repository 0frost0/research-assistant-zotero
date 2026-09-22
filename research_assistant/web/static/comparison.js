/* Comparison UI shares library selection, but never submits to the planning endpoint. */
const comparisonHistory = document.getElementById("comparisonHistory");
const cancelComparison = document.getElementById("cancelComparison");
const comparisonPhases = {
  queued: "等待执行", retrieving: "按篇取证", analyzing: "并行单篇分析",
  synthesizing: "综合对照", reviewing: "单轮 AI 复核", completed: "已完成",
  failed: "未完成", cancelled: "已停止", interrupted: "服务重启中断", stale: "资料版本已变化"
};
const comparisonTerminal = new Set(["completed", "failed", "cancelled", "interrupted", "stale"]);
let comparisonRunId = null;
let comparisonPollToken = 0;
let comparisonStarted = false;
let comparisonRequest = null;

function cmpElement(tag, text, className) {
  const node = document.createElement(tag);
  if (text !== undefined) node.textContent = text;
  if (className) node.className = className;
  return node;
}

function comparisonModeChanged() {
  const comparing = currentMode === "compare";
  document.getElementById("comparisonControls").classList.toggle("hidden", !comparing);
  document.getElementById("topicControl").classList.toggle("hidden", comparing);
  document.getElementById("hoursControl").classList.toggle("hidden", comparing);
  previewButton.classList.toggle("hidden", comparing);
  document.getElementById("resultCountLabel").textContent = comparing ? "每篇证据数量" : "证据数量";
  resultCountInput.max = comparing ? "8" : "20";
  if (comparing) {
    resultCountInput.value = Math.min(8, Number(resultCountInput.value));
    questionLabel.textContent = "对照问题（选择 2–4 篇文献）";
    questionInput.placeholder = "这些文献的方法、研究对象和结论有哪些共识与差异？哪些结果不可直接比较？";
    if (!comparisonStarted) {
      comparisonStarted = true;
      refreshComparisonHistory(true).catch(error => showToast(error.message));
    } else if (comparisonRunId) {
      watchComparison(comparisonRunId);
    }
  } else {
    comparisonPollToken += 1;
    runButton.disabled = false;
    runStatus.textContent = "";
    cancelComparison.classList.add("hidden");
  }
}

async function refreshComparisonHistory(restore = false) {
  const data = await api("/api/comparisons");
  const selected = comparisonHistory.value;
  comparisonHistory.replaceChildren(cmpElement("option", "选择历史结果"));
  comparisonHistory.firstChild.value = "";
  data.runs.forEach(run => {
    const option = cmpElement("option", comparisonPhases[run.phase] + " · " + run.question.slice(0, 65));
    option.value = run.id;
    comparisonHistory.append(option);
  });
  comparisonHistory.value = selected;
  if (restore && data.runs.length && currentMode === "compare") {
    await watchComparison(data.runs[0].id);
  }
}

function cmpReferences(refs) {
  const span = cmpElement("span", undefined, "comparison-refs");
  refs.forEach(ref => {
    const link = cmpElement("a", "[" + ref + "]");
    link.href = "#cmp-" + ref;
    span.append(link);
  });
  return span;
}

function renderComparisonTrace(trace) {
  const details = cmpElement("details", undefined, "comparison-details comparison-trace");
  details.id = "comparisonTrace";
  details.append(cmpElement("summary", "执行记录"));
  if (!trace) {
    details.append(cmpElement("p", "本条历史没有调用轨迹记录。", "comparison-meta"));
    return details;
  }
  const stats = trace.summary;
  const seconds = ms => ms == null ? "未知" : (ms / 1000).toFixed(2) + " s";
  const totals = stats.known_tokens;
  details.append(cmpElement("p", `整轮 ${seconds(stats.wall_ms)} · 峰值模型并发 ${stats.peak_model_concurrency} · 调用成功 ${stats.completed_calls}/${stats.model_calls}`, "comparison-meta"));
  const usage = totals ? `已报告 ${totals.total_tokens} token（输入 ${totals.input_tokens}，输出 ${totals.output_tokens}）` : "token 用量未知";
  details.append(cmpElement("p", `${usage} · 服务用量覆盖 ${stats.provider_usage_calls}/${stats.model_calls} 次调用`, "comparison-meta"));
  if (!stats.usage_complete) details.append(cmpElement("p", "部分调用未报告用量，不能据此计算完整费用。", "comparison-limit"));
  if (stats.timing_incomplete) details.append(cmpElement("p", "运行曾被中断，总耗时记录不完整。", "comparison-limit"));
  const list = cmpElement("ol", undefined, "comparison-spans");
  const roles = {analyst: "单篇分析", synthesizer: "综合", reviewer: "复核", ...comparisonPhases};
  const statuses = {running: "进行中", ok: "成功", error: "失败", timeout: "超时", cancelled: "已取消", interrupted: "中断"};
  const errors = {connection: "连接失败", authentication: "认证失败", rate_limit: "限流", invalid_output: "输出不符合结构", timeout: "请求超时", execution_error: "执行错误"};
  trace.spans.forEach(span => {
    const row = cmpElement("li", undefined, span.kind === "model" ? "comparison-model-span" : "comparison-phase-span");
    const title = `${roles[span.name] || span.name}${span.paper_slot ? " · 文献 " + span.paper_slot : ""}`;
    row.append(cmpElement("strong", title), cmpElement("span", span.kind === "phase" && span.status === "ok" ? "已结束" : (statuses[span.status] || span.status)));
    const timing = `开始 +${seconds(span.start_ms)} · 耗时 ${seconds(span.duration_ms)}`;
    row.append(cmpElement("p", timing + (span.kind === "model" ? ` · 排队 ${seconds(span.queue_ms)} · 请求 ${seconds(span.request_duration_ms)}` : ""), "comparison-meta"));
    if (span.kind === "model") {
      const tokens = span.usage ? `${span.usage.total_tokens} token${span.usage_source === "simulated" ? "（模拟）" : ""}` : "用量未知";
      row.append(cmpElement("p", tokens + (span.error_category ? " · " + (errors[span.error_category] || "执行错误") : ""), "comparison-meta"));
    }
    list.append(row);
  });
  details.append(list, cmpElement("p", "Trace " + trace.trace_id, "comparison-meta"));
  return details;
}

function renderComparison(run) {
  const traceOpen = document.getElementById("comparisonTrace")?.open;
  resultPanel.replaceChildren();
  searchPanel.classList.add("hidden");
  const result = run.result;
  resultPanel.append(cmpElement("h2", "多文献对照", "comparison-title"));
  resultPanel.append(cmpElement("p", run.question, "comparison-question"));
  resultPanel.append(cmpElement("p", `${comparisonPhases[run.phase]} · 模型调用 ${run.calls}/${run.call_budget} · ${run.sources.length} 篇文献`, "comparison-meta"));
  if (run.error) resultPanel.append(cmpElement("p", run.error, "degraded-warning"));
  const trace = renderComparisonTrace(run.telemetry);
  trace.open = Boolean(traceOpen);
  resultPanel.append(trace);
  if (result) {
    resultPanel.append(cmpElement("p", result.summary));
    [...result.warnings, ...result.limitations].forEach(w => resultPanel.append(cmpElement("p", w, "comparison-limit")));
    if (result.comparisons.length) {
      const section = appendResultSection("对照结果 · AI 复核支持", "comparison-table-wrap");
      const table = document.createElement("table");
      table.className = "comparison-table";
      const head = document.createElement("thead");
      const header = document.createElement("tr");
      ["维度", "关系", "比较结论与证据"].forEach(t => header.append(cmpElement("th", t)));
      head.append(header);
      const body = document.createElement("tbody");
      const relations = {agreement: "共识", difference: "差异", incomparable: "不可直接比较"};
      result.comparisons.forEach(claim => {
        const row = document.createElement("tr");
        const statement = cmpElement("td");
        statement.append(cmpElement("p", claim.statement), cmpReferences(claim.evidence_ids));
        statement.append(cmpElement("p", "复核意见：" + claim.review.reason, "comparison-meta"));
        row.append(cmpElement("td", claim.aspect), cmpElement("td", relations[claim.relation]), statement);
        body.append(row);
      });
      table.append(head, body);
      section.append(table);
    }
    if (result.withheld.length) {
      const details = document.createElement("details");
      details.className = "comparison-details";
      details.append(cmpElement("summary", "未采纳 / 待核实的比较（" + result.withheld.length + "）"));
      result.withheld.forEach(c => {
        details.append(cmpElement("p", c.statement), cmpReferences(c.evidence_ids), cmpElement("p", "未采纳原因：" + c.review.reason, "comparison-limit"));
      });
      resultPanel.append(details);
    }
  }
  const analyses = result ? result.analyses : run.analyses;
  if (analyses.length) {
    const section = appendResultSection("单篇分析 · AI 草稿", "comparison-papers");
    run.sources.forEach(source => {
      const analysis = analyses.find(a => a.source === source);
      if (!analysis) return;
      const details = document.createElement("details");
      details.className = "comparison-details";
      details.append(cmpElement("summary", source + " · " + ({ok: "已分析", failed: "分析失败", no_evidence: "证据不足"}[analysis.status])));
      analysis.findings.forEach(f => {
        details.append(cmpElement("h4", f.aspect), cmpElement("p", f.statement), cmpReferences(f.evidence_ids));
      });
      analysis.gaps.forEach(g => details.append(cmpElement("p", "证据缺口：" + g, "comparison-limit")));
      section.append(details);
    });
  }
  const documents = result ? result.documents : run.documents;
  if (documents.length) {
    const section = appendResultSection("本轮证据快照", "comparison-evidence");
    documents.forEach(doc => {
      section.append(cmpElement("h4", doc.source));
      if (!doc.evidence.length) section.append(cmpElement("p", doc.error || "未命中证据", "comparison-limit"));
      doc.evidence.forEach(e => {
        const details = document.createElement("details");
        details.id = "cmp-" + e.ref;
        details.className = "comparison-details";
        details.append(cmpElement("summary", `[${e.ref}] ${e.source}${e.page ? " · 第 " + e.page + " 页" : ""} · ${e.backend}`));
        details.append(cmpElement("p", e.quote || "仅定位图片，未将图片发送给分析模型。"));
        e.quality_warnings.forEach(w => details.append(cmpElement("p", w, "comparison-limit")));
        if (e.truncated) details.append(cmpElement("p", "受本轮上下文预算限制，证据已截短。", "comparison-meta"));
        section.append(details);
      });
    });
  }
}

async function watchComparison(id) {
  const token = ++comparisonPollToken;
  comparisonRunId = id;
  comparisonHistory.value = id;
  let last = "";
  try {
    while (token === comparisonPollToken && currentMode === "compare") {
      const run = await api("/api/comparisons/" + id);
      if (token !== comparisonPollToken || currentMode !== "compare") return;
      const done = comparisonTerminal.has(run.phase);
      runButton.disabled = !done;
      cancelComparison.classList.toggle("hidden", done);
      runStatus.textContent = comparisonPhases[run.phase] + " · " + run.analyses.length + "/" + run.sources.length + " 篇已处理";
      const signature = JSON.stringify([run.phase, run.calls, run.analyses.length, run.telemetry]);
      if (signature !== last) renderComparison(run);
      last = signature;
      if (done && (!run.telemetry || run.telemetry.status !== "running")) {
        await refreshComparisonHistory();
        comparisonHistory.value = id;
        return;
      }
      await wait(1000);
    }
  } catch (error) {
    showToast(error.message + " 可刷新对照记录恢复查看，任务不会因轮询失败自动重跑。");
  } finally {
    if (token === comparisonPollToken) runButton.disabled = false;
  }
}

async function startComparison(payload) {
  if (payload.sources.length < 2 || payload.sources.length > 4) {
    showToast("请选择 2 到 4 篇不同文献");
    return;
  }
  const fingerprint = JSON.stringify(payload);
  if (!comparisonRequest || comparisonRequest.fingerprint !== fingerprint) {
    comparisonRequest = {fingerprint, id: crypto.randomUUID()};
  }
  runButton.disabled = true;
  permissionError.classList.add("hidden");
  try {
    const run = await api("/api/comparisons", {method: "POST", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({...payload, request_id: comparisonRequest.id})});
    comparisonRequest = null;
    await refreshComparisonHistory();
    await watchComparison(run.id);
  } catch (error) {
    if (error.code === "external_permission_required") {
      permissionError.textContent = error.message;
      permissionError.classList.remove("hidden");
    } else showToast(error.message);
    runButton.disabled = false;
  }
}

cancelComparison.addEventListener("click", async function () {
  if (!comparisonRunId) return;
  cancelComparison.disabled = true;
  try {
    await api("/api/comparisons/" + comparisonRunId, {method: "POST", headers: {"Content-Type": "application/json"}, body: JSON.stringify({action: "cancel"})});
    await watchComparison(comparisonRunId);
  } catch (error) { showToast(error.message); }
  finally { cancelComparison.disabled = false; }
});
comparisonHistory.addEventListener("change", function () {
  if (this.value) watchComparison(this.value);
});
document.getElementById("reloadComparison").addEventListener("click", async function () {
  try { await refreshComparisonHistory(true); } catch (error) { showToast(error.message); }
});
resultPanel.addEventListener("click", function (event) {
  const link = event.target.closest('a[href^="#cmp-"]');
  if (link) {
    const target = document.getElementById(link.getAttribute("href").slice(1));
    if (target) target.open = true;
  }
});
