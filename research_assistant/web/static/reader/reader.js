// PDF user-space anchors survive CSS scaling, viewport rotation and crop offsets.
import { selectionLines } from "./selection_geometry.js";
import { renderPixels } from "./render_quality.js";
import { confirmedMatch, sentenceBounds } from "./selection_sync.js?v=sentence-v6";
const $ = (id) => document.getElementById(id);
let pdfjs,
  papers = [],
  paper = null,
  annotations = [],
  panes = [],
  selection = null,
  editing = null,
  anchors = [],
  sourceLinks = [],
  linkingIndex = null,
  dirty = false,
  historyView = false;
const docs = new Map();
let translationEpoch = 0;
let syncEpoch = 0;
let selectionCaptureScheduled = false;
document.addEventListener("mouseup", () => {
  if (selectionCaptureScheduled) return;
  selectionCaptureScheduled = true;
  requestAnimationFrame(() => {
    selectionCaptureScheduled = false;
    const selected = window.getSelection();
    if (!selected || selected.isCollapsed || !selected.rangeCount) return;
    const range = selected.getRangeAt(0);
    const pane = panes.find(
      (candidate) =>
        candidate.text?.contains(range.startContainer) &&
        candidate.text?.contains(range.endContainer),
    );
    if (pane) guard(() => capture(pane));
  });
});
function clearSelectionSync() {
  syncEpoch++;
  $("selection-sync-status").textContent = "";
  for (const p of panes) { p.syncAnchor = null; p.paint(); }
}
$("sync-selection").onchange = clearSelectionSync;
let submittingTranslation = false, pollingJobs = false;
function translationFeedback(text, error = false, working = false) {
  $("translation-feedback").hidden = !text;
  $("translation-feedback").textContent = text;
  $("translation-feedback").classList.toggle("error", error);
  if (working) {
    const bar = el("progress"); bar.setAttribute("aria-label", "正在提交翻译任务");
    $("translation-feedback").append(bar);
  }
}
function sidePanel(id) {
  document.querySelectorAll(".side-panel").forEach((p) => { p.hidden = p.id !== id; });
  document.querySelectorAll("[data-panel]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.panel === id)));
}
document.querySelectorAll("[data-panel]").forEach((b) => b.onclick = () => sidePanel(b.dataset.panel));
$("focus").onclick = () => {
  const focused = document.body.classList.toggle("focus-reading");
  $("focus").textContent = focused ? "显示手记" : "专注阅读";
  guard(() => Promise.all(panes.filter((p) => p.fitWidth).map((p) => p.render())));
};
function syncControls() {
  const enabled = panes.length === 2 && panes[0].artifact.metadata.page_count === panes[1].artifact.metadata.page_count;
  $("sync-pages").disabled = !enabled;
  $("panes").classList.toggle("comparing", panes.length === 2);
  $("compare").classList.toggle("active", panes.length === 2);
  return enabled;
}
async function navigate(pane, page) {
  clearSelectionSync();
  if (!Number.isInteger(page) || page < 1 || page > pane.artifact.metadata.page_count) {
    pane.number.value = pane.page;
    return;
  }
  translationEpoch++;
  selection = null;
  $("selection").textContent = "请选择本页文字，记录你的理解。";
  const targets = syncControls() && $("sync-pages").checked ? panes : [pane];
  await Promise.all(targets.map((p) => p.render(page)));
}
$("sync-pages").onchange = () => guard(() => navigate(panes[0], panes[0].page));
let resizeTimer;
window.addEventListener("resize", () => {
  clearTimeout(resizeTimer);
  resizeTimer = setTimeout(() => guard(() => Promise.all(panes.filter((p) => p.fitWidth).map((p) => p.render()))), 180);
});
function notify(text, error = false) {
  $("notice").textContent = text;
  $("notice").classList.toggle("error", error);
}
async function api(path, data, timeout = 0) {
  const r = await fetch(
    "/api/reading/" + path,
    { ...(data === undefined
      ? {}
      : {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(data),
        }), ...(timeout ? {signal: AbortSignal.timeout(timeout)} : {}) },
  );
  const v = await r.json();
  if (!r.ok) {
    const e = new Error(v.error || "请求失败");
    e.status = r.status;
    throw e;
  }
  return v;
}
function el(tag, text, cls) {
  const n = document.createElement(tag);
  if (text !== undefined) n.textContent = text;
  if (cls) n.className = cls;
  return n;
}
function action(text, fn) {
  const b = el("button", text);
  b.onclick = () => guard(fn);
  return b;
}
async function guard(fn) {
  try {
    return await fn();
  } catch (e) {
    notify(e.message || String(e), true);
  }
}
const kindName = (a) =>
  a.kind === "original" ? "论文原文" : "机器译文（对应需核对）";
const noteName = (n) =>
  n.confirmed
    ? n.author_source === "ai_assisted"
      ? "你的记录 · AI 辅助，已确认"
      : "你的记录"
    : "AI 草稿 · 未确认";
function artifact(id) {
  return paper.artifacts.find((a) => a.id === id);
}
function sourceURL(a) {
  return (
    "/reader?" +
    new URLSearchParams({
      paper: paper.id,
      artifact: a.artifact_id,
      page: a.page,
    })
  );
}
async function pdfDoc(a) {
  if (!docs.has(a.id))
    docs.set(
      a.id,
      pdfjs.getDocument({
        url: a.url,
        cMapUrl: "/reader-assets/cmaps/",
        cMapPacked: true,
        standardFontDataUrl: "/reader-assets/standard_fonts/",
      }).promise,
    );
  return docs.get(a.id);
}

class Pane {
  constructor(a) {
    this.artifact = a;
    this.page = 1;
    this.scale = 1;
    this.fitWidth = true;
    this.turn = 0;
    this.token = 0;
    this.node = el("div", undefined, "pane");
    this.controls = el("div", undefined, "pane-controls");
    this.label = el("span", kindName(a), "tag");
    this.zoomLabel = el("span", "", "zoom-label");
    this.number = el("input");
    this.number.type = "number";
    this.number.min = 1;
    this.number.max = a.metadata.page_count;
    this.number.value = 1;
    this.number.setAttribute("aria-label", "页码");
    this.number.onchange = () =>
      guard(() => navigate(this, Number(this.number.value)));
    this.scroll = el("div", undefined, "scroll");
    this.controls.append(
      this.label,
      action("上一页", () => navigate(this, this.page - 1)),
      this.number,
      el("span", "/ " + a.metadata.page_count),
      action("下一页", () => navigate(this, this.page + 1)),
      action("缩小", () => {
        this.fitWidth = false;
        this.scale = Math.max(0.4, this.scale - 0.2);
        return this.render();
      }),
      action("放大", () => {
        this.fitWidth = false;
        this.scale = Math.min(3, this.scale + 0.2);
        return this.render();
      }),
      action("旋转", () => {
        this.turn = (this.turn + 90) % 360;
        return this.render();
      }),
      action("适应宽度", () => { this.fitWidth = true; return this.render(); }),
      action("150%阅读", () => { this.fitWidth = false; this.scale = 1.5; return this.render(); }),
      this.zoomLabel,
    );
    this.node.append(this.controls, this.scroll);
  }
  async render(page = this.page) {
    if (
      !Number.isInteger(page) ||
      page < 1 ||
      page > this.artifact.metadata.page_count
    )
      return;
    const token = ++this.token;
    this.node.setAttribute("aria-busy", "true");
    this.page = page;
    this.number.value = page;
    const doc = await pdfDoc(this.artifact),
      p = await doc.getPage(page);
    if (token !== this.token) return;
    if (this.fitWidth) {
      const natural = p.getViewport({ scale: 1, rotation: (p.rotate + this.turn) % 360 });
      this.scale = Math.max(0.15, Math.min(2, (this.scroll.clientWidth - 32) / natural.width));
    }
    const vp = p.getViewport({
      scale: this.scale,
      rotation: (p.rotate + this.turn) % 360,
    });
    this.viewport = vp;
    this.zoomLabel.textContent = Math.round(this.scale * 100) + "%";
    this.pdfPage = p;
    const wrap = el("div", undefined, "pdf-page");
    wrap.style.width = vp.width + "px";
    wrap.style.height = vp.height + "px";
    wrap.style.setProperty("--scale-factor", vp.scale);
    wrap.style.setProperty("--total-scale-factor", vp.scale);
    wrap.style.setProperty("--user-unit", p.userUnit);
    const canvas = el("canvas");
    const pixels = renderPixels(vp.width, vp.height, window.devicePixelRatio);
    canvas.width = pixels.width;
    canvas.height = pixels.height;
    canvas.style.width = vp.width + "px";
    canvas.style.height = vp.height + "px";
    const text = el("div", undefined, "textLayer");
    const marks = el("div", undefined, "marks");
    wrap.append(canvas, text, marks);
    this.scroll.replaceChildren(wrap);
    this.wrap = wrap;
    this.text = text;
    this.marks = marks;
    await p.render({
      canvasContext: canvas.getContext("2d"),
      viewport: vp,
      transform: pixels.transform,
    }).promise;
    if (token !== this.token) return;
    this.textLayer = new pdfjs.TextLayer({
      textContentSource: await p.getTextContent(),
      container: text,
      viewport: vp,
    });
    await this.textLayer.render();
    if (token !== this.token) return;
    this.paint();
    this.node.setAttribute("aria-busy", "false");
  }
  paint(focus) {
    if (!this.marks) return;
    this.marks.replaceChildren();
    const list = annotations
      .filter(
        (n) =>
          n.anchor.artifact_id === this.artifact.id &&
          n.anchor.page === this.page,
      )
      .map((n) => ({ anchor: n.anchor, style: n.style }));
    if (focus) list.push({ anchor: focus, style: "focus" });
    if (this.syncAnchor?.page === this.page) list.push({anchor:this.syncAnchor,style:this.syncStyle || "sync-candidate"});
    for (const n of list)
      for (const rect of n.anchor.rects) {
        const r = this.viewport.convertToViewportRectangle(rect);
        const mark = el("div", undefined, "mark " + n.style);
        mark.style.left = Math.min(r[0], r[2]) + "px";
        mark.style.top = Math.min(r[1], r[3]) + "px";
        mark.style.width = Math.abs(r[2] - r[0]) + "px";
        mark.style.height = Math.abs(r[3] - r[1]) + "px";
        // Underlines rotate with the PDF baseline, rather than the browser screen.
        if (n.style === "underline") {
          mark.style.border = "none";
          mark.style[
            "border" +
              ["Bottom", "Left", "Top", "Right"][this.viewport.rotation / 90]
          ] = "2px solid #df7700";
        }
        this.marks.append(mark);
      }
  }
}

function capture(pane) {
  clearSelectionSync();
  translationEpoch++;
  $("selection-translation").replaceChildren();
  selection = null;
  $("selection").textContent = "请选择一页的一栏内的文字。";
  const s = window.getSelection();
  if (!s || s.isCollapsed || !s.rangeCount) return;
  let range = s.getRangeAt(0);
  if (
    !pane.text.contains(range.startContainer) ||
    !pane.text.contains(range.endContainer)
  )
    throw Error("跨页或跨窗口选区不受支持，请在一页的一栏内分段选择。");
  const nodes=[];
  const nodeWalker=document.createTreeWalker(pane.text,NodeFilter.SHOW_TEXT);
  while(nodeWalker.nextNode())if(nodeWalker.currentNode.textContent)nodes.push(nodeWalker.currentNode);
  const starts=new Map();let pageText="";
  for(const node of nodes){starts.set(node,pageText.length);pageText+=node.textContent;}
  if(!starts.has(range.startContainer) || !starts.has(range.endContainer))
    throw Error("无法确定当前文字的完整句，请重新选择句中正文。");
  const sentence=sentenceBounds(pageText,starts.get(range.startContainer)+range.startOffset,
    starts.get(range.endContainer)+range.endOffset);
  if(!sentence)throw Error("请选择同一个完整句中的文字；跨句或句界不清的选区不保存。");
  const point=(offset,endPoint=false)=>{
    for(const node of nodes){const start=starts.get(node),finish=start+node.textContent.length;
      if((!endPoint && start<=offset && offset<finish) || (endPoint && start<offset && offset<=finish))
        return [node,offset-start];
    }
    return endPoint ? [nodes.at(-1),nodes.at(-1).textContent.length] : [nodes[0],0];
  };
  const expanded=document.createRange(),a=point(sentence[0]),b=point(sentence[1],true);
  expanded.setStart(a[0],a[1]);expanded.setEnd(b[0],b[1]);range=expanded;
  s.removeAllRanges();s.addRange(range);
  const excerpt = range.toString().trim();
  if (!excerpt) return;
  let boxes = [];
  // Intersect each leaf text node; using the parent Range rectangle would cover the column gutter.
  const walker = document.createTreeWalker(pane.text, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    const node = walker.currentNode;
    if (!range.intersectsNode(node) || !node.textContent.trim()) continue;
    const r = document.createRange();
    r.selectNodeContents(node);
    if (node === range.startContainer) r.setStart(node, range.startOffset);
    if (node === range.endContainer) r.setEnd(node, range.endOffset);
    for (const b of r.getClientRects())
      if (b.width > 0.1 && b.height > 0.1) boxes.push(b);
  }
  if (!boxes.length || boxes.length > 200)
    throw Error("选区为空或过大，请缩小范围。");
  const bounds = pane.wrap.getBoundingClientRect(),
    sx = pane.viewport.width / bounds.width,
    sy = pane.viewport.height / bounds.height;
  const meta = pane.artifact.metadata.pages[pane.page - 1],
    box = meta.view_box;
  const rects = boxes.map((r) => {
    const a = pane.viewport.convertToPdfPoint(
        (r.left - bounds.left) * sx,
        (r.top - bounds.top) * sy,
      ),
      b = pane.viewport.convertToPdfPoint(
        (r.right - bounds.left) * sx,
        (r.bottom - bounds.top) * sy,
      );
    return [
      Math.max(box[0], Math.min(a[0], b[0])),
      Math.max(box[1], Math.min(a[1], b[1])),
      Math.min(box[2], Math.max(a[0], b[0])),
      Math.min(box[3], Math.max(a[1], b[1])),
    ].map((n) => Math.round(n * 1000) / 1000);
  });
  selection = {
    artifact_id: pane.artifact.id,
    artifact_hash: pane.artifact.hash,
    page: pane.page,
    coordinate_system: "pdf_user_space",
    view_box: box,
    rects: selectionLines(rects),
    excerpt,
  };
  $("selection").textContent =
    kindName(pane.artifact) + " · 第 " + pane.page + " 页\n" + excerpt;
  // Keep the normalized full sentence visible after the native browser selection loses focus.
  pane.syncAnchor = structuredClone(selection);
  pane.syncStyle = "sync-picked";
  pane.paint();
  renderLinkPrompt();
  notify("已按完整句捕获。翻译、划线和笔记将使用同一句来源。");
  if (pane.artifact.kind === "translation" && $("sync-selection").checked && linkingIndex === null)
    guard(() => syncOriginalSelection(pane, structuredClone(selection)));
}

async function syncOriginalSelection(translatedPane, picked) {
  const epoch=syncEpoch, requestedPaper=paper.id;
  const valid=()=>epoch===syncEpoch && paper.id===requestedPaper && $("sync-selection").checked;
  const status=$("selection-sync-status");
  status.setAttribute("aria-busy", "true");
  status.textContent="正在定位对应的完整原文句…首次计算通常需要 8–20 秒。";
  try {
    const notes=await api("notes?paper_id="+requestedPaper,undefined,10000);
    if(!valid())return;
    const match=confirmedMatch(picked,notes);
    if(match.ambiguous){status.textContent="已有手动关联存在冲突，请核对笔记来源。";return;}
    const original=artifact(translatedPane.artifact.parent_id);
    if(!original)throw Error("找不到译文绑定的原件版本。");
    let targetAnchor=match.anchor;
    if(targetAnchor && (targetAnchor.artifact_id!==original.id || targetAnchor.artifact_hash!==original.hash))targetAnchor=null;
    const userConfirmed=Boolean(targetAnchor);
    let alignmentMessage="";
    if(!targetAnchor) {
      status.textContent="正在进行本地完整句对齐…首次需建立语义缓存，不调用付费 API。";
      // Debounce before CPU work. Stale responses never change the next selection.
      await new Promise(resolve=>setTimeout(resolve,300));
      if(!valid())return;
      const result=await api("alignment",{paper_id:requestedPaper,anchor:picked},85000);
      if(!valid())return;
      if(result.status==="aligned") {
        const pairs=result.anchors;
        if(result.alignment_unit!=="sentence" || pairs?.length!==1 || pairs.some(a=>a.artifact_id!==original.id || a.artifact_hash!==original.hash))
          throw Error("对齐返回的原件版本或页码无效。");
        targetAnchor=pairs[0];
        alignmentMessage=result.message+(result.cached ? "（已复用缓存）" : "");
      } else {
        status.textContent=result.message || "无法可靠对齐，请手动补选原文。";
        return;
      }
    }
    let originalPane=panes.find(p=>p.artifact.id===original.id);
    if(!originalPane){
      originalPane=new Pane(original);
      panes=[originalPane,translatedPane];$("panes").replaceChildren(...panes.map(p=>p.node));syncControls();
      await translatedPane.render(picked.page);
    }
    if(!valid())return;
    await originalPane.render(targetAnchor?.page || picked.page);
    if(!valid())return;
    translatedPane.syncAnchor=picked;translatedPane.syncStyle="sync-confirmed";translatedPane.paint();
    originalPane.syncAnchor=targetAnchor;originalPane.syncStyle=userConfirmed ? "sync-confirmed" : "sync-machine";originalPane.paint();
    originalPane.marks.querySelector(".sync-confirmed, .sync-machine")?.scrollIntoView({block:"nearest",behavior:"smooth"});
    status.textContent=userConfirmed ? "已联动到你确认过的原文选区（蓝色）。" : alignmentMessage;
  } catch(e){if(valid())status.textContent="原文联动暂不可用："+e.message;}
  finally { if(valid())status.setAttribute("aria-busy", "false"); }
}

async function showArtifact(id, page = 1) {
  clearSelectionSync();
  const a = artifact(id);
  if (!a) throw Error("该历史文档版本不存在。");
  panes = [new Pane(a)];
  $("panes").replaceChildren(panes[0].node);
  syncControls();
  $("versions").value = id;
  $("download").href = a.url + "?download=1";
  await panes[0].render(page);
}
async function gotoAnchor(a) {
  let target = panes.find((p) => p.artifact.id === a.artifact_id);
  if (target) await navigate(target, a.page);
  else { await showArtifact(a.artifact_id, a.page); target = panes[0]; }
  target.paint(a);
  const first = target.marks.querySelector(".focus");
  first?.scrollIntoView({ block: "center", behavior: "smooth" });
}
function renderAnchors() {
  const container = $("anchors");
  container.replaceChildren();
  anchors.forEach((a, i) => {
    const row = el("div");
    const link = el(
      "a",
      (artifact(a.artifact_id)?.kind === "original"
        ? "原文"
        : sourceLinks.some(l => l.translation_index === i) ? "机器译文 · 已手动关联原文" : "机器译文 · 未关联原文") +
        " p." +
        a.page +
        " · " +
        a.excerpt.slice(0, 45),
    );
    link.href = sourceURL(a);
    link.onclick = (e) => {
      e.preventDefault();
      guard(() => gotoAnchor(a));
    };
    row.append(
      link,
      action("移除此来源", () => {
        anchors.splice(i, 1);
        sourceLinks = sourceLinks.filter(l => l.translation_index !== i && l.original_index !== i)
          .map(l => ({...l, translation_index: l.translation_index > i ? l.translation_index - 1 : l.translation_index,
            original_index: l.original_index > i ? l.original_index - 1 : l.original_index}));
        linkingIndex = null;
        dirty = true;
        renderAnchors();
      }),
    );
    const matched = sourceLinks.find(l => l.translation_index === i);
    if (artifact(a.artifact_id)?.kind === "translation") {
      if (matched) {
        row.append(el("p", "已手动关联原文 · 用户确认，非自动语义核验", "hint"),
          action("返回对应原文", () => gotoAnchor(anchors[matched.original_index])));
      } else row.append(el("p", "仅定位译文；还不能返回对应的完整原文句。", "hint"));
      row.append(action(matched ? "重新关联原文句" : "补选对应原文句", () => beginSourceLink(i)));
    }
    container.append(row);
  });
  if (
    anchors.some((a) => artifact(a.artifact_id)?.kind === "translation") &&
    anchors.some((a) => artifact(a.artifact_id)?.kind === "original")
  )
    container.append(
      el("p", "原文与译文分开保存坐标；关联仅代表你的确认，不改变论文事实与个人观点的边界。", "hint"),
    );
  renderLinkPrompt();
}
function renderLinkPrompt() {
  const box = $("source-linking"); box.replaceChildren();
  if (linkingIndex === null) return;
  box.append(el("p", "请在对应原文句中选择任意文字，系统扩展为完整句后再确认。不会把同页码视为对应关系。", "hint"));
  if (selection && artifact(selection.artifact_id)?.kind === "original") {
    box.append(el("p", `待关联原文 p.${selection.page}：${selection.excerpt.slice(0, 160)}`, "hint"));
    box.append(action("确认这两段对应", () => {
      if (!selection || artifact(selection.artifact_id)?.kind !== "original") throw Error("请重新选择原文。");
      if (!confirm("确认原文选区与这段译文对应？此关系标记为你手动确认，不代表系统验证了翻译准确性。")) return;
      const original = structuredClone(selection);
      let index = anchors.findIndex(a => a.artifact_id === original.artifact_id && a.page === original.page && a.excerpt === original.excerpt && JSON.stringify(a.rects) === JSON.stringify(original.rects));
      if (index < 0) { index = anchors.length; anchors.push(original); }
      sourceLinks = sourceLinks.filter(l => l.translation_index !== linkingIndex);
      sourceLinks.push({translation_index: linkingIndex, original_index: index, confirm: true});
      linkingIndex = null; dirty = true; renderAnchors();
      notify("原文关联已加入编辑，请保存笔记以追加版本。");
    }));
  }
  box.append(action("取消补选", () => { linkingIndex = null; renderLinkPrompt(); }));
}
async function beginSourceLink(index) {
  clearSelectionSync();
  if (historyView) throw Error("历史只读，请打开最新版本。");
  const translation = artifact(anchors[index].artifact_id);
  const original = artifact(translation.parent_id);
  if (!original) throw Error("找不到译文绑定的原件版本。");
  linkingIndex = index; selection = null;
  $("sync-pages").checked = false;
  panes = [new Pane(original), new Pane(translation)];
  $("panes").replaceChildren(...panes.map(p => p.node)); syncControls();
  $("versions").value = translation.id; $("download").href = translation.url + "?download=1";
  // The same-number page is only a browsing starting point, never a saved mapping.
  await Promise.all([panes[0].render(Math.min(anchors[index].page, original.metadata.page_count)), panes[1].render(anchors[index].page)]);
  panes[1].paint(anchors[index]); sidePanel("note-panel"); renderLinkPrompt();
  notify("右侧保留译文选区，左侧请找到原文并补选；已关闭同步翻页。");
}
function resetEditor() {
  editing = null;
  anchors = [];
  sourceLinks = []; linkingIndex = null;
  dirty = false;
  historyView = false;
  $("body").value = "";
  $("history").replaceChildren();
  $("editing").textContent = "新笔记";
  $("save").disabled = false;
  renderAnchors();
}
async function openNote(n, historical = false) {
  sidePanel("note-panel");
  if (dirty && !confirm("放弃当前未保存的编辑并打开这条笔记？")) return;
  const history = await api("history/" + n.id);
  historical = historical || n.revision !== history[0].revision;
  editing = n;
  anchors = structuredClone(n.anchors);
  sourceLinks = structuredClone(n.source_links || []); linkingIndex = null;
  $("body").value = n.body;
  $("type").value = n.type;
  dirty = false;
  historyView = historical;
  $("save").disabled = historical;
  $("editing").textContent =
    noteName(n) +
    " · revision " +
    n.revision +
    (historical ? " · 历史只读" : "") +
    (n.archived ? " · 已归档" : "");
  renderAnchors();
  $("history").replaceChildren();
  for (const h of history)
    $("history").append(
      action("v" + h.revision + " · " + h.modified_at, () =>
        openNote(h, h.revision !== history[0].revision),
      ),
    );
  if (anchors[0]) await gotoAnchor(anchors[0]);
}
async function refreshNotes() {
  if (!paper) return;
  const list = await api("notes?paper_id=" + paper.id);
  $("notes").replaceChildren();
  for (const n of list) {
    if (n.archived && !$("showArchived").checked) continue;
    const card = el("div", undefined, "note");
    card.append(
      el(
        "div",
        noteName(n) + " · v" + n.revision + (n.archived ? " · 已归档" : ""),
        "tag",
      ),
      el("p", n.body),
      action("打开 / 编辑", () => openNote(n)),
    );
    $("notes").append(card);
  }
}
async function save(extra = {}) {
  if (historyView) throw Error("历史版本只读，请先打开最新版本。");
  if (!paper) throw Error("请先打开论文。");
  const body = $("body").value;
  const payload = {
    paper_id: paper.id,
    type: $("type").value,
    body,
    anchors,
    source_links: sourceLinks,
    ...extra,
  };
  if (editing) payload.expected_revision = editing.revision;
  try {
    const note = await api(
      "notes" + (editing ? "/" + editing.id : ""),
      payload,
    );
    dirty = false;
    await openNote(note);
    await refreshNotes();
    notify("已原子保存 revision " + note.revision + "；旧版本保留。");
  } catch (e) {
    if (e.status === 409) {
      notify(e.message + " 你的输入仍保留。请复制或对照最新版本后合并。", true);
      $("history").append(
        action("查看服务器最新正文（不覆盖输入）", async () => {
          const latest = await api("notes/" + editing.id);
          $("history").append(
            el("pre", "最新 v" + latest.revision + "\n" + latest.body),
          );
        }),
        action("以最新版本为基础保存我的编辑", async () => {
          const latest = await api("notes/" + editing.id);
          if (confirm("请确保已对照最新正文；将当前输入追加为新版本？")) {
            editing = latest;
            await save(extra);
          }
        }),
      );
    } else throw e;
  }
}

async function loadPapers(selectId) {
  const data = await api("papers");
  papers = data.papers;
  $("papers").replaceChildren();
  for (const p of papers) {
    const o = el("option", p.source);
    o.value = p.id;
    $("papers").append(o);
  }
  if (data.errors.length)
    notify(data.errors.map((x) => x.source + "：" + x.error).join("\n"), true);
  if (papers.length) {
    await choosePaper(selectId || paper?.id || papers[0].id);
  } else notify("资料库暂无可阅读 PDF，请上传。");
}
async function choosePaper(id) {
  clearSelectionSync();
  translationEpoch++;
  $("selection-translation").replaceChildren();
  paper = papers.find((p) => p.id === id) || papers[0];
  if (!paper) return;
  $("papers").value = paper.id;
  annotations = await api("annotations?paper_id=" + paper.id);
  $("versions").replaceChildren();
  for (const a of paper.artifacts) {
    const option = el(
      "option",
      (a.kind === "original" ? "原文" : "中文译文 · 待核对") +
        " · " +
        a.created_at.slice(0, 19),
    );
    option.value = a.id;
    $("versions").append(option);
  }
  resetEditor();
  selection = null;
  const orig = artifact(paper.original_id);
  $("paper-title").textContent = paper.source.replace(/\.pdf$/i, "");
  $("paper-meta").textContent = `${orig.metadata.page_count} 页 · 原件已保留`;
  notify(
    "原件已按哈希保留。语言检测：" +
      orig.metadata.language +
      (orig.metadata.warning ? "；" + orig.metadata.warning : ""),
  );
  await showArtifact(paper.original_id);
  await refreshNotes();
  await pollJobs();
  await refreshSelectionTranslation();
}

async function refreshSelectionTranslation() {
  const s = await api("selection-translation");
  $("selection-budget").textContent = `${s.model || "模型未配置"} · 今日请求 ${s.requests_today}/${s.daily_request_limit} · 用量/预留 ${s.accounted_tokens_today}/${s.daily_token_limit} token。一次最多 ${s.max_characters} 字符。${s.budget_note}`;
  if (!paper) return;
  const id = paper.id;
  const history = await api("selection-history?paper_id=" + id);
  if (paper.id !== id) return;
  $("translation-history").replaceChildren(...history.map((item) => action(
    item.source_text.slice(0, 60) + "…", async () => {
      await gotoAnchor(item.anchor);
      renderSelectionTranslation({ ...item, cached: true });
    },
  )));
}

function renderSelectionTranslation(result) {
  const out = $("selection-translation");
  out.replaceChildren(el("h4", "原文选区"), el("p", result.source_text),
    el("h4", "机器译文 · 待核对"), el("p", result.result),
    el("p", `${result.cached ? "本地缓存，本次未调用模型" : "本次一次模型请求"} · ${result.profile.model} · ${result.usage_status === "reported" ? "生成时服务商返回 " + result.usage.total_tokens + " token" : "服务商未返回用量，保留预算预留"}`, "hint"));
  out.append(action("返回原文选区", () => gotoAnchor(result.anchor)),
    action("保存为待确认草稿", async () => {
      if (dirty) throw Error("请先保存当前笔记，避免覆盖未保存输入。");
      const note = await api("selection-translation-draft/" + result.id, {
        paper_id: result.paper_id, anchor: result.anchor,
      });
      await openNote(note);
      await refreshNotes();
      notify("译文已保存为 AI 来源草稿，确认前不参与笔记检索；原文来源保持独立。");
    }));
}

$("translate-selection").onclick = () => guard(async () => {
  if (!paper || !selection) throw Error("请先在原文中选择一句话里的任意文字，系统会扩展为完整句。");
  const requested = structuredClone(selection), id = paper.id, epoch = ++translationEpoch;
  $("translate-selection").disabled = true;
  $("selection-translation").textContent = "正在查询缓存或翻译这一选区…";
  try {
    const result = await api("selection-translation", { paper_id: id, anchor: requested, allow_external: true });
    if (paper.id === id && epoch === translationEpoch) renderSelectionTranslation(result);
  } catch (error) {
    if (paper.id === id && epoch === translationEpoch) $("selection-translation").textContent = error.message;
    throw error;
  } finally {
    $("translate-selection").disabled = false;
    await refreshSelectionTranslation();
  }
});
async function pollJobs() {
  if (!paper) return;
  const requestedPaper = paper.id;
  const list = await api("jobs?paper_id=" + requestedPaper, undefined, 15000);
  if (paper.id !== requestedPaper) return;
  $("jobs").replaceChildren();
  const past = el("details", undefined, "past-jobs");
  past.append(el("summary", `历史翻译 · ${Math.max(0, list.length - 1)} 次`));
  const prominent = list.find((j) => ["running", "queued"].includes(j.state)) || [...list].sort((a,b) => b.updated_at.localeCompare(a.updated_at))[0];
  for (const j of list) {
    const row = el("div", undefined, "job-card " + j.state);
    const names = {queued: "排队中", running: "翻译中", succeeded: "译文已生成", failed: "已停止"};
    row.append(el("span", names[j.state] || j.state, "job-state"), el("strong", j.stage));
    if (j.state === "running" || j.state === "queued" || j.state === "succeeded") {
      const progress = el("progress"); progress.max = 100;
      if (j.state === "succeeded") progress.value = 100;
      else if (Number.isFinite(j.progress)) progress.value = Math.min(99, Math.max(0,j.progress));
      progress.setAttribute("aria-label", "全文翻译进度"); row.append(progress);
      const label = j.state === "succeeded" ? "100% · 译文已生成" : j.state === "queued" ? "已进入队列，等待引擎启动" : Number.isFinite(j.progress) ? `${progress.value.toFixed(1)}% · 引擎报告的总体进度` : "正在处理当前阶段，暂未返回百分比";
      row.append(el("p", label, "progress-label"));
    }
    if (j.state === "running" || j.state === "queued") {
      const started = j.budget?.attempts.find(a => a.attempt === j.attempt)?.started_at;
      if (started) row.append(el("p", `已运行 ${Math.max(0,Math.floor((Date.now()-Date.parse(started))/1000))} 秒 · 每 2 秒刷新状态`, "hint"));
      row.append(action("停止任务", async () => { await api("cancel/" + j.id, {}); await pollJobs(); }));
    }
    if (j.error) row.append(el("p", j.error));
    if (j.validation?.reference_pages_preserved?.length)
      row.append(el("p", `第 ${j.validation.reference_pages_preserved.join("、")} 页为参考文献，原样保留英文及原件版式。`, "hint"));
    const b = j.budget;
    const current = b?.attempts.find((a) => a.attempt === j.attempt);
    row.append(el("p", current ? `本次执行：${current.reported_tokens} token（输入 ${current.prompt_tokens_reported} / 输出 ${current.completion_tokens_reported}）· ${current.requests} 次请求${current.unknown_requests ? ` · ${current.unknown_requests} 次用量未知，总数不完整` : ""}` : "本次尚无逐请求用量记录。", "hint"));
    row.append(el("p", b ? `含历史重试累计：${b.reported_tokens} token · ${b.requests} 次请求${b.unknown_requests ? ` · ${b.unknown_requests} 次用量未知` : ""}` : "历史任务无逐请求账本，用量未知。", "hint"));
    if (b?.attempts.length) {
      const history = el("details"); history.append(el("summary", "执行历史（跨重试保留）"));
      b.attempts.forEach((a) => history.append(el("p", `第 ${a.attempt} 次 · ${a.state} · ${a.reported_tokens} token · ${a.requests} 次请求${a.unknown_requests ? ` · ${a.unknown_requests} 次用量未知` : ""} · ${a.started_at.slice(0, 19)}`)));
      row.append(history);
    }
    if (j.state === "failed")
      row.append(
        action("重试", async () => {
          await translate(false, j.id);
        }),
      );
    if (j.artifact_id) {
      row.append(
        action("打开译文", async () => {
          const data = await api("papers");
          papers = data.papers;
          paper = papers.find((p) => p.id === paper.id);
          if (
            !$("versions").querySelector(
              'option[value="' + j.artifact_id + '"]',
            )
          ) {
            const o = el("option", "中文译文 · " + j.updated_at.slice(0, 19));
            o.value = j.artifact_id;
            $("versions").append(o);
          }
          await showArtifact(j.artifact_id);
        }),
      );
    }
    if (j === prominent) $("jobs").append(row);
    else past.append(row);
  }
  if (list.length > 1) $("jobs").append(past);
  if (!submittingTranslation) translationFeedback("");
}
async function status() {
  const s = await api("settings");
  $("service").textContent = s.configured
    ? "服务已配置：" +
      s.model +
      "；引擎 pdf2zh-next " +
      s.versions["pdf2zh-next"]
    : "翻译服务未配置或未安装，原文阅读与手动笔记不受影响。";
  $("auto").checked = s.auto_translate;
}

$("highlight").onclick = () =>
  guard(async () => {
    if (!selection) throw Error("请先选择文字。");
    const a = await api("annotations", {
      paper_id: paper.id,
      anchor: selection,
      style: "highlight",
    });
    annotations.push(a);
    panes.forEach((p) => p.paint());
    notify("高亮已保存，无需填写笔记。");
  });
$("underline").onclick = () =>
  guard(async () => {
    if (!selection) throw Error("请先选择文字。");
    const a = await api("annotations", {
      paper_id: paper.id,
      anchor: selection,
      style: "underline",
    });
    annotations.push(a);
    panes.forEach((p) => p.paint());
    notify("下划线已保存。");
  });
$("attach").onclick = () =>
  guard(() => {
    if (!selection) throw Error("请先选择文字。");
    if (historyView) throw Error("历史只读，请打开最新笔记。");
    anchors.push(structuredClone(selection));
    dirty = true;
    renderAnchors();
  });
$("save").onclick = () =>
  guard(async () => {
    if (!editing && !anchors.length && selection)
      anchors.push(structuredClone(selection));
    await save();
  });
$("archive").onclick = () =>
  guard(async () => {
    if (!editing) throw Error("先打开需要归档的笔记。");
    await save({ archived: true });
  });
$("confirm").onclick = () =>
  guard(async () => {
    if (!editing || editing.confirmed)
      throw Error("请先打开未确认的 AI 草稿。");
    await save({ confirm: true });
  });
$("new").onclick = () => {
  if (!dirty || confirm("放弃尚未保存的输入？")) resetEditor();
};
$("body").oninput = $("type").onchange = () => {
  dirty = true;
};
$("draft").onclick = () =>
  guard(async () => {
    if (!selection && !anchors.length) throw Error("请先选区。");
    if (dirty && !confirm("保留当前输入请先保存。继续生成并打开新 AI 草稿？"))
      return;
    const n = await api("draft", {
      paper_id: paper.id,
      body: $("body").value,
      type: $("type").value,
      anchors: anchors.length ? anchors : [selection],
      allow_external: $("external").checked,
    });
    dirty = false;
    await openNote(n);
    await refreshNotes();
    notify("AI 草稿已单独保存，未经你确认不会进入记忆检索。");
  });
$("versions").onchange = () => guard(() => showArtifact($("versions").value));
$("papers").onchange = () =>
  guard(async () => {
    if (dirty && !confirm("放弃未保存输入并切换论文？")) {
      $("papers").value = paper.id;
      return;
    }
    await choosePaper($("papers").value);
  });
$("compare").onclick = () =>
  guard(async () => {
    clearSelectionSync();
    const current = artifact($("versions").value);
    const translated = current?.kind === "translation" ? current : paper.artifacts
      .filter((a) => a.kind === "translation")
      .at(-1);
    if (!translated) throw Error("还没有中文译文，请先翻译。");
    const page = panes[0]?.page || 1;
    panes = [new Pane(artifact(paper.original_id)), new Pane(translated)];
    $("panes").replaceChildren(...panes.map((p) => p.node));
    const canSync = syncControls();
    await Promise.all(panes.map((p) => p.render(Math.min(page, p.artifact.metadata.page_count))));
    notify(canSync ? "中英对照已打开，可同步翻页；同页码不代表段落精确对应。" : "原译文页数不同，已禁用同步翻页，请分别定位。");
  });
async function translate(force, retryId = null) {
  if (!paper) throw Error("请先打开论文。");
  if (submittingTranslation) return;
  submittingTranslation = true;
  const requestedPaper = paper.id;
  const dialog = $("translation-dialog");
  $("translation-dialog-text").textContent = force ? "生成新的中文译文版本，旧译文和笔记保留。" : "已有译文将直接复用；已有失败任务将重新尝试，保留缓存和历史用量。";
  $("translate").disabled = $("retranslate").disabled = true;
  try {
    const approved = await new Promise(resolve => {
      dialog.addEventListener("close", () => resolve(dialog.returnValue === "start"), {once:true});
      dialog.returnValue = "cancel"; dialog.showModal();
    });
    if (!approved) { translationFeedback("已取消，未提交翻译请求。"); return; }
    if (paper.id !== requestedPaper) throw Error("论文已切换，请重新发起翻译。");
    $("translate").textContent = "正在提交…";
    translationFeedback("正在提交翻译任务，请稍候…", false, true);
    let job;
    if (retryId) job = await api("retry/" + retryId, {}, 15000);
    else {
      const result = await api("jobs", {paper_id: requestedPaper, manual:true, force}, 15000);
      job = result.job;
      if (!result.created && job.state === "failed") {
        translationFeedback("找到之前的失败任务，正在重新排队…", false, true);
        job = await api("retry/" + job.id, {}, 15000);
      }
    }
    if (paper.id === requestedPaper) {
      await pollJobs();
      translationFeedback(job.state === "succeeded" ? "已有译文，无需重复翻译。点击下方“打开译文”。" : "任务已提交。下方显示当前阶段与进度，可随时停止。");
      $("jobs").scrollIntoView({block:"nearest", behavior:"smooth"});
    }
  } catch(e) {
    const timedOut = ["TimeoutError", "AbortError"].includes(e.name);
    translationFeedback(timedOut ? "提交或状态查询超时，结果暂未确认；任务可能已启动，请等待下方状态更新，勿连续点击。" : `翻译请求失败：${e.message}`, true);
  } finally {
    submittingTranslation = false;
    $("translate").disabled = $("retranslate").disabled = false;
    $("translate").textContent = "翻译全文";
  }
}
$("translate").onclick = () => guard(() => translate(false));
$("retranslate").onclick = () => guard(() => translate(true));
$("auto").onchange = () =>
  guard(() => api("settings", { auto_translate: $("auto").checked }));
$("reload").onclick = () =>
  guard(async () => {
    if (dirty) throw Error("请先保存当前笔记，再刷新资料。");
    await loadPapers();
  });
$("showArchived").onchange = () => guard(refreshNotes);
$("upload").onchange = () =>
  guard(async () => {
    const file = $("upload").files[0];
    if (!file) return;
    const form = new FormData();
    form.append("file", file);
    const r = await fetch("/api/library", { method: "POST", body: form }),
      v = await r.json();
    if (!r.ok) throw Error(v.error);
    await loadPapers(v.paper_id);
    if (v.translation_warning) notify(v.translation_warning, true);
  });
$("ask").onclick = () =>
  guard(async () => {
    if (!paper) throw Error("请先打开论文。");
    $("ask").disabled = true;
    $("answer").textContent = "正在检索…";
    try {
      const r = await api("ask", {
        question: $("query").value,
        paper_id: $("allPapers").checked ? null : paper.id,
        scope: $("scope").value,
        backend: $("backend").value,
        allow_external: $("external").checked,
      });
      const out = $("answer");
      out.replaceChildren(el("h4", "原文依据"), el("div", r.original_answer));
      for (const c of r.original_citations) {
        const card = el("div", undefined, "citation"),
          link = el("a", c.source + " · p." + c.page);
        link.href = c.url;
        link.target = "_blank";
        card.append(link, el("p", c.quote));
        out.append(card);
      }
      out.append(el("h4", "你的记录"), el("div", r.notes_answer));
      for (const n of r.note_citations) {
        const card = el("div", undefined, "citation"),
          link = el("a", "笔记 " + n.id + " · revision " + n.revision);
        link.href = n.url;
        link.target = "_blank";
        card.append(link, el("p", n.body), el("span", noteName(n), "tag"));
        out.append(card);
      }
      if (r.conflict)
        out.append(el("h4", "差异 / 待核实"), el("div", r.conflict));
      for (const w of r.warnings) out.append(el("p", w, "hint"));
    } finally {
      $("ask").disabled = false;
    }
  });
window.addEventListener("beforeunload", (e) => {
  if (dirty) {
    e.preventDefault();
    e.returnValue = "";
  }
});
await guard(async () => {
  try {
    pdfjs = await import("/reader-assets/build/pdf.mjs");
    pdfjs.GlobalWorkerOptions.workerSrc = "/reader-assets/build/pdf.worker.mjs";
  } catch {
    throw Error(
      "PDF 阅读组件未安装，请运行 scripts/install_reader.ps1 后刷新。",
    );
  }
  await status();
  const q = new URLSearchParams(location.search);
  let id = q.get("paper");
  if (q.get("source")) {
    const p = await api("papers", { source: q.get("source") });
    id = p.id;
  }
  await loadPapers(id);
  if (!paper) return;
  if (q.get("artifact"))
    await showArtifact(q.get("artifact"), Number(q.get("page") || 1));
  else if (q.get("page")) await panes[0].render(Number(q.get("page")));
  if (q.get("note"))
    await openNote(
      await api(
        "notes/" +
          q.get("note") +
          (q.get("revision") ? "?revision=" + q.get("revision") : ""),
      ),
      false,
    );
  const staged = sessionStorage.getItem("reading-evidence");
  if (staged) {
    const v = JSON.parse(staged);
    if (v.source === paper.source) {
      const a = artifact(paper.original_id),
        page = Number(v.page);
      if (page >= 1 && page <= a.metadata.page_count) {
        anchors = [
          {
            artifact_id: a.id,
            artifact_hash: a.hash,
            page,
            coordinate_system: "pdf_user_space",
            view_box: a.metadata.pages[page - 1].view_box,
            rects: [],
            excerpt: v.quote,
            evidence_id: v.evidence_id || null,
          },
        ];
        renderAnchors();
        await panes[0].render(page);
        notify(
          "已带入证据摘录（仅页级，未核验解析坐标）。请写下你的理解后保存。",
        );
      }
      sessionStorage.removeItem("reading-evidence");
    }
  }
});
setInterval(() => {
  if (document.visibilityState !== "visible" || pollingJobs || submittingTranslation) return;
  pollingJobs = true;
  pollJobs().catch(() => translationFeedback("暂时无法更新进度，保留上次状态；正在尝试重新连接。", true))
    .finally(() => { pollingJobs = false; });
}, 2000);
