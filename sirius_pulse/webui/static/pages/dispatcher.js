import { get } from '../app.js';
import { toast } from '../components.js';
import { createScopedPage } from '../page-context.js';

const scopedPage = createScopedPage();
const $ = scopedPage.$;

const REASONS = {
  selected: '获得发送权',
  group_busy: '群组已有租约',
  another_worker_selected: '其他人格优先',
  reply_cooldown: '回复冷却中',
  peer_waiting_for_humans: '等待人类消息',
  peer_topic_closed: '没有连续话题',
  peer_target_unavailable: '期待的人格不可用',
  peer_budget_exhausted: 'Peer 轮次已用尽',
  peer_disabled: 'Peer 交互已关闭',
  target_unavailable: '目标人格不可用',
  all_candidates_silent: '所有人格判断为静默',
  preview_failed: '基础评分失败',
  no_workers: '没有在线人格',
  event_granted: '事件已被其他人格接管',
};

const STATUS_LABELS = {
  granted: '已授予',
  sent: '已送达',
  silent: '静默结束',
  observed: '已观察',
  expired: '已过期',
};

const STRATEGY_LABELS = {
  immediate: '立即',
  delayed: '延后',
};

export function dispose() {
  scopedPage.use(null, null);
}

export async function init(container, params = {}) {
  scopedPage.use(params?.ctx, container);
  $('dispatcherRefresh').addEventListener('click', () => loadOverview(false));
  await loadOverview(false);
  scopedPage.interval(() => loadOverview(true), 5000);
}

async function loadOverview(silent) {
  const connection = $('dispatcherConnection');
  connection.textContent = '读取中';
  connection.className = 'dispatcher-connection is-loading';
  try {
    const data = await get('/dispatcher/overview');
    if (!$('dispatcherPage')) return;
    render(data);
    connection.textContent = data.available ? 'LIVE' : '未启用';
    connection.className = `dispatcher-connection ${data.available ? 'is-live' : 'is-muted'}`;
  } catch (error) {
    if (error?.name === 'AbortError' || !$('dispatcherPage')) return;
    connection.textContent = '不可用';
    connection.className = 'dispatcher-connection is-error';
    console.warn('dispatcher overview render failed:', error);
    if (!silent) toast('调度器状态加载失败', 'error');
    else console.warn('dispatcher overview refresh failed:', error);
  }
}

function render(data) {
  const summary = data.summary || {};
  const workers = data.workers || [];
  const groups = data.groups || [];
  const events = data.events || [];
  const activeGroups = groups.filter(group => group.active);

  $('dispatchActiveTurns').textContent = String(summary.active_turns ?? 0);
  $('dispatchOnlineWorkers').textContent = `${summary.workers_online ?? 0}/${summary.workers_total ?? 0}`;
  $('dispatchWorkerDetail').textContent = workers.length ? '注册表心跳正常' : '暂无 Worker';
  $('dispatchGroups').textContent = String(summary.groups_total ?? 0);
  $('dispatchSent').textContent = String(summary.sent_24h ?? 0);
  $('dispatcherLiveCount').textContent = `${activeGroups.length} 个活跃`;
  $('dispatcherWorkerNote').textContent = `${summary.decisions_24h ?? 0} 次决策 / 24h`;

  renderLiveTurns(activeGroups);
  renderPolicy(data.policy || {});
  renderGroups(groups);
  renderEvents(events);
  renderWorkers(workers, groups);
  $('dispatcherSource').textContent = data.available
    ? `数据源 · ${data.db_path || 'dispatcher.db'}`
    : `等待数据库 · ${(data.configured_paths || [])[0] || 'dispatcher/dispatcher.db'}`;
}

function renderLiveTurns(groups) {
  const root = $('dispatcherLiveTurns');
  if (!groups.length) {
    root.innerHTML = emptyState('◎', '当前没有活跃租约', '新消息进入后，这里会显示唯一发送者');
    return;
  }
  root.innerHTML = groups.slice(0, 6).map(group => `
    <div class="dispatcher-live-turn">
      <div class="dispatcher-live-turn-head">
        <span class="dispatcher-group-id">群 ${escapeHtml(group.group_id)}</span>
        <span class="dispatcher-lease-remaining">剩余 ${formatDuration(group.active_remaining_seconds)}</span>
      </div>
      <div class="dispatcher-turn-track">
        <span class="dispatcher-track-node is-done">消息</span>
        <span class="dispatcher-track-line is-done"></span>
        <span class="dispatcher-track-node is-active">${escapeHtml(group.active_worker_id || '未知人格')}</span>
        <span class="dispatcher-track-line"></span>
        <span class="dispatcher-track-node">发送</span>
      </div>
      <div class="dispatcher-live-meta">事件 ${escapeHtml(shortId(group.active_event_id))} · 租约 ${escapeHtml(shortId(group.active_lease_id))}</div>
    </div>
  `).join('');
}

function renderPolicy(policy) {
  const values = policy.values || {};
  const state = $('dispatcherPolicyState');
  state.textContent = policy.dispatch_enabled ? '已启用' : '未启用';
  state.className = `dispatcher-policy-state ${policy.dispatch_enabled ? 'is-on' : 'is-off'}`;
  const rows = [
    ['最小回复间隔', formatPolicyValue(values.dispatch_min_reply_interval_seconds, '秒')],
    ['租约时长', formatPolicyValue(values.dispatch_lease_seconds, '秒')],
    ['Peer 冷却窗口', formatPolicyValue(values.dispatch_peer_cooldown_seconds, '秒')],
    ['最大 Peer 轮次', formatPolicyValue(values.dispatch_max_peer_turns, '轮')],
    ['分数收集窗口', formatPolicyValue(values.dispatch_score_collection_seconds, '秒')],
    ['活跃度窗口', formatPolicyValue(values.dispatch_activity_window_seconds, '秒')],
    ['单次活跃惩罚', formatPolicyValue(values.dispatch_activity_penalty_per_reply, '分')],
    ['最大活跃惩罚', formatPolicyValue(values.dispatch_max_activity_penalty, '分')],
  ];
  $('dispatcherPolicy').innerHTML = rows.map(([label, value]) => `
    <div class="dispatcher-policy-row"><span>${label}</span><strong>${value}</strong></div>
  `).join('');
}

function renderGroups(groups) {
  const body = $('dispatcherGroups');
  if (!groups.length) {
    body.innerHTML = `<tr><td colspan="5">${emptyTable('暂无群组事件')}</td></tr>`;
    return;
  }
  body.innerHTML = groups.slice(0, 40).map(group => `
    <tr>
      <td><span class="dispatcher-table-primary">${escapeHtml(group.group_id)}</span></td>
      <td class="text-mono">${escapeHtml(group.active_worker_id || '—')}</td>
      <td>${group.active ? '<span class="dispatcher-status is-active">占用中</span>' : '<span class="dispatcher-status">空闲</span>'}</td>
      <td>${formatRelative(group.last_human_at)}</td>
      <td><span class="dispatcher-peer-count">${group.peer_turns || 0}</span></td>
    </tr>
  `).join('');
}

function renderEvents(events) {
  const root = $('dispatcherEvents');
  if (!events.length) {
    root.innerHTML = emptyState('◌', '暂无决策记录', '消息经过调度器后会出现在这里');
    return;
  }
  root.innerHTML = events.slice(0, 18).map(event => {
    const status = STATUS_LABELS[event.status] || event.status || '未知';
    const reason = REASONS[event.reason] || event.reason || '历史记录未保存原因';
    const granted = ['granted', 'sent'].includes(event.status);
    const score = event.final_score
      ? `最终 ${Number(event.final_score).toFixed(2)} · 基础 ${Number(event.base_score || 0).toFixed(2)}`
      : '';
    const strategy = STRATEGY_LABELS[event.response_strategy]
      ? ` · ${STRATEGY_LABELS[event.response_strategy]}`
      : '';
    return `
      <div class="dispatcher-event-row">
        <span class="dispatcher-event-mark ${granted ? 'is-granted' : 'is-observed'}"></span>
        <div class="dispatcher-event-main">
          <div><strong>${escapeHtml(event.group_id)}</strong><span class="dispatcher-event-status ${granted ? 'is-granted' : ''}">${status}</span></div>
          <div class="dispatcher-event-reason">${escapeHtml(reason)} · ${escapeHtml(event.worker_id || '未指定')}${strategy}${score ? ` · ${escapeHtml(score)}` : ''}</div>
        </div>
        <time>${formatRelative(event.updated_at)}</time>
      </div>
    `;
  }).join('');
}

function renderWorkers(workers, groups) {
  const root = $('dispatcherWorkers');
  if (!workers.length) {
    root.innerHTML = emptyState('◎', '暂无人格 Worker', '启动启用调度器的 Adapter 后会自动注册');
    return;
  }
  const activeByWorker = new Map();
  groups.filter(group => group.active).forEach(group => {
    activeByWorker.set(group.active_worker_id, (activeByWorker.get(group.active_worker_id) || 0) + 1);
  });
  const maxReplies = Math.max(1, ...workers.map(worker => Number(worker.reply_count || 0)));
  root.innerHTML = workers.map(worker => {
    const active = activeByWorker.get(worker.worker_id) || 0;
    const width = Math.min(100, (Number(worker.reply_count || 0) / maxReplies) * 100);
    return `
      <div class="dispatcher-worker-row">
        <span class="dispatcher-worker-dot ${worker.online ? 'is-online' : ''}"></span>
        <div class="dispatcher-worker-identity">
          <strong>${escapeHtml(worker.worker_id)}</strong>
          <span>${worker.account_id ? `QQ ${escapeHtml(worker.account_id)}` : '未绑定 QQ'} · 优先级 ${Number(worker.priority || 0).toFixed(1)}</span>
        </div>
        <div class="dispatcher-worker-load"><span style="width:${width}%"></span></div>
        <div class="dispatcher-worker-count"><strong>${worker.reply_count || 0}</strong><span>回复 · 近5分 ${worker.recent_reply_count || 0}</span></div>
        <div class="dispatcher-worker-active">${active ? `${active} 群占用` : '空闲'}</div>
        <div class="dispatcher-worker-seen">${worker.online ? '在线' : formatRelative(worker.last_seen)}</div>
      </div>
    `;
  }).join('');
}

function emptyState(icon, title, detail) {
  return `<div class="dispatcher-empty"><span class="dispatcher-empty-icon">${icon}</span><strong>${title}</strong><span>${detail}</span></div>`;
}

function emptyTable(text) {
  return `<div class="dispatcher-table-empty">${text}</div>`;
}

function formatPolicyValue(value, unit) {
  if (value === undefined || value === null) return '—';
  if (Array.isArray(value)) return `${value.join(' / ')} ${unit}`;
  return `${Number(value).toLocaleString('zh-CN')} ${unit}`;
}

function formatDuration(seconds) {
  const total = Math.max(0, Math.round(Number(seconds || 0)));
  if (total < 60) return `${total}s`;
  return `${Math.floor(total / 60)}m ${total % 60}s`;
}

function formatRelative(timestamp) {
  const value = Number(timestamp || 0);
  if (!value) return '—';
  const seconds = Math.max(0, Date.now() / 1000 - value);
  if (seconds < 10) return '刚刚';
  if (seconds < 60) return `${Math.floor(seconds)} 秒前`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} 分钟前`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)} 小时前`;
  return new Date(value * 1000).toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' });
}

function shortId(value) {
  const text = String(value || '—');
  return text.length > 14 ? `${text.slice(0, 7)}…${text.slice(-5)}` : text;
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, char => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[char]));
}
