/* Uses Zotero public item/collection APIs; no translation-plugin internals. */
var ResearchBridge = {
  id: "research-assistant@local",
  busy: false,
  windows: new Set(),
  prefix: "extensions.researchAssistant.",
  rootURI: "",
  workbench: null,
  engineProcess: null,
  engineStarting: null,

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
  async response(path, body, method = "POST") {
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
      if (!response.ok) {
        let data = {};
        try { data = await response.json(); } catch (_) {}
        throw new Error(data.error || "知识库请求失败");
      }
      return response;
    } finally {
      window.clearTimeout(timer);
    }
  },
  async request(path, body, method = "POST") {
    return (await this.response(path, body, method)).json();
  },
  async start(rootURI) {
    this.rootURI = rootURI;
    if (!this.pref("connection")) this.setPref("connection", Zotero.Utilities.randomString(24));
    this.readerHandler = ({ doc, params, append }) => {
      if (!params.annotation?.text) return;
      const button = doc.createElement("button");
      button.textContent = "在科研知识库中检索";
      button.addEventListener("click", () => this.open(params.annotation.text));
      append(button);
    };
    Zotero.Reader.registerEventListener("renderTextSelectionPopup", this.readerHandler, this.id);
    this.ensureBackend().catch(error => Zotero.debug("Research Assistant backend: " + error));
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
    if (this.workbench && !this.workbench.closed) this.workbench.close();
    this.workbench = null;
    if (this.engineProcess && this.pref("stopEngineOnExit", true)) {
      this.engineProcess.kill(1000).catch(error => Zotero.logError(error));
    }
    this.engineProcess = null;
  },
  async configure(window = Zotero.getMainWindow()) {
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
  async configureEngine(window = Zotero.getMainWindow()) {
    const executable = window.prompt(
      "Python 解释器或已打包科研引擎的完整路径",
      this.pref("engineExecutable")
    );
    if (executable === null) return false;
    const script = window.prompt(
      "Python 启动脚本的完整路径；如果上一步选择的是独立 EXE，请留空",
      this.pref("engineScript")
    );
    if (script === null) return false;
    const workdir = window.prompt(
      "科研助手工作目录",
      this.pref("engineWorkdir")
    );
    if (workdir === null) return false;
    this.setPref("engineExecutable", executable.trim());
    this.setPref("engineScript", script.trim());
    this.setPref("engineWorkdir", workdir.trim());
    this.setPref("autoStartEngine", window.confirm("以后启动 Zotero 时自动启动该 Python 引擎？"));
    this.setPref("stopEngineOnExit", window.confirm("退出 Zotero 时停止由插件启动的引擎？"));
    return true;
  },
  async backendStatus() {
    try {
      return { connected: true, ...(await this.request("status", undefined, "GET")) };
    } catch (error) {
      return { connected: false, error: error.message };
    }
  },
  async ensureBackend(force = false) {
    const status = await this.backendStatus();
    if (status.connected) return status;
    if (!force && !this.pref("autoStartEngine", false)) return status;
    return this.startEngine();
  },
  async startEngine() {
    if (this.engineStarting) return this.engineStarting;
    this.engineStarting = (async () => {
      const executable = this.pref("engineExecutable").trim();
      const script = this.pref("engineScript").trim();
      const workdir = this.pref("engineWorkdir").trim();
      if (!executable) throw new Error("尚未配置 Python 解释器或科研引擎。");
      const arguments_ = script ? [script] : [];
      this.engineProcess = await Subprocess.call({
        command: executable,
        arguments: arguments_,
        workdir: workdir || null,
        stderr: "pipe",
      });
      for (let attempt = 0; attempt < 30; attempt++) {
        await new Promise(resolve => Zotero.getMainWindow().setTimeout(resolve, 1000));
        const status = await this.backendStatus();
        if (status.connected) return status;
        if (this.engineProcess.exitCode !== null) {
          throw new Error("Python 科研引擎已退出，未能建立连接。");
        }
      }
      throw new Error("Python 科研引擎启动超时。");
    })();
    try {
      return await this.engineStarting;
    } finally {
      this.engineStarting = null;
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
  async sync(window = Zotero.getMainWindow()) {
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
    if (this.workbench && !this.workbench.closed) {
      this.workbench.focus();
      this.workbench.ResearchAssistantWorkbench?.setQuery(query);
      return this.workbench;
    }
    const args = { bridge: this, query: query.slice(0, 2000) };
    this.workbench = Zotero.getMainWindow().openDialog(
      "chrome://researchassistant/content/assistant.xhtml",
      "research-assistant-workbench",
      "chrome,centerscreen,resizable",
      args
    );
    return this.workbench;
  },
  openSource(url) {
    if (typeof url !== "string" || !url.startsWith("zotero://")) {
      throw new Error("只允许打开 Zotero 内部来源链接。");
    }
    Zotero.launchURL(url);
  },
  async exportBackup(window = Zotero.getMainWindow()) {
    const response = await this.response("export", undefined, "GET");
    const bytes = new Uint8Array(await response.arrayBuffer());
    const { FilePicker } = ChromeUtils.importESModule("chrome://zotero/content/modules/filePicker.mjs");
    const picker = new FilePicker();
    picker.init(window, "保存科研知识库备份", picker.modeSave);
    picker.defaultString = "research-knowledge-backup.zip";
    picker.defaultExtension = "zip";
    picker.appendFilter("ZIP 备份", "*.zip");
    const result = await picker.show();
    if (result === picker.returnCancel) return false;
    await IOUtils.write(picker.file, bytes);
    return true;
  },
  async writeNotes(window = Zotero.getMainWindow()) {
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
