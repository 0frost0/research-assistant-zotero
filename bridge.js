/* Uses Zotero public item/collection APIs; no translation-plugin internals. */
var ResearchBridge = {
  id: "research-assistant@local",
  busy: false,
  windows: new Set(),
  prefix: "extensions.researchAssistant.",

  pref(name, fallback = "") {
    return Zotero.Prefs.get(this.prefix + name, true) ?? fallback;
  },
  setPref(name, value) {
    Zotero.Prefs.set(this.prefix + name, value, true);
  },
  endpoint() {
    const url = new URL(this.pref("endpoint", "http://127.0.0.1:8765"));
    if (url.username || url.password || url.search || url.hash ||
        (url.protocol !== "https:" && !(url.protocol === "http:" && ["127.0.0.1", "localhost", "[::1]"].includes(url.hostname)))) {
      throw new Error("连接地址只允许 HTTPS 或本机 HTTP，且不能包含凭据或查询参数。");
    }
    return url.href.replace(/\/$/, "");
  },
  async request(path, body, method = "POST") {
    const window = Zotero.getMainWindow();
    const controller = new window.AbortController();
    const timer = window.setTimeout(() => controller.abort(), 90000);
    try {
      const binary = ArrayBuffer.isView(body);
      const response = await window.fetch(this.endpoint() + "/api/knowledge/" + path, {
        method, headers: {
          Authorization: "Bearer " + this.pref("token"),
          "Content-Type": binary ? "application/pdf" : "application/json",
        },
        body: body === undefined ? undefined : binary ? body : JSON.stringify(body),
        signal: controller.signal, credentials: "omit", redirect: "error",
      });
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || "知识库请求失败");
      return data;
    } finally {
      window.clearTimeout(timer);
    }
  },
  async start() {
    if (!this.pref("connection")) this.setPref("connection", Zotero.Utilities.randomString(24));
    this.readerHandler = ({ doc, params, append }) => {
      if (!params.annotation?.text) return;
      const button = doc.createElement("button");
      button.textContent = "在科研知识库中检索";
      button.addEventListener("click", () => this.open(params.annotation.text));
      append(button);
    };
    Zotero.Reader.registerEventListener("renderTextSelectionPopup", this.readerHandler, this.id);
  },
  loadWindow(window) {
    if (window.document.getElementById("research-assistant-menu")) return;
    const doc = window.document;
    const menu = doc.createXULElement("menu");
    menu.id = "research-assistant-menu";
    menu.setAttribute("label", "科研知识库");
    const popup = doc.createXULElement("menupopup");
    for (const [label, action] of [
      ["配置后端连接", () => this.configure(window)],
      ["同步当前分类", () => this.sync(window)],
      ["打开知识库", () => this.open()],
      ["将知识库笔记新增到 Zotero", () => this.writeNotes(window)],
    ]) {
      const item = doc.createXULElement("menuitem");
      item.setAttribute("label", label);
      item.addEventListener("command", () => Promise.resolve().then(action).catch(() =>
        window.alert("操作未完成。请检查连接、令牌、所选分类和附件是否已下载；已有笔记未被覆盖。")));
      popup.append(item);
    }
    menu.append(popup);
    doc.getElementById("menu_ToolsPopup").append(menu);
    this.windows.add(window);
  },
  unloadWindow(window) {
    window.document.getElementById("research-assistant-menu")?.remove();
    this.windows.delete(window);
  },
  stop() {
    Zotero.Reader.unregisterEventListener("renderTextSelectionPopup", this.readerHandler);
    for (const window of [...this.windows]) this.unloadWindow(window);
  },
  async configure(window) {
    const endpoint = window.prompt("科研助手后端地址", this.pref("endpoint", "http://127.0.0.1:8765"));
    if (endpoint === null) return;
    const secret = { value: "" };
    const accepted = Services.prompt.promptPassword(window, "科研助手连接", "连接令牌", secret, null, {});
    if (!accepted || !secret.value) return;
    const old = { endpoint: this.pref("endpoint"), token: this.pref("token") };
    this.setPref("endpoint", endpoint);
    this.setPref("token", secret.value.trim());
    try {
      this.endpoint();
      const status = await this.request("status", undefined, "GET");
      if (status.protocol !== 1) throw new Error("协议版本不兼容");
      window.alert("连接成功。");
    } catch (error) {
      this.setPref("endpoint", old.endpoint);
      this.setPref("token", old.token);
      throw error;
    }
  },
  libraryKey(libraryID) {
    const library = Zotero.Libraries.get(libraryID);
    return library.libraryType === "group" ? "group:" + Zotero.Groups.getGroupIDFromLibraryID(libraryID) : "user:0";
  },
  async collect(collection, subtree) {
    const items = new Map();
    const visit = async col => {
      for (const item of col.getChildItems()) items.set(item.id, item);
      if (subtree) for (const child of col.getChildCollections()) await visit(child);
    };
    await visit(collection);
    const entities = new Map();
    const documents = new Map();
    const window = Zotero.getMainWindow();
    const plain = html => {
      const doc = new window.DOMParser().parseFromString(html, "text/html");
      for (const node of doc.querySelectorAll("script,style")) node.remove();
      for (const node of doc.querySelectorAll("p,div,li,br")) node.append(doc.createTextNode("\n"));
      return doc.body.textContent.trim();
    };
    const note = item => {
      entities.set(item.key, { key: item.key, kind: "note", title: item.getNoteTitle(),
        parent_key: item.parentItemKey || null, body: plain(item.getNote()),
        confirmed: !item.getTags().some(t => t.tag === "research-assistant/draft") });
    };
    const attachment = async item => {
      if (entities.has(item.key) || item.attachmentContentType !== "application/pdf") return;
      const path = await item.getFilePathAsync();
      if (!path) throw new Error("附件未下载，未提交分类快照");
      const content = await IOUtils.read(path);
      if (content.length > 25 * 1024 * 1024) throw new Error("附件超过 25 MB");
      const hash = [...new Uint8Array(await window.crypto.subtle.digest("SHA-256", content))]
        .map(v => v.toString(16).padStart(2, "0")).join("");
      documents.set(hash, content);
      const parent = item.parentID ? await Zotero.Items.getAsync(item.parentID) : null;
      const title = parent?.getField("title") || item.getField("title");
      entities.set(item.key, { key: item.key, kind: "document", title,
        parent_key: item.parentItemKey || null, hash });
      for (const annotation of item.getAnnotations()) {
        const position = JSON.parse(annotation.annotationPosition || "{}");
        if (!Number.isInteger(position.pageIndex)) continue;
        entities.set(annotation.key, { key: annotation.key, kind: "annotation",
          title, parent_key: item.parentItemKey || null,
          attachment_key: item.key, page: position.pageIndex + 1,
          body: annotation.annotationText || "", comment: annotation.annotationComment || "", confirmed: true });
      }
    };
    for (const item of items.values()) {
      if (item.isNote()) note(item);
      else if (item.isAttachment()) await attachment(item);
      else if (item.isRegularItem()) {
        for (const id of item.getAttachments()) await attachment(await Zotero.Items.getAsync(id));
        for (const id of item.getNotes()) note(await Zotero.Items.getAsync(id));
      }
    }
    return { entities: [...entities.values()].sort((a, b) => a.key.localeCompare(b.key)), documents };
  },
  async sync(window) {
    if (this.busy) return window.alert("同步正在进行。");
    const collection = Zotero.getActiveZoteroPane().getSelectedCollection();
    if (!collection) return window.alert("请先选择一个分类，不是整个资料库。");
    const subtree = window.confirm("是否包含当前分类的所有子分类？\n“取消”表示仅当前分类。");
    if (!window.confirm(`允许把“${collection.name}”${subtree ? "及子分类" : ""}中的 PDF、批注和笔记发送至 ${this.endpoint()}？\n不会自动翻译全文或调用生成模型。`)) return;
    this.busy = true;
    try {
      const first = await this.collect(collection, subtree);
      const base = await this.request("bases", { connection: this.pref("connection"),
        library: this.libraryKey(collection.libraryID), collection: collection.key,
        name: collection.name, include_children: subtree });
      for (const [hash, bytes] of first.documents) {
        const state = await this.request("objects/" + hash, undefined, "GET");
        if (!state.exists) await this.request("objects/" + hash, bytes, "PUT");
      }
      const second = await this.collect(collection, subtree);
      if (JSON.stringify(first.entities) !== JSON.stringify(second.entities) || collection.deleted) {
        throw new Error("同步期间资料发生变化，未替换知识库成员");
      }
      const result = await this.request("sync/" + base.id, {
        name: collection.name, include_children: subtree, expected_revision: base.revision,
        entities: first.entities, complete: true, allow_upload: true,
      });
      this.setPref("lastBase", result.id);
      window.alert(`同步完成：${first.entities.length} 个文献、批注或笔记条目。`);
      this.open();
    } finally {
      this.busy = false;
    }
  },
  open(query = "") {
    const url = new URL(this.endpoint() + "/knowledge");
    if (this.pref("lastBase")) url.searchParams.set("kb", this.pref("lastBase"));
    if (query) url.searchParams.set("q", query.slice(0, 2000));
    // Fragment is not sent to the HTTP server; the page removes it immediately.
    url.hash = "token=" + encodeURIComponent(this.pref("token"));
    Zotero.launchURL(url.href);
  },
  async writeNotes(window) {
    const id = this.pref("lastBase");
    if (!id) return window.alert("请先同步一个分类。");
    const snapshot = await this.request("bases/" + id, undefined, "GET");
    const base = snapshot.bases[0];
    if (base.connection !== this.pref("connection")) throw new Error("不是此 Zotero 配置创建的知识库");
    const libraries = Zotero.Libraries.getAll();
    const library = libraries.find(l => this.libraryKey(l.libraryID) === base.library);
    if (!library || !library.editable) throw new Error("资料库不可写");
    const collection = Zotero.Collections.getByLibraryAndKey(library.libraryID, base.collection);
    if (!collection) throw new Error("原分类不存在");
    const notes = snapshot.notes.filter(n => n.data.confirmed && !n.data.archived);
    if (!window.confirm(`将 ${notes.length} 条已确认的专题笔记作为独立笔记新增到“${collection.name}”？\n相同修订不会重复新增，不覆盖现有笔记。`)) return;
    for (const note of notes) {
      const marker = `research-assistant/${note.id}/${note.revision}`;
      const search = new Zotero.Search();
      search.libraryID = library.libraryID;
      search.addCondition("tag", "is", marker);
      if ((await search.search()).length) continue;
      const item = new Zotero.Item("note");
      item.libraryID = library.libraryID;
      const escape = value => value.replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[c]);
      item.setNote("<h1>" + escape(note.data.title) + "</h1><p>" +
                   escape(note.data.body).replace(/\n/g, "<br>") + "</p>");
      item.setCollections([collection.id]);
      item.addTag(marker, 1);
      await item.saveTx();
    }
    window.alert("笔记写回完成。");
  },
};
