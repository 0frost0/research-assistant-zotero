const {bridge, done} = window.arguments[0];
const get = id => document.getElementById(id);
const recommended = {
  engineExecutable: 'D:\\miniconda3\\envs\\deepagents\\python.exe',
  engineWorkdir: 'D:\\workspace\\research-assistant-zotero-github',
  engineScript: 'D:\\workspace\\research-assistant-zotero-github\\scripts\\start_local_engine.py',
};
const paths = ['engineExecutable', 'engineWorkdir', 'engineScript', 'qwenSshHost', 'qwenServiceUnit', 'sshExecutable'];
for (const key of paths) get(key).value = bridge.pref(key, recommended[key] || '');
for (const key of ['autoStartEngine', 'stopEngineOnExit']) get(key).checked = bridge.pref(key, false);
let saved = false;
window.addEventListener('unload', () => done(saved), {once: true});
get('suggest').addEventListener('click', () => {
  for (const [key, value] of Object.entries(recommended)) get(key).value = value;
});
get('cancel').addEventListener('click', () => window.close());
get('settings').addEventListener('submit', async event => {
  event.preventDefault();
  get('fields').disabled = true;
  get('error').textContent = '';
  try {
    const values = Object.fromEntries(paths.map(key => [key, get(key).value.trim()]));
    for (const key of ['autoStartEngine', 'stopEngineOnExit']) values[key] = get(key).checked;
    await bridge.saveEngineSettings(values);
    saved = true;
    window.close();
  } catch (error) {
    get('error').textContent = error.message;
    get('fields').disabled = false;
  }
});
