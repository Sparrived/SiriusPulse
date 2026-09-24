import { get } from '../app.js';
import { toast } from '../components.js';
import { createScopedPage } from '../page-context.js';
import { store } from '../store.js';
import { createRealtimeRefresh } from './realtime.js';

const scopedPage = createScopedPage();
const $ = scopedPage.$;

const STATUS_LABELS = {
  running: '进行中',
  completed: '已完成',
  aborted: '未完成',
};

// 心跳轮询是兜底：实时推送若不可用（例如没连上 WebSocket），页面仍然会更新。
const POLL_MS = 15000;

const realtime = createRealtimeRefresh(() => load(true), {
  resources: ['work-mode'],
  debounceMs: 400,
});

export function dispose() {
  scopedPage.use(null, null);
  realtime.stop();
}

export async function init(container, params = {}) {
  scopedPage.use(params?.ctx, container);
  if (!store.currentPersona) {
    container.innerHTML = `
      <div class="card">
        <div class="card-header"><div class="card-title">工作模式</div></div>
        <div class="work-mode-empty">
          <div class="work-mode-empty-title">请先选择人格</div>
          <div class="work-mode-empty-detail">在顶部导航栏中选择要查看的人格</div>
        </div>
      </div>
    `;
    return;
  }

  $('workModeRefresh')?.addEventListener('click', () => load(false));

  realtime.start();
  scopedPage.on(window, 'sirius:event', onLiveEvent);

  await load(false);
  scopedPage.interval(() => load(true), POLL_MS);
}

function onLiveEvent(event) {
  const detail = event?.detail || {};
  if (!['agent_turn_updated', 'data_changed'].includes(String(detail.type || ''))) return;
  realtime.refreshNow(true);
}

async function load(silent) {
  const connection = $('workModeConnection');
  if (connection) {
    connection.textContent = silent ? '更新中' : '读取中';
    connection.className = 'work-mode-connection is-loading';
  }
  try {
    const data = await get('/persona/work-mode');
    if (!$('workModePage')) return;
    render(data);
    if (connection) {
      connection.textContent = '已同步';
      connection.className = 'work-mode-connection is-live';
    }
  } catch (error) {
    if (error?.name === 'AbortError' || !$('workModePage')) return;
    if (connection) {
      connection.textContent = '读取失败';
      connection.className = 'work-mode-connection is-error';
    }
    if (!silent) toast('工作模式记录加载失败', 'error');
  }
}

function render(data) {
  const summary = data?.summary || {};
  const sessions = Array.isArray(data?.sessions) ? data.sessions : [];

  setText('workModeTotal', String(summary.sessions_total ?? 0));
  setText('workModeCompleted', String(summary.sessions_completed ?? 0));
  setText('workModeRunning', String(summary.sessions_running ?? 0));
  setText('workModeLastAt', shortTime(summary.last_session_at));
  setText('workModeLastGoal', truncate(summary.last_goal, 40) || '还没有过');
  setText('workModeCount', `${sessions.length} 次`);

  const box = $('workModeSessions');
  if (box) {
    box.innerHTML = sessions.length
      ? sessions.map(sessionCard).join('')
      : emptyState(
          '她还没有进过工作模式',
          '需要多步工具协作时，她会自己调用 enter_work_mode 进去做事，做完再退出。'
        );
  }
  renderFootnote(data?.paths);
}

function sessionCard(session) {
  const status = String(session.status || 'running');
  const steps = Array.isArray(session.steps) ? session.steps : [];
  const result = String(session.result || '').trim();
  const ended = session.ended_at ? ` → ${shortTime(session.ended_at)}` : ' → 进行中';
  return `
    <article class="work-mode-session is-${escapeHtml(status)}">
      <header class="work-mode-session-head">
        <span class="work-mode-status is-${escapeHtml(status)}">${escapeHtml(
          STATUS_LABELS[status] || status
        )}</span>
        <span class="work-mode-goal">${escapeHtml(session.goal || '（没有说明目标）')}</span>
        <span class="work-mode-time">${escapeHtml(shortTime(session.started_at) + ended)}</span>
      </header>
      ${result ? resultBlock(result) : ''}
      ${
        steps.length
          ? `<ol class="work-mode-steps">${steps.map(stepItem).join('')}</ol>`
          : '<div class="work-mode-step-empty">这一轮还没有留下可看的步骤</div>'
      }
    </article>
  `;
}

function resultBlock(result) {
  return `<div class="work-mode-result">
    <span class="work-mode-result-label">工作结果</span>
    <span class="work-mode-result-text">${escapeHtml(truncate(result, 400))}</span>
  </div>`;
}

function stepItem(step, index) {
  const position = String(index + 1).padStart(2, '0');
  if (step?.kind === 'midway') {
    return `<li class="work-mode-step is-midway">
      <span class="work-mode-step-index">${position}</span>
      <div class="work-mode-step-body">
        <div class="work-mode-midway-label">发到群里</div>
        <div class="work-mode-midway">${escapeHtml(truncate(step.text, 400))}</div>
      </div>
    </li>`;
  }

  const text = String(step?.text || '').trim();
  const tools = Array.isArray(step?.tools) ? step.tools : [];
  const results = Array.isArray(step?.results) ? step.results : [];
  return `<li class="work-mode-step">
    <span class="work-mode-step-index">${position}</span>
    <div class="work-mode-step-body">
      ${
        text
          ? `<div class="work-mode-thought"><span class="work-mode-thought-label">正文未外发</span>${escapeHtml(
              truncate(text, 400)
            )}</div>`
          : ''
      }
      ${tools.length ? `<div class="work-mode-tools">${tools.map(toolChip).join('')}</div>` : ''}
      ${results.map(resultDetail).join('')}
    </div>
  </li>`;
}

function toolChip(tool) {
  const name = String(tool?.name || '未知工具');
  const args = String(tool?.arguments || '').trim();
  return `<span class="work-mode-tool" title="${escapeHtml(args)}">${escapeHtml(name)}</span>`;
}

function resultDetail(entry) {
  const output = String(entry?.output || '').trim();
  if (!output) return '';
  return `<details class="work-mode-result-detail">
    <summary>${escapeHtml(String(entry?.tool || 'tool'))} 的结果</summary>
    <pre>${escapeHtml(truncate(output, 2000))}</pre>
  </details>`;
}

function renderFootnote(paths) {
  const box = $('workModeFootnote');
  if (!box) return;
  const path = paths?.sessions;
  box.innerHTML = path
    ? `记录文件：<code>${escapeHtml(path)}</code> · 工作模式内的正文不会发到群里，只有工具结果和 send_midway_msg 会。`
    : '';
}

function emptyState(title, detail) {
  return `<div class="work-mode-empty">
    <div class="work-mode-empty-title">${escapeHtml(title)}</div>
    <div class="work-mode-empty-detail">${escapeHtml(detail)}</div>
  </div>`;
}

function setText(id, text) {
  const el = $(id);
  if (el) el.textContent = text;
}

function truncate(value, max) {
  const text = String(value || '');
  return text.length > max ? `${text.slice(0, max)}…` : text;
}

function shortTime(value) {
  const text = String(value || '');
  if (!text) return '—';
  const date = new Date(text);
  if (Number.isNaN(date.getTime())) return text;
  const pad = (n) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(
    date.getHours()
  )}:${pad(date.getMinutes())}`;
}

function escapeHtml(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}
