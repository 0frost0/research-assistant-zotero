const fileInput = document.getElementById("fileInput");
const fileList = document.getElementById("fileList");
const fileCount = document.getElementById("fileCount");
const dropZone = document.getElementById("dropZone");
const form = document.getElementById("researchForm");
const resultPanel = document.getElementById("resultPanel");
const runButton = document.getElementById("runButton");
const runStatus = document.getElementById("runStatus");
const toast = document.getElementById("toast");
const savePlanControl = document.getElementById("savePlanControl");
const questionLabel = document.getElementById("questionLabel");
const questionInput = document.getElementById("question");
const backendInput = document.getElementById("backend");
const fileTypeInput = document.getElementById("fileType");
const minScoreInput = document.getElementById("minScore");
const resultCountInput = document.getElementById("resultCount");
const previewButton = document.getElementById("previewButton");
const searchPanel = document.getElementById("searchPanel");
const permissionError = document.getElementById("permissionError");
const selectAllSources = document.getElementById("selectAllSources");
const systemStatus = document.getElementById("systemStatus");
const statusDot = document.querySelector(".status-dot");
const pdfReportButton = document.getElementById("pdfReportButton");
const pdfReportList = document.getElementById("pdfReportList");
const pdfReportMeta = document.getElementById("pdfReportMeta");
let currentMode = "answer";
let toastTimer;
let sourceSelectionInitialized = false;
let selectedSources = new Set();
let knownSources = new Set();
let defaultThresholds = {
  tfidf: 0.10,
  bge: 0.35,
  hybrid: 0.18,
  qdrant: 0.35,
  multimodal: 0.35
};

function formatSize(bytes) {
  if (bytes < 1024) return bytes + " B";
  if (bytes < 1024 * 1024) return (bytes / 1024).toFixed(1) + " KB";
  return (bytes / (1024 * 1024)).toFixed(1) + " MB";
}

function showToast(message) {
  toast.textContent = message;
  toast.classList.add("show");
  clearTimeout(toastTimer);
  toastTimer = setTimeout(function () {
    toast.classList.remove("show");
  }, 3200);
}

async function api(url, options) {
  let response;
  try {
    response = await fetch(url, options);
  } catch (_networkError) {
    const error = new Error(
      "无法连接本地网页服务。请运行 start_web.ps1，等待启动完成后刷新页面。"
    );
    error.code = "local_service_unavailable";
    throw error;
  }
  let data;
  try {
    data = await response.json();
  } catch (_jsonError) {
    throw new Error("本地服务返回了无法识别的响应，请查看 web_app 日志。");
  }
  if (!response.ok) {
    const error = new Error(data.error || "请求失败");
    error.code = data.error_code || "request_failed";
    error.requestId = data.request_id;
    throw error;
  }
  return data;
}

function createFileRow(file) {
  const row = document.createElement("div");
  row.className = "file-row";

  const selected = document.createElement("input");
  selected.className = "source-check";
  selected.type = "checkbox";
  selected.checked = selectedSources.has(file.name);
  selected.dataset.source = file.name;
  selected.setAttribute("aria-label", "使用 " + file.name);
  selected.addEventListener("change", function () {
    if (selected.checked) selectedSources.add(file.name);
    else selectedSources.delete(file.name);
    updateSelectAllState();
  });

  const type = document.createElement("div");
  type.className = "file-type";
  type.textContent = file.type;

  const info = document.createElement("div");
  info.className = "file-info";
  const name = document.createElement("div");
  name.className = "file-name";
  name.title = file.name;
  name.textContent = file.name;
  const size = document.createElement("div");
  size.className = "file-size";
  size.textContent = formatSize(file.size) + (file.mineru_cached ? " · MinerU 已缓存" : "");
  info.append(name, size);

  const actions = document.createElement("div");
  actions.className = "file-actions";

  if (file.type === "PDF") {
    const parse = document.createElement("button");
    parse.className = "file-action-button";
    parse.type = "button";
    parse.title = file.mineru_cached ? "重新运行 MinerU 高精度解析" : "使用 MinerU 高精度解析";
    parse.textContent = file.mineru_cached ? "重析" : "精析";
    parse.addEventListener("click", function () {
      runMinerUParse(file.name, parse, file.mineru_cached);
    });
    actions.append(parse);
    const read = document.createElement("a");
    read.href = "/reader?source=" + encodeURIComponent(file.name);
    read.textContent = "阅读 / 笔记";
    actions.append(read);
  }

  const remove = document.createElement("button");
  remove.className = "icon-button";
  remove.type = "button";
  remove.title = "删除资料";
  remove.setAttribute("aria-label", "删除 " + file.name);
  remove.textContent = "×";
  remove.addEventListener("click", async function () {
    if (!window.confirm("确定从资料库删除“" + file.name + "”吗？")) return;
    try {
      await api("/api/library/" + encodeURIComponent(file.name), {
        method: "DELETE"
      });
      await loadLibrary();
      showToast("已删除 " + file.name);
    } catch (error) {
      showToast(error.message);
    }
  });

  actions.append(remove);
  row.append(selected, type, info, actions);
  return row;
}

function wait(milliseconds) {
  return new Promise(function (resolve) { setTimeout(resolve, milliseconds); });
}

async function runMinerUParse(source, button, force) {
  button.disabled = true;
  button.textContent = "排队";
  try {
    const started = await api("/api/pdf-parse", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source: source, force: force })
    });
    button.textContent = "解析中";
    let job = started;
    while (job.status === "queued" || job.status === "running") {
      await wait(2000);
      job = await api("/api/pdf-parse/" + started.job_id);
    }
    if (job.status !== "completed") {
      throw new Error(job.error || "MinerU 解析失败");
    }
    showToast("MinerU 高精度解析完成：" + source);
    await loadLibrary();
    const reports = await api("/api/pdf-reports");
    renderPdfReports(reports);
  } catch (error) {
    showToast(error.message);
  } finally {
    button.disabled = false;
    button.textContent = force ? "重析" : "精析";
  }
}

function updateSelectAllState() {
  const checkboxes = Array.from(document.querySelectorAll(".source-check"));
  const checked = checkboxes.filter(function (item) { return item.checked; }).length;
  selectAllSources.checked = checkboxes.length > 0 && checked === checkboxes.length;
  selectAllSources.indeterminate = checked > 0 && checked < checkboxes.length;
}

async function loadLibrary() {
  try {
    const data = await api("/api/library");
    fileList.replaceChildren();
    fileCount.textContent = data.files.length + " 个文件";
    const availableNames = new Set(data.files.map(function (file) { return file.name; }));
    if (!sourceSelectionInitialized) {
      selectedSources = new Set(availableNames);
      sourceSelectionInitialized = true;
    } else {
      selectedSources = new Set(
        Array.from(selectedSources).filter(function (name) { return availableNames.has(name); })
      );
      data.files.forEach(function (file) {
        if (!knownSources.has(file.name)) {
          selectedSources.add(file.name);
        }
      });
    }
    knownSources = availableNames;
    if (!data.files.length) {
      const empty = document.createElement("div");
      empty.className = "empty-library";
      empty.textContent = "资料库为空";
      fileList.append(empty);
      return;
    }
    data.files.forEach(function (file) {
      fileList.append(createFileRow(file));
    });
    updateSelectAllState();
  } catch (error) {
    showToast(error.message);
  }
}

async function uploadFile(file) {
  const body = new FormData();
  body.append("file", file);
  try {
    await api("/api/library", { method: "POST", body: body });
    await loadLibrary();
    showToast("已加入资料库：" + file.name);
  } catch (error) {
    showToast(error.message);
  } finally {
    fileInput.value = "";
  }
}

function renderPdfReports(data) {
  pdfReportList.replaceChildren();
  pdfReportList.classList.remove("hidden");
  const reports = data.reports || [];
  pdfReportMeta.textContent = reports.length
    ? reports.length + " 份 PDF · OCR " + data.ocr_language
    : "没有 PDF";

  reports.forEach(function (report) {
    const item = document.createElement("div");
    item.className = "pdf-report-item";
    const name = document.createElement("div");
    name.className = "pdf-report-name";
    name.title = report.source;
    name.textContent = report.source;
    const summary = document.createElement("div");
    summary.className = "pdf-report-summary";
    const problemPages = report.low_text_pages + report.empty_pages;
    if (problemPages) summary.classList.add("warning");
    if (report.parser === "mineru") {
      summary.textContent =
        "MinerU · " + report.total_pages + " 页 · " +
        report.searchable_blocks + " 个可检索内容块";
    } else {
      summary.textContent =
        "快速解析 · " + report.total_pages + " 页 · 原生 " + report.native_pages +
        " · OCR " + report.ocr_pages + " · 异常 " + problemPages;
    }
    item.append(name, summary);
    pdfReportList.append(item);
  });
}

pdfReportButton.addEventListener("click", async function () {
  pdfReportButton.disabled = true;
  pdfReportButton.textContent = "检查中…";
  pdfReportMeta.textContent = "正在逐页分析";
  try {
    const data = await api("/api/pdf-reports");
    renderPdfReports(data);
  } catch (error) {
    pdfReportMeta.textContent = "检查失败";
    showToast(error.message);
  } finally {
    pdfReportButton.disabled = false;
    pdfReportButton.textContent = "检查 PDF 提取";
  }
});

selectAllSources.addEventListener("change", function () {
  document.querySelectorAll(".source-check").forEach(function (checkbox) {
    checkbox.checked = selectAllSources.checked;
    const name = checkbox.dataset.source;
    if (checkbox.checked) selectedSources.add(name);
    else selectedSources.delete(name);
  });
  updateSelectAllState();
});

async function loadStatus() {
  try {
    const data = await api("/api/status");
    defaultThresholds = data.default_thresholds || defaultThresholds;
    minScoreInput.value = defaultThresholds[backendInput.value];
    const qdrantState = data.qdrant && data.qdrant.available
      ? "Qdrant 在线"
      : "Qdrant 离线";
    const embeddingState = data.embedding && data.embedding.available
      ? "Qwen 嵌入在线"
      : "Qwen 嵌入离线";
    systemStatus.textContent = data.model + " · " + data.files + " 份资料 · " + qdrantState + " · " + embeddingState;
    statusDot.classList.toggle("offline", !data.model_configured);
  } catch (error) {
    systemStatus.textContent = "服务状态不可用";
    statusDot.classList.add("offline");
  }
}

backendInput.addEventListener("change", function () {
  minScoreInput.value = defaultThresholds[backendInput.value];
});

fileInput.addEventListener("change", function () {
  if (fileInput.files[0]) uploadFile(fileInput.files[0]);
});

["dragenter", "dragover"].forEach(function (eventName) {
  dropZone.addEventListener(eventName, function (event) {
    event.preventDefault();
    dropZone.classList.add("dragging");
  });
});

["dragleave", "drop"].forEach(function (eventName) {
  dropZone.addEventListener(eventName, function (event) {
    event.preventDefault();
    dropZone.classList.remove("dragging");
  });
});

dropZone.addEventListener("drop", function (event) {
  if (event.dataTransfer.files[0]) uploadFile(event.dataTransfer.files[0]);
});

document.querySelectorAll(".mode-tab").forEach(function (button) {
  button.addEventListener("click", function () {
    currentMode = button.dataset.mode;
    document.querySelectorAll(".mode-tab").forEach(function (item) {
      const active = item === button;
      item.classList.toggle("active", active);
      item.setAttribute("aria-selected", String(active));
    });
    const planning = currentMode === "plan";
    savePlanControl.classList.toggle("hidden", !planning);
    questionLabel.textContent = planning ? "研究目标" : "问题";
    questionInput.placeholder = planning
      ? "输入需要拆解和安排的研究目标"
      : "输入需要基于资料回答的问题";
    comparisonModeChanged();
  });
});

function appendResultSection(titleText, className) {
  const section = document.createElement("section");
  section.className = "result-section";
  const title = document.createElement("h3");
  title.textContent = titleText;
  const list = document.createElement("div");
  list.className = className;
  section.append(title, list);
  resultPanel.append(section);
  return list;
}

function buildRequestPayload(question) {
  return {
    mode: currentMode,
    question: question,
    topic: document.getElementById("topic").value.trim(),
    hours: Number(document.getElementById("hours").value),
    backend: backendInput.value,
    file_type: fileTypeInput.value,
    min_score: Number(minScoreInput.value),
    k: Number(resultCountInput.value),
    sources: Array.from(selectedSources),
    allow_external: document.getElementById("allowExternal").checked,
    save_plan: document.getElementById("savePlan").checked
  };
}

function renderSearchPanel(searches, metadata) {
  searchPanel.replaceChildren();
  searchPanel.classList.remove("hidden");

  const heading = document.createElement("div");
  heading.className = "search-heading";
  const title = document.createElement("h2");
  title.textContent = "检索证据";
  const meta = document.createElement("span");
  meta.textContent = metadata || "Agent 实际工具调用";
  heading.append(title, meta);
  searchPanel.append(heading);

  if (!searches || !searches.length) {
    const empty = document.createElement("div");
    empty.className = "no-evidence";
    empty.textContent = "没有检索调用或可靠证据。";
    searchPanel.append(empty);
    return;
  }

  searches.forEach(function (search, searchIndex) {
    if (searches.length > 1 || search.query) {
      const query = document.createElement("div");
      query.className = "search-query";
      query.textContent = "检索 " + (searchIndex + 1) + "：" + search.query;
      searchPanel.append(query);
    }

    if (!search.results.length) {
      const empty = document.createElement("div");
      empty.className = "no-evidence";
      empty.textContent = "没有结果达到当前最低相关度。";
      searchPanel.append(empty);
      return;
    }

    const list = document.createElement("div");
    list.className = "evidence-list";
    search.results.forEach(function (result) {
      const item = document.createElement("article");
      item.className = "evidence-item";
      const itemMeta = document.createElement("div");
      itemMeta.className = "evidence-meta";
      const source = document.createElement("span");
      source.textContent = result.source + (result.page ? " · 第 " + result.page + " 页" : "");
      const score = document.createElement("span");
      score.className = "evidence-score";
      score.textContent = "相关度 " + Number(result.score).toFixed(3);
      const quote = document.createElement("p");
      quote.className = "evidence-quote";
      quote.textContent = result.quote || (result.modality === "image" ? "已定位图像证据" : "");
      itemMeta.append(source, score);
      item.append(itemMeta);
      if (result.image_url) {
        const image = document.createElement("img");
        image.className = "evidence-image";
        image.src = result.image_url;
        image.alt = result.content_type + " · " + result.source;
        image.loading = "lazy";
        item.append(image);
      }
        item.append(quote);
        if (result.source?.toLowerCase().endsWith(".pdf") && result.page && result.quote) {
          const noteLink = document.createElement("a");
          noteLink.href = "/reader?source=" + encodeURIComponent(result.source) + "&page=" + result.page;
          noteLink.textContent = "打开原文 / 记录理解";
          noteLink.addEventListener("click", function () {
            sessionStorage.setItem("reading-evidence", JSON.stringify({source: result.source, page: result.page, quote: result.quote, evidence_id: result.evidence_id}));
          });
          item.append(noteLink);
        }
      if (result.context_span) {
        const context = document.createElement("p");
        context.className = "evidence-context";
        context.textContent = (result.context_span.complete_block ? "原始段落已补全" : "有界上下文") +
          " · " + (result.context_evidence_ids?.length || 1) + " 个命中片段";
        item.append(context);
      }
      if (result.quality_warnings?.length) {
        const warning = document.createElement("p");
        warning.className = "evidence-quality";
        warning.textContent = result.quality_warnings.join(" ");
        item.append(warning);
      }
      if (result.retrieved_text && result.retrieved_text !== result.quote) {
        const details = document.createElement("details"), summary = document.createElement("summary"), original = document.createElement("p");
        summary.textContent = result.modality === "image" ? "未核验图表转写" : "原始命中片段";
        original.className = "evidence-quote"; original.textContent = result.retrieved_text;
        details.append(summary, original); item.append(details);
      }
      if (result.content_type === "table") {
        const tableLink = document.createElement("a");
        tableLink.href = "/lab?tab=tables&source=" + encodeURIComponent(result.source) + "&page=" + (result.page || "");
        tableLink.textContent = "查看结构化表格";
        item.append(tableLink);
      }
      list.append(item);
    });
    searchPanel.append(list);
  });
}

previewButton.addEventListener("click", async function () {
  const question = questionInput.value.trim();
  if (!question) {
    showToast("请输入需要检索的问题");
    return;
  }
  if (!selectedSources.size) {
    showToast("请至少选择一份资料");
    return;
  }

  previewButton.disabled = true;
  previewButton.textContent = "检索中…";
  try {
    const result = await api("/api/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(buildRequestPayload(question))
    });
    renderSearchPanel(
      [{ query: result.query, results: result.results }],
      result.effective_backend + " · 阈值 " + result.min_score
    );
    if (result.warning) showToast("统一多模态检索不可用，已自动降级");
  } catch (error) {
    showToast(error.message);
  } finally {
    previewButton.disabled = false;
    previewButton.textContent = "只检索";
  }
});

function renderResult(payload) {
  const response = payload.response;
  resultPanel.replaceChildren();

  if (response.warning) {
    const warning = document.createElement("div");
    warning.className = "degraded-warning";
    warning.textContent = response.warning;
    resultPanel.append(warning);
  }

  const summary = document.createElement("p");
  summary.className = "result-summary";
  summary.textContent = response.summary;
  resultPanel.append(summary);

  if (response.citations.length) {
    const list = appendResultSection("参考证据", "citation-list");
    response.citations.forEach(function (citation) {
      const item = document.createElement("article");
      item.className = "citation-item";
      const source = document.createElement("div");
      source.className = "citation-source";
      source.textContent = citation.source + (citation.page ? " · 第 " + citation.page + " 页" : "");
      const quote = document.createElement("p");
      quote.className = "citation-quote";
      quote.textContent = citation.quote;
      item.append(source, quote);
      list.append(item);
    });
  }

  if (response.tasks.length) {
    const list = appendResultSection("研究任务", "task-list");
    response.tasks.forEach(function (task) {
      const item = document.createElement("article");
      item.className = "task-item";
      const title = document.createElement("div");
      title.className = "task-title";
      title.textContent = "[" + task.task_id + "] " + task.title;
      const purpose = document.createElement("p");
      purpose.className = "task-purpose";
      purpose.textContent = task.purpose;
      const meta = document.createElement("div");
      meta.className = "task-meta";
      ["优先级 " + task.priority, task.estimated_hours + " 小时",
       "依赖 " + (task.dependencies.join(", ") || "无")].forEach(function (text) {
        const span = document.createElement("span");
        span.textContent = text;
        meta.append(span);
      });
      item.append(title, purpose, meta);
      list.append(item);
    });
  }

  if (payload.saved_path) {
    const saved = document.createElement("div");
    saved.className = "saved-path";
    saved.textContent = "计划已保存：" + payload.saved_path;
    resultPanel.append(saved);
  }

  renderSearchPanel(payload.searches || [], "Agent 实际工具调用");
}

form.addEventListener("submit", async function (event) {
  event.preventDefault();
  const question = questionInput.value.trim();
  if (!question) {
    showToast(currentMode === "plan" ? "请输入研究目标" : "请输入问题");
    return;
  }
  if (!selectedSources.size) {
    showToast("请至少选择一份资料");
    return;
  }

  const payload = buildRequestPayload(question);
  if (currentMode === "compare") {
    await startComparison(payload);
    return;
  }

  runButton.disabled = true;
  runStatus.textContent = "正在检索与分析…";
  permissionError.classList.add("hidden");
  try {
    const result = await api("/api/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload)
    });
    renderResult(result);
  } catch (error) {
    if (error.code === "external_permission_required") {
      permissionError.textContent = error.message;
      permissionError.classList.remove("hidden");
    } else {
      const suffix = error.requestId ? "（请求 " + error.requestId + "）" : "";
      showToast(error.message + suffix);
    }
  } finally {
    runButton.disabled = false;
    runStatus.textContent = "";
  }
});

document.getElementById("allowExternal").addEventListener("change", function () {
  if (this.checked) permissionError.classList.add("hidden");
});

loadLibrary();
loadStatus();
