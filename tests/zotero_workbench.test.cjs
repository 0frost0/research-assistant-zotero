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

test('visual observations are labeled, scoped and cannot be saved as source-quote AI drafts', async () => {
  const f = await fixture();
  vm.runInContext("selected.add('one')", f.context);
  const asset = {asset_id: 'V1', image_hash: 'hash', document_version: 'version'};
  f.context.renderAnswer({status: 'answered', scope: 'original', claims: [{text: '蓝柱较高', citations: [{
    kind: 'original', source_id: 'paper.pdf', page: 2, citation_type: 'visual', observation: '蓝柱高于橙柱',
    visual_asset: asset}]}], evidence: [], gaps: [], warnings: [],
    trace: {steps: [], model_calls: 2, retrieval_calls: 1, rewrite_rounds: 0, input_tokens: 0, output_tokens: 0}});
  const nodes = [];
  function visit(node) { nodes.push(node); for (const child of node.children) visit(child); }
  visit(f.elements.get('results'));
  assert.match(nodes.map(n => n.textContent).join('\n'), /模型视觉观察（非原文摘录）/);
  assert.ok(!nodes.some(n => n.textContent === '保存为 AI 草稿'));
  let read;
  f.bridge.request = async (path, body) => {
    read = {path, body};
    return {...asset, source_id: 'paper.pdf', page: 2, data_url: 'data:image/png;base64,cGl4ZWxz'};
  };
  vm.runInContext("selected.clear(); selected.add('other')", f.context);
  await nodes.find(n => n.textContent === '查看引用图片').handlers.click();
  assert.equal(read.path, 'visual');
  assert.deepEqual(Array.from(read.body.kb_ids), ['one']);
  const row = nodes.find(n => n.className === 'answer-claim');
  assert.ok(row.children.some(n => n.src === 'data:image/png;base64,cGl4ZWxz'));
});

test('experimental multimodal flag is opt-in and never applied to notes-only requests', async () => {
  const f = await fixture();
  vm.runInContext("selected.add('one')", f.context);
  f.elements.get('query').value = 'chart';
  const requests = [];
  f.bridge.request = async (path, body) => { requests.push(body); return {results: [], backend: 'fixture'}; };
  await f.context.runSearch({preventDefault() {}});
  assert.equal(requests[0].multimodal, undefined);
  f.elements.get('multimodalEvidence').checked = true;
  await f.context.runSearch({preventDefault() {}});
  assert.equal(requests[1].multimodal, true);
  f.radios.forEach(r => r.checked = r.value === 'notes');
  await f.context.runSearch({preventDefault() {}});
  assert.equal(requests[2].multimodal, undefined);
});

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

test('sufficiency gate is opt-in, ask-only and excluded from the notes lane', async () => {
  const f = await fixture();
  vm.runInContext("selected.add('one')", f.context);
  f.elements.get('query').value = 'evidence';
  f.context.window.setInterval = () => 1;
  f.context.window.clearInterval = () => {};
  const requests = [];
  f.bridge.request = async (path, body) => {
    requests.push({path, body});
    return path === 'search' ? {results: [], backend: 'fixture'} : {
      state: 'succeeded', job_id: 'fixture', result: {
        status: 'no_evidence', evidence: [], claims: [], gaps: [], warnings: [],
        trace: {steps: [], model_calls: 0, retrieval_calls: 1, rewrite_rounds: 0}}};
  };
  const ask = () => f.context.runSearch({preventDefault() {}, submitter: {id: 'askButton'}});
  await ask();
  assert.equal(requests.at(-1).body.sufficiency_gate, undefined);
  f.elements.get('sufficiencyGate').checked = true;
  await f.context.runSearch({preventDefault() {}});
  assert.equal(requests.at(-1).path, 'search');
  assert.equal(requests.at(-1).body.sufficiency_gate, undefined);
  for (const scope of ['both', 'original', 'notes']) {
    f.radios.forEach(r => r.checked = r.value === scope);
    await ask();
    assert.equal(requests.at(-1).path, 'ask-async');
    assert.equal(requests.at(-1).body.sufficiency_gate, scope === 'notes' ? undefined : true);
  }
});

test('sufficiency trace displays uncalibrated judgment without invented draft counts', async () => {
  const f = await fixture();
  f.context.renderAnswer({status: 'no_evidence', evidence: [], claims: [], gaps: [], warnings: [],
    trace: {model_calls: 2, retrieval_calls: 2, rewrite_rounds: 1, input_tokens: 0, output_tokens: 0, steps: [
      {stage: 'sufficiency', round: 1, sufficient: false, selected: 6},
      {stage: 'sufficiency', round: 2, sufficient: true, selected: 4}]}});
  const texts = [];
  function visit(node) { texts.push(node.textContent); for (const child of node.children) visit(child); }
  visit(f.elements.get('results'));
  const output = texts.join('\n');
  assert.match(output, /证据不足，未生成/);
  assert.match(output, /可尝试生成（模型判断）/);
  assert.match(output, /未经独立校准/);
  assert.doesNotMatch(output, /undefined|草稿|已采纳/);
});

test('QA trace distinguishes the search pool from passages read by the answer model', async () => {
  const f = await fixture();
  f.context.renderAnswer({status: 'no_evidence', evidence: [], claims: [], gaps: [], warnings: [],
    trace: {model_calls: 1, retrieval_calls: 2, rewrite_rounds: 0, elapsed_ms: 1000,
      input_tokens: 0, output_tokens: 0, steps: [{stage: 'retrieve', round: 1,
        query: '什么是心衰', searches: [{query: '什么是心衰'}, {query: 'heart failure definition'}],
        candidates: 35, selected: 6, new_selected: 6, elapsed_ms: 500}]}});
  const texts = [];
  function visit(node) { texts.push(node.textContent); for (const child of node.children) visit(child); }
  visit(f.elements.get('results'));
  assert.match(texts.join('\n'), /heart failure definition/);
  assert.match(texts.join('\n'), /35 条去重候选 \/ 6 条供本轮回答 \/ 6 条首次入选/);
  assert.match(f.elements.get('resultMeta').textContent, /检索 2 次/);
});

test('personal-memory rewrite shows a search step and no nonexistent review', async () => {
  const f = await fixture();
  f.context.renderAnswer({status: 'no_evidence', scope: 'notes', evidence: [], claims: [], gaps: [],
    trace: {model: 'fixture-model', model_calls: 1, retrieval_calls: 2, rewrite_rounds: 1,
      elapsed_ms: 20, input_tokens: 2, output_tokens: 2,
      steps: [{stage: 'memory_rewrite', scope: 'notes', round: 1, query: 'irregular EHR intervals', candidates: 1}]}});
  const texts = [];
  function visit(node) { texts.push(node.textContent); for (const child of node.children) visit(child); }
  visit(f.elements.get('results'));
  assert.match(texts.join('\n'), /检索词改写：irregular EHR intervals/);
  assert.match(texts.join('\n'), /回答模型：fixture-model/);
  assert.doesNotMatch(texts.join('\n'), /undefined|复核模型/);
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
