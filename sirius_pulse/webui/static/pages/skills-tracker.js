import { store } from '../store.js';
import { get } from '../app.js';
import { toast } from '../components.js';
import { createRealtimePoller } from './realtime.js';
import { createScopedPage } from '../page-context.js';

const scopedPage = createScopedPage();
const $ = scopedPage.$;

let historyData = [];
let skillFilter = '';
let successFilter = '';
let currentPage = 0;
let totalRecords = 0;
const PAGE_SIZE = 50;
const poller = createRealtimePoller(() => {
  if (currentPage === 0) return loadHistory(true);
  return Promise.resolve();
}, 3000);

export function dispose() {
  poller.stop();
}

export async function init(container, params = {}) {
  scopedPage.use(params?.ctx, container);
  const name = store.currentPersona;
  if (!name) {
    container.innerHTML = `
      <div class="card">
        <div style="padding:60px;text-align:center;color:var(--text-3)">
          <div style="font-size:48px;margin-bottom:16px">⟠</div>
          <div style="font-size:16px;margin-bottom:8px">请先选择人格</div>
        </div>
      </div>
    `;
    return;
  }

  currentPage = 0;
  totalRecords = 0;

  container.innerHTML = `
    <div class="card" style="margin-bottom:20px">
      <div class="card-header">
        <div>
          <div class="card-title">Skill 调用追踪</div>
          <div class="card-subtitle">查看 ${name} 的技能执行历史</div>
        </div>
        <div style="display:flex;gap:12px;align-items:center">
          <select id="skillFilter" class="btn btn-sm">
            <option value="">全部技能</option>
          </select>
          <select id="successFilter" class="btn btn-sm">
            <option value="">全部状态</option>
            <option value="true">成功</option>
            <option value="false">失败</option>
          </select>
          <button class="btn btn-sm" id="refreshBtn">刷新</button>
        </div>
      </div>
      <div class="stat-grid" id="statsGrid"></div>
    </div>
    <div class="card">
      <div id="historyList" style="padding:16px">
        <div style="color:var(--text-3)">加载中...</div>
      </div>
      <div id="pagination" style="display:flex;justify-content:space-between;align-items:center;padding:12px 16px;border-top:1px solid var(--border-1)"></div>
    </div>
  `;

  const skillFilterEl = $('skillFilter');
  if (skillFilterEl) {
    skillFilterEl.addEventListener('change', (e) => {
      skillFilter = e.target.value;
      currentPage = 0;
      loadHistory();
    });
  }

  const successFilterEl = $('successFilter');
  if (successFilterEl) {
    successFilterEl.addEventListener('change', (e) => {
      successFilter = e.target.value;
      currentPage = 0;
      loadHistory();
    });
  }

  const refreshBtn = $('refreshBtn');
  if (refreshBtn) {
    refreshBtn.addEventListener('click', () => loadHistory());
  }

  await loadHistory();
  poller.start();
}

async function loadHistory(silent = false) {
  const name = store.currentPersona;
  const params = new URLSearchParams({
    limit: String(PAGE_SIZE),
    offset: String(currentPage * PAGE_SIZE),
  });
  if (skillFilter) params.set('skill_name', skillFilter);
  if (successFilter) params.set('success', successFilter);

  try {
    const data = await get(`/persona/skill-history?${params}`);
    historyData = data.history || [];
    totalRecords = data.total || 0;
    renderStats(data.stats || {});
    updateSkillFilter();
    renderHistory();
    renderPagination();
  } catch (e) {
    if (silent) {
      console.warn('skill history realtime refresh failed:', e);
      return;
    }
    toast('加载历史失败: ' + e.message, 'error');
    const historyList = $('historyList');
    if (historyList) {
      historyList.innerHTML = `<div style="color:var(--danger);padding:12px">加载失败: ${e.message}</div>`;
    }
  }
}

let knownSkills = [];

function updateSkillFilter() {
  const pageSkills = historyData.map(h => h.skill_name);
  for (const s of pageSkills) {
    if (!knownSkills.includes(s)) knownSkills.push(s);
  }
  knownSkills.sort();

  const sel = $('skillFilter');
  if (!sel) return;
  const current = sel.value;
  sel.innerHTML = `<option value="">全部技能</option>` +
    knownSkills.map(s => `<option value="${s}"${s === current ? ' selected' : ''}>${s}</option>`).join('');
}

function renderStats(stats) {
  let total = 0, success = 0, totalMs = 0;
  for (const s of Object.values(stats)) {
    total += s.calls || 0;
    success += s.successes || 0;
    totalMs += s.total_ms || 0;
  }
  const failed = total - success;
  const avgDuration = total > 0 ? Math.round(totalMs / total) : 0;
  const successRate = total > 0 ? Math.round(success / total * 100) : 0;

  const statsGrid = $('statsGrid');
  if (!statsGrid) return;

  statsGrid.innerHTML = `
    <div class="stat-card">
      <div class="stat-label">总调用次数</div>
      <div class="stat-value">${total}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">成功</div>
      <div class="stat-value" style="color:var(--success)">${success}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">失败</div>
      <div class="stat-value" style="color:var(--danger)">${failed}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">成功率</div>
      <div class="stat-value">${successRate}%</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">平均耗时</div>
      <div class="stat-value">${avgDuration}ms</div>
    </div>
  `;
}

function renderPagination() {
  const el = $('pagination');
  if (!el) return;

  const totalPages = Math.max(1, Math.ceil(totalRecords / PAGE_SIZE));
  const isFirst = currentPage === 0;
  const isLast = currentPage >= totalPages - 1;

  el.innerHTML = `
    <span style="font-size:12px;color:var(--text-3)">
      共 ${totalRecords} 条，第 ${currentPage + 1}/${totalPages} 页
    </span>
    <div style="display:flex;gap:8px">
      <button class="btn btn-sm" id="prevPage" ${isFirst ? 'disabled' : ''}>上一页</button>
      <button class="btn btn-sm" id="nextPage" ${isLast ? 'disabled' : ''}>下一页</button>
    </div>
  `;

  const prevBtn = $('prevPage');
  const nextBtn = $('nextPage');
  if (prevBtn) prevBtn.addEventListener('click', () => { currentPage--; loadHistory(); });
  if (nextBtn) nextBtn.addEventListener('click', () => { currentPage++; loadHistory(); });
}

function formatTime(ts) {
  if (!ts) return '—';
  try {
    const date = new Date(ts * 1000);
    return date.toLocaleString('zh-CN');
  } catch {
    return '—';
  }
}

function renderHistory() {
  const el = $('historyList');
  if (!el) return;

  if (!historyData.length) {
    el.innerHTML = `
      <div style="padding:40px;text-align:center;color:var(--text-3)">
        <div style="font-size:36px;margin-bottom:12px">⟠</div>
        <div>暂无调用记录</div>
      </div>
    `;
    return;
  }

  el.innerHTML = `
    <div style="display:grid;gap:12px">
      ${historyData.map(h => {
        const statusColor = h.success ? 'var(--success)' : 'var(--danger)';
        const statusIcon = h.success ? '✓' : '✗';
        const summary = h.result_summary || h.error || '';
        const params = h.params || null;

        return `
          <div style="padding:16px;background:var(--bg-1);border-radius:8px;border-left:3px solid ${statusColor}">
            <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:8px">
              <div style="display:flex;align-items:center;gap:8px">
                <span style="color:${statusColor};font-weight:600">${statusIcon}</span>
                <span style="font-weight:600;font-size:14px">${h.skill_name}</span>
                ${h.caller_user_id ? `<span class="tag" style="font-size:11px">${h.caller_user_id}</span>` : ''}
              </div>
              <div style="display:flex;align-items:center;gap:12px;font-size:12px;color:var(--text-3)">
                <span>${h.duration_ms || 0}ms</span>
                <span>${formatTime(h.timestamp)}</span>
              </div>
            </div>
            ${params ? `<div style="margin-bottom:6px">${renderParams(params)}</div>` : ''}
            ${summary ? `<div style="font-size:12px;color:var(--text-2)"><strong>结果:</strong> ${truncate(summary, 200)}</div>` : ''}
          </div>
        `;
      }).join('')}
    </div>
  `;
}

function renderParams(params) {
  if (!params || typeof params !== 'object' || !Object.keys(params).length) return '';
  const entries = Object.entries(params);
  return `
    <div style="font-size:12px;color:var(--text-2);margin-bottom:2px"><strong>调用参数</strong></div>
    <div style="display:flex;flex-wrap:wrap;gap:6px">
      ${entries.map(([k, v]) => {
        const val = formatParamValue(v);
        return `<span style="display:inline-flex;align-items:center;gap:4px;padding:2px 8px;background:var(--bg-2);border-radius:4px;font-size:11px;border:1px solid var(--border-1)">
          <span style="color:var(--accent);font-weight:500">${escapeHtml(k)}</span>
          <span style="color:var(--text-1)">${escapeHtml(val)}</span>
        </span>`;
      }).join('')}
    </div>
  `;
}

function formatParamValue(v) {
  if (v === null || v === undefined) return 'null';
  if (typeof v === 'object') {
    const s = JSON.stringify(v);
    return s.length > 80 ? s.slice(0, 80) + '...' : s;
  }
  const s = String(v);
  return s.length > 100 ? s.slice(0, 100) + '...' : s;
}

function escapeHtml(str) {
  const s = String(str);
  return s.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function truncate(str, max) {
  if (!str) return '';
  return str.length > max ? str.slice(0, max) + '...' : str;
}
