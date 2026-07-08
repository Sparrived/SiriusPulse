import { store } from '../store.js';
import { get } from '../app.js';
import { toast } from '../components.js';
import { createRealtimeRefresh } from './realtime.js';
import { createScopedPage } from '../page-context.js';

const scopedPage = createScopedPage();
const $ = scopedPage.$;
const $$ = scopedPage.$$;

const SOURCES = {
  webui: {
    label: 'WebUI',
    path: () => '/system/logs',
    empty: 'WebUI 日志文件尚未创建',
  },
  persona: {
    label: '人格',
    path: () => '/persona/logs?source=persona',
    empty: '人格日志文件尚未创建',
  },
};

let paused = false;
let activeSource = 'webui';
const stateBySource = new Map();
const realtime = createRealtimeRefresh(() => loadLogs(false), {
  resources: ['logs'],
  debounceMs: 250,
  personaScoped: false,
  shouldRefresh: () => !paused,
});

function stateFor(source) {
  if (!stateBySource.has(source)) {
    stateBySource.set(source, { offset: 0, text: '', path: '' });
  }
  return stateBySource.get(source);
}

export function dispose() {
  scopedPage.use(null, null);
  realtime.stop();
}

export async function init(container, params = {}) {
  scopedPage.use(params?.ctx, container);
  const refresh = $('logsRefresh');
  const pause = $('logsPause');
  const clear = $('logsClear');

  refresh.onclick = () => resetAndLoad();
  pause.onclick = () => {
    paused = !paused;
    pause.textContent = paused ? '继续' : '暂停';
    setStatus(paused ? '已暂停' : statusText());
  };
  clear.onclick = () => {
    const current = stateFor(activeSource);
    current.text = '';
    $('logsConsole').textContent = '';
  };

  $$('[data-source]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const source = btn.dataset.source;
      if (!source || source === activeSource) return;
      activeSource = source;
      $$('[data-source]').forEach((el) => el.classList.toggle('active', el === btn));
      renderCachedSource();
      if (!stateFor(activeSource).offset) await resetAndLoad();
      else if (!paused) await loadLogs(false);
    });
  });

  await resetAndLoad();
  realtime.start();
}

async function resetAndLoad() {
  const current = stateFor(activeSource);
  current.offset = 0;
  current.text = '';
  current.path = '';
  renderCachedSource();
  await loadLogs(true);
}

async function loadLogs(initial) {
  if (!$('logsConsole')) {
    dispose();
    return;
  }
  const source = activeSource;
  const current = stateFor(source);
  const config = SOURCES[source] || SOURCES.webui;
  const lines = initial ? 300 : 2000;
  const separator = config.path().includes('?') ? '&' : '?';
  const path = `${config.path()}${separator}lines=${lines}&offset=${current.offset}`;
  try {
    const data = await get(path);
    if (source !== activeSource) return;
    current.offset = data.offset || current.offset;
    current.path = data.path || '';
    $('logsPath').textContent = current.path;
    if (!data.exists) {
      setStatus(config.empty);
      return;
    }
    appendLines(source, data.lines || []);
    setStatus(statusText());
  } catch (e) {
    if (e?.name === 'AbortError') return;
    setStatus('日志加载失败');
    toast('日志加载失败', 'error');
  }
}

function renderCachedSource() {
  const current = stateFor(activeSource);
  const consoleEl = $('logsConsole');
  if (consoleEl) consoleEl.textContent = current.text;
  const pathEl = $('logsPath');
  if (pathEl) pathEl.textContent = current.path || '';
  setStatus(paused ? '已暂停' : statusText());
}

function appendLines(source, lines) {
  if (!lines.length) return;
  const current = stateFor(source);
  const consoleEl = $('logsConsole');
  const shouldStick = consoleEl.scrollTop + consoleEl.clientHeight >= consoleEl.scrollHeight - 24;
  current.text += `${lines.join('\n')}\n`;
  const all = current.text.split('\n');
  if (all.length > 3000) current.text = all.slice(-3000).join('\n');
  if (source === activeSource) {
    consoleEl.textContent = current.text;
    if (shouldStick) consoleEl.scrollTop = consoleEl.scrollHeight;
  }
}

function statusText() {
  const config = SOURCES[activeSource] || SOURCES.webui;
  if (activeSource === 'persona') {
    return `事件实时 · ${store.currentPersona || '当前人格'} · ${config.label}`;
  }
  return `事件实时 · ${config.label}`;
}

function setStatus(text) {
  const el = $('logsStatus');
  if (el) el.textContent = text;
}
