import { store } from '../store.js';
import { get } from '../app.js';
import { toast, $ } from '../components.js';

let timer = null;
let offset = 0;
let paused = false;

export async function init() {
  const refresh = $('logsRefresh');
  const pause = $('logsPause');
  const clear = $('logsClear');

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

async function loadLogs(initial) {
  if (!$('logsConsole')) {
    if (timer) clearInterval(timer);
    timer = null;
    return;
  }
  const lines = initial ? 300 : 2000;
  const path = `/persona/logs?lines=${lines}&offset=${offset}`;
  try {
    const data = await get(path);
    offset = data.offset || offset;
    $('logsPath').textContent = data.path || '';
    if (!data.exists) {
      setStatus('日志文件尚未创建');
      return;
    }
    appendLines(data.lines || []);
    setStatus(`实时刷新中 · ${store.currentPersona || '当前人格'}`);
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
