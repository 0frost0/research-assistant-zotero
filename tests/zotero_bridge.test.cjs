const { test } = require("node:test");
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");

test("manifest includes Zotero 9 required update URL", () => {
  const manifest = JSON.parse(
    fs.readFileSync(path.join(__dirname, "../deployment/zotero/manifest.json"), "utf8")
  );
  const zotero = manifest.applications.zotero;
  assert.match(zotero.update_url, /^https:\/\//);
  // C01: Zotero 9.0.6 rejects manifests without this field, so it must stay a
  // real, resolvable location rather than a placeholder domain.
  assert.doesNotMatch(zotero.update_url, /example\.(invalid|com|org)/);

  // The published update manifest must describe the version actually shipped.
  const updates = JSON.parse(
    fs.readFileSync(path.join(__dirname, "../updates.json"), "utf8")
  );
  const entries = updates.addons[zotero.id].updates;
  const current = entries.find((item) => item.version === manifest.version);
  assert.ok(current, `updates.json has no entry for version ${manifest.version}`);
  assert.ok(current.update_link.endsWith(`research-assistant-${manifest.version}.xpi`));
  assert.equal(
    current.applications.zotero.strict_min_version,
    zotero.strict_min_version
  );
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
  const scope = { Zotero, Services: {}, ChromeUtils: {}, Subprocess, PathUtils: {join: path.win32.join}, IOUtils: {exists: async () => false}, URL, Uint8Array, console };
  vm.createContext(scope);
  vm.runInContext(fs.readFileSync(path.join(__dirname, "../deployment/zotero/bridge.js"), "utf8"), scope);
  return { bridge: scope.ResearchBridge, Zotero, window, prefs, events, Subprocess, IOUtils: scope.IOUtils };
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
    return { exitCode: null, stdout: {readString: async () => ''}, kill: async () => ({ exitCode: -9 }) };
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

test("desktop startup prepares dependencies then connects, with duplicate calls merged", async () => {
  const { bridge, Subprocess } = fixture();
  bridge.setPref('engineExecutable', 'C:/Python/python.exe');
  bridge.setPref('engineWorkdir', 'D:/assistant');
  bridge.setPref('endpoint', 'http://127.0.0.1:8765');
  let launches = 0;
  Subprocess.call = async options => {
    launches++;
    assert.equal(options.arguments[0], 'D:\\assistant\\scripts\\prepare_desktop_services.py');
    return {stdout: {readString: async () => 'ready'}, wait: async () => ({exitCode: 0})};
  };
  bridge.backendStatus = async () => ({connected: true});
  const results = await Promise.all([bridge.startAllServices(), bridge.startAllServices()]);
  assert.equal(launches, 1);
  assert.ok(results.every(result => result.connected));
});

test("cancelled desktop configuration is not reported as successful startup", async () => {
  const { bridge } = fixture();
  bridge.configureEngine = async () => false;
  await assert.rejects(bridge.startAllServices(), /已取消/);
});

test('Windows token path uses native separators and reads token without exposing it', async () => {
  const {bridge, IOUtils} = fixture();
  bridge.setPref('engineWorkdir', 'D:\\workspace\\research-assistant-zotero-github');
  IOUtils.exists = async value => {
    assert.equal(value, 'D:\\workspace\\research-assistant-zotero-github\\.data\\zotero\\connection-token');
    assert.equal(value.includes('/'), false);
    return true;
  };
  IOUtils.readUTF8 = async () => 'synthetic-token\n';
  await bridge.readLocalToken();
  assert.equal(bridge.pref('token'), 'synthetic-token');
});

test('SSH resolves exe suffix and respects explicit path without requiring PATH', async () => {
  const {bridge, Subprocess, IOUtils} = fixture();
  const executable = 'C:\\Windows\\System32\\OpenSSH\\ssh.exe';
  Subprocess.pathSearch = async name => {
    assert.equal(name, 'ssh.exe');
    return executable;
  };
  assert.equal(await bridge.resolveSshExecutable(), executable);
  bridge.setPref('sshExecutable', executable);
  IOUtils.exists = async value => value === executable;
  Subprocess.pathSearch = async () => { throw new Error('PATH unavailable'); };
  assert.equal(await bridge.resolveSshExecutable(), executable);
  IOUtils.exists = async () => false;
  await assert.rejects(bridge.resolveSshExecutable(), /SSH 程序不存在/);
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
