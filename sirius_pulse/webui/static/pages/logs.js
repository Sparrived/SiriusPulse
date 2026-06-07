import { store } from '../store.js';
import { get } from '../app.js';
import { toast, $ } from '../components.js';

let timer = null;
let offset = 0;
let paused = false;
let target = 'webui';

export async function init() {
  const selector = $('logsTarget');
  const refresh = $('logsRefresh');
  const pause = $('logsPause');
  const clear = $('logsClear');

  selector.value = target;
  selector.onchange = () => {
    target = selector.value;
    resetAndLoad();
  };
  refresh.onclick = () => resetAndLoad();
  pause.onclick = () => {
    paused = !paused;
    pause.textContent = paused ? '继续' : '暂停';
    setStatus(paused ? '已暂停' : '实时刷新中');
  };
  clear.onclick = () => {
    $('logsConsole').textContent = '';
  };

  await resetAndLoad();
  startPolling();
}

async function resetAndLoad() {
  offset = 0;
  $('logsConsole').textContent = '';
  await loadLogs(true);
}

function startPolling() {
  if (timer) clearInterval(timer);
  timer = setInterval(() => {
    if (!paused) loadLogs(false);
  }, 1500);
}

function buildPath(initial) {
  const lines = initial ? 300 : 2000;
  if (target === 'webui') return `/system/logs?lines=${lines}&offset=${offset}`;
  const name = store.currentPersona;
  if (!name) return null;
  return `/personas/${encodeURIComponent(name)}/logs?lines=${lines}&offset=${offset}`;
}

async function loadLogs(initial) {
  if (!$('logsConsole')) {
    if (timer) clearInterval(timer);
    timer = null;
    return;
  }
  const path = buildPath(initial);
  if (!path) {
    setStatus('请先选择人格');
    return;
  }
  try {
    const data = await get(path);
    offset = data.offset || offset;
    $('logsPath').textContent = data.path || '';
    if (!data.exists) {
      setStatus('日志文件尚未创建');
      return;
    }
    appendLines(data.lines || []);
    setStatus(`实时刷新中 · ${data.name || target}`);
  } catch (e) {
    setStatus('日志加载失败');
    toast('日志加载失败', 'error');
  }
}

function appendLines(lines) {
  if (!lines.length) return;
  const consoleEl = $('logsConsole');
  const shouldStick = consoleEl.scrollTop + consoleEl.clientHeight >= consoleEl.scrollHeight - 24;
  consoleEl.textContent += `${lines.join('\n')}\n`;
  const all = consoleEl.textContent.split('\n');
  if (all.length > 3000) consoleEl.textContent = all.slice(-3000).join('\n');
  if (shouldStick) consoleEl.scrollTop = consoleEl.scrollHeight;
}

function setStatus(text) {
  const el = $('logsStatus');
  if (el) el.textContent = text;
}
