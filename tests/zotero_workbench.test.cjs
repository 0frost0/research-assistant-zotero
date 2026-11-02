// Execute the real workbench with an API-shaped fixture and a minimal DOM.
// These are behavior tests, not a replacement for native Zotero rendering QA.
const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const root = path.join(__dirname, '../deployment/zotero');

class Element {
  constructor() { this.children = []; this.dataset = {}; this.handlers = {}; this.textContent = ''; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  setAttribute(name, value) { this[name] = value; }
  addEventListener(name, fn) { this.handlers[name] = fn; }
  focus() {}
}

async function fixture() {
  const elements = new Map([...fs.readFileSync(path.join(root, 'assistant.xhtml'), 'utf8')
    .matchAll(/id="([^"]+)"/g)].map(match => [match[1], new Element()]));
  const radios = ['both', 'original', 'notes'].map(value => Object.assign(new Element(), {value, checked: value === 'both'}));
  const document = {
    getElementById: id => { assert.ok(elements.has(id), `Unknown DOM id ${id}`); return elements.get(id); },
    createElement: () => new Element(),
    querySelector: () => radios.find(r => r.checked),
    querySelectorAll: selector => selector === '[name=scope]' ? radios : [],
  };
  const bridge = {backendStatus: async () => ({connected: true}), pref: () => '',
    request: async () => ({bases: []})};
  const window = {arguments: [{bridge}], addEventListener() {}, confirm: () => false};
  const context = vm.createContext({document, window, structuredClone});
  vm.runInContext(fs.readFileSync(path.join(root, 'assistant.js'), 'utf8'), context);
  await window.ResearchAssistantWorkbench.ready;
  assert.equal(window.ResearchAssistantWorkbench.startupError, '');
  return {elements, bridge, context, radios, select: base => context.selectBase(base)};
}

function snapshot(notes = []) {
  return {bases: [], entities: [
    {id: 'external', kind: 'note', revision: 1, data: {kind: 'note', confirmed: true, body: '我的方法记录'}},
    {id: 'annotation', kind: 'annotation', revision: 1, data: {kind: 'annotation', confirmed: true, body: '', comment: '需要核对实验'}},
    {id: 'draft', kind: 'note', data: {kind: 'note', confirmed: false, body: '未确认'}},
    {id: 'empty', kind: 'note', data: {kind: 'note', confirmed: true, body: '  '}},
    {id: 'paper', kind: 'document', data: {kind: 'document', title: '论文'}},
  ], notes};
}

test('index preparation polls without presenting an empty or failed search', async () => {
  const f = await fixture();
  vm.runInContext("selected.add('one')", f.context);
  f.elements.get('query').value = '什么是结构化数据';
  let requests = 0;
  f.context.window.setTimeout = resolve => {
    assert.match(f.elements.get('resultMeta').textContent, /准备语义索引/);
    assert.match(f.elements.get('results').children[0].textContent, /准备中/);
    resolve();
  };
  f.bridge.request = async () => ++requests === 1
    ? {status: 'indexing', warning: '准备中', results: []}
    : {backend: 'multimodal', warning: null, results: []};
  await f.context.runSearch({preventDefault() {}});
  assert.equal(requests, 2);
  assert.match(f.elements.get('resultMeta').textContent, /0 条结果/);
  assert.equal(f.elements.get('searchButton').disabled, false);
});

test('scope hints count both raw Zotero entities and local notes, excluding drafts and archived notes', async () => {
  const f = await fixture();
  const base = {id: 'one', name: '我的库'};
  const note = {id: 'local', revision: 1, data: {confirmed: true, body: '我的理解'}};
  let data = snapshot([note, {data: {confirmed: false, body: 'AI 草稿'}},
    {data: {confirmed: true, archived: true, body: '旧记录'}}]);
  f.bridge.request = async () => data;
  await f.select(base);
  assert.match(f.elements.get('scopeHint').textContent, /3 条/);
  assert.match(f.elements.get('scopeHint').textContent, /我的库/);
  for (const [scope, button] of [['original', '依据论文回答'], ['notes', '根据我的笔记回答'], ['both', '文献与笔记分开回答']]) {
    f.radios.forEach(r => r.checked = r.value === scope);
    f.radios.find(r => r.checked).handlers.change();
    assert.equal(f.elements.get('askButton').textContent, button);
    assert.match(f.elements.get('scopeHint').textContent, /3 条/);
  }
  // Read after archive/refresh, then after user confirmation of a new draft.
  note.data.archived = true;
  await f.select(base);
  assert.match(f.elements.get('scopeHint').textContent, /2 条/);
  data.notes[1].data.confirmed = true;
  await f.select(base);
  assert.match(f.elements.get('scopeHint').textContent, /3 条/);
  data = {entities: [], notes: []};
  await f.select(base);
  assert.match(f.elements.get('scopeHint').textContent, /暂无/);
});

test('late responses cannot replace the newly selected knowledge base or its notes', async () => {
  const f = await fixture();
  let resolveOld;
  f.bridge.request = async url => url === 'bases/old'
    ? new Promise(resolve => { resolveOld = resolve; }) : {entities: [], notes: []};
  const old = f.select({id: 'old', name: '旧库'});
  await f.select({id: 'new', name: '新库'});
  resolveOld(snapshot());
  await old;
  assert.equal(f.elements.get('baseTitle').textContent, '新库');
  assert.match(f.elements.get('scopeHint').textContent, /暂无/);
  assert.equal(f.elements.get('members').children.length, 0);
});

test('archiving an open base clears its previously displayed record count', async () => {
  const f = await fixture();
  f.bridge.request = async () => snapshot();
  await f.select({id: 'one', name: '我的库'});
  assert.match(f.elements.get('scopeHint').textContent, /2 条/);
  await f.select({id: 'one', name: '我的库', archived: true});
  assert.doesNotMatch(f.elements.get('scopeHint').textContent, /2 条/);
});

test('refresh retains unsaved text and original revision; switching or cancelling can be declined', async () => {
  const f = await fixture();
  const base = {id: 'one', name: '我的库'};
  const note = {id: 'local', revision: 1, data: {confirmed: true, body: '旧理解'}};
  f.bridge.request = async url => url === 'bases' ? {bases: [base]} : snapshot([note]);
  await f.select(base);
  f.context.editNote(note);
  f.elements.get('noteBody').value = '未保存的新理解';
  await f.context.refresh();
  assert.equal(f.elements.get('noteForm').hidden, false);
  assert.equal(f.elements.get('noteBody').value, '未保存的新理解');
  assert.equal(vm.runInContext('editing.revision', f.context), 1);
  assert.equal(await f.select({id: 'two', name: '另一个库'}), false);
  f.elements.get('cancelNote').handlers.click();
  assert.equal(f.elements.get('noteForm').hidden, false);
  assert.equal(f.elements.get('baseTitle').textContent, '我的库');
});

test('citation opens exact historical version and reports newer/archive status without substituting', async () => {
  const f = await fixture();
  f.bridge.request = async () => [
    {id: 'local', kb_id: 'one', revision: 2, body: '新的观点', archived: true},
    {id: 'local', kb_id: 'one', revision: 1, body: '当时的理解', anchors: []},
  ];
  await f.context.openNoteCitation({entity_id: 'local', kb_id: 'one', revision: 1});
  let texts = f.elements.get('noteReference').children.map(x => x.textContent).join('\n');
  assert.match(texts, /当时的理解/);
  assert.match(texts, /v2（已归档）/);
  assert.doesNotMatch(texts, /新的观点/);
  await f.context.openNoteCitation({entity_id: 'local', kb_id: 'one', revision: 99});
  texts = f.elements.get('noteReference').children.map(x => x.textContent).join('\n');
  assert.match(texts, /未找到引用的笔记版本/);
});
