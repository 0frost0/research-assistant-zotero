const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

test("manifest includes Zotero 9 required update URL", () => {
  const manifest = JSON.parse(
    fs.readFileSync(path.join(__dirname, "../deployment/zotero/manifest.json"), "utf8")
  );
  assert.match(manifest.applications.zotero.update_url, /^https:\/\//);
});

function fixture() {
  const prefs = new Map(), events = [];
  const window = { URL, AbortController, setTimeout, clearTimeout, crypto: require("node:crypto").webcrypto,
    alert() {}, confirm: () => true };
  window.openDialog = (url, name, features, args) => {
    const dialog = {
      closed: false,
      focus: () => events.push("focus"),
      close() { this.closed = true; },
      ResearchAssistantWorkbench: { setQuery: query => events.push(["query", query]) },
    };
    events.push({ url, name, features, args, dialog });
    return dialog;
  };
  const Zotero = {
    Prefs: { get: key => prefs.get(key), set: (key, value) => prefs.set(key, value) },
    Utilities: { randomString: () => "fixture-connection" },
    Reader: { registerEventListener: (...args) => events.push(args), unregisterEventListener: (...args) => events.push(args) },
    getMainWindow: () => window, launchURL: url => events.push(["launch", url]),
    debug() {}, logError() {},
  };
  const Subprocess = { call: async options => ({ options, exitCode: null, kill: async () => ({ exitCode: -9 }) }) };
  const scope = { Zotero, Services: {}, ChromeUtils: {}, Subprocess, IOUtils: {}, URL, Uint8Array, console };
  vm.createContext(scope);
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../deployment/zotero/bridge.js"), "utf8"), scope);
  return { bridge: scope.ResearchBridge, Zotero, window, prefs, events, Subprocess };
}

test("refuses remote plaintext, credentials, query and fragments in endpoint", () => {
  const { bridge } = fixture();
  for (const endpoint of ["http://server.invalid", "https://secret@server.invalid", "https://x.invalid/?secret=1", "https://x.invalid/#key"]) {
    bridge.setPref("endpoint", endpoint);
    assert.throws(() => bridge.endpoint());
  }
  bridge.setPref("endpoint", "http://127.0.0.1:8765");
  assert.equal(bridge.endpoint(), "http://127.0.0.1:8765");
});

test("request has auth, timeout and no redirects; no implicit retry", async () => {
  const { bridge, window } = fixture();
  bridge.setPref("token", "synthetic-token");
  let calls = 0;
  window.fetch = async (url, options) => {
    calls++;
    assert.equal(options.headers.Authorization, "Bearer synthetic-token");
    assert.equal(options.redirect, "error");
    assert.ok(options.signal);
    return { ok: false, json: async () => ({ error: "synthetic failure" }) };
  };
  await assert.rejects(bridge.request("status", undefined, "GET"));
  assert.equal(calls, 1);
});

test("reader handler is cleaned up; workbench opens inside Zotero and is reused", async () => {
  const { bridge, events } = fixture();
  await bridge.start("file:///fixture/");
  bridge.open("question");
  const opened = events.find(value => value?.url);
  assert.equal(opened.url, "chrome://researchassistant/content/assistant.xhtml");
  assert.equal(opened.args.query, "question");
  bridge.open("second");
  assert.ok(events.includes("focus"));
  assert.deepEqual(events.find(value => Array.isArray(value) && value[0] === "query"), ["query", "second"]);
  assert.equal(events.some(value => Array.isArray(value) && value[0] === "launch"), false);
  bridge.stop();
  assert.equal(opened.dialog.closed, true);
  assert.equal(events.filter(value => Array.isArray(value) && typeof value[0] === "string").length >= 3, true);
});

test("only Zotero source URLs can be opened", () => {
  const { bridge, events } = fixture();
  bridge.openSource("zotero://open-pdf/library/items/ABC?page=2");
  assert.deepEqual(events.pop(), ["launch", "zotero://open-pdf/library/items/ABC?page=2"]);
  assert.throws(() => bridge.openSource("https://example.invalid/"));
});

test("configured Python engine is launched and polled", async () => {
  const { bridge, prefs, Subprocess, window } = fixture();
  prefs.set("extensions.researchAssistant.engineExecutable", "C:\\Python\\python.exe");
  prefs.set("extensions.researchAssistant.engineScript", "D:\\assistant\\web_app.py");
  prefs.set("extensions.researchAssistant.engineWorkdir", "D:\\assistant");
  window.setTimeout = callback => { callback(); return 1; };
  let calls = 0, launched;
  bridge.backendStatus = async () => ({ connected: ++calls > 1, backend: "tfidf" });
  Subprocess.call = async options => {
    launched = options;
    return { exitCode: null, kill: async () => ({ exitCode: -9 }) };
  };
  const status = await bridge.startEngine();
  assert.equal(status.connected, true);
  assert.equal(launched.command, "C:\\Python\\python.exe");
  assert.deepEqual(Array.from(launched.arguments), ["D:\\assistant\\web_app.py"]);
  assert.equal(launched.workdir, "D:\\assistant");
});

test("PDF bytes from another realm are sent as binary, not JSON", async () => {
  const { bridge, window } = fixture();
  const bytes = vm.runInNewContext("new Uint8Array([37, 80, 68, 70])");
  window.fetch = async (url, options) => {
    assert.equal(options.headers["Content-Type"], "application/pdf");
    assert.equal(options.body, bytes);
    return { ok: true, json: async () => ({ ok: true }) };
  };
  await bridge.request("objects/hash", bytes, "PUT");
});

test("declining upload sends no requests", async () => {
  const { bridge, Zotero, window } = fixture();
  Zotero.getActiveZoteroPane = () => ({ getSelectedCollection: () => ({ name: "Fixture" }) });
  window.confirm = () => false;
  let calls = 0;
  bridge.request = async () => { calls++; };
  await bridge.sync(window);
  assert.equal(calls, 0);
});

test("failed object upload never commits replacement membership", async () => {
  const { bridge, Zotero, window } = fixture();
  Zotero.getActiveZoteroPane = () => ({ getSelectedCollection: () => ({ name: "Fixture", key: "COL00001", libraryID: 1 }) });
  Zotero.Libraries = { get: () => ({ libraryType: "user" }) };
  bridge.collect = async () => ({ entities: [], documents: new Map([["hash", new Uint8Array([1])]]) });
  const calls = [];
  bridge.request = async (route) => {
    calls.push(route);
    if (route === "bases") return { id: "base", revision: 1 };
    if (route.startsWith("objects")) {
      if (calls.length === 2) return { exists: false };
      throw new Error("upload failed");
    }
  };
  await assert.rejects(bridge.sync(window));
  assert.equal(calls.some(value => value.startsWith("sync/")), false);
  assert.equal(bridge.busy, false);
});

test("changed local data aborts membership commit", async () => {
  const { bridge, Zotero, window } = fixture();
  Zotero.getActiveZoteroPane = () => ({ getSelectedCollection: () => ({ name: "Fixture", key: "COL00001", libraryID: 1 }) });
  Zotero.Libraries = { get: () => ({ libraryType: "user" }) };
  let reads = 0;
  bridge.collect = async () => ({ entities: [{ key: String(++reads) }], documents: new Map() });
  const calls = [];
  bridge.request = async route => { calls.push(route); return { id: "base", revision: 1 }; };
  await assert.rejects(bridge.sync(window));
  assert.deepEqual(calls, ["bases"]);
});

test("successful unchanged sync commits exactly once with expected revision", async () => {
  const { bridge, Zotero, window } = fixture();
  Zotero.getActiveZoteroPane = () => ({ getSelectedCollection: () => ({ name: "Fixture", key: "COL00001", libraryID: 1 }) });
  Zotero.Libraries = { get: () => ({ libraryType: "user" }) };
  bridge.collect = async () => ({ entities: [], documents: new Map() });
  const calls = [];
  bridge.request = async (route, data) => {
    calls.push(route);
    if (route === "bases") return { id: "base", revision: 8 };
    assert.equal(data.complete, true);
    assert.equal(data.allow_upload, true);
    assert.equal(data.expected_revision, 8);
    return { id: "base", revision: 9 };
  };
  await bridge.sync(window);
  assert.deepEqual(calls, ["bases", "sync/base"]);
});
