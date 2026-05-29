import { store } from '../store.js';
import { get } from '../app.js';
import { toast, $ } from '../components.js';

let historyData = [];
let skillFilter = '';

export async function init(container) {
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
          <select id="limitSelect" class="btn btn-sm">
            <option value="20">最近 20 条</option>
            <option value="50" selected>最近 50 条</option>
            <option value="100">最近 100 条</option>
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
    </div>
  `;

  $('skillFilter').addEventListener('change', (e) => {
    skillFilter = e.target.value;
    renderHistory();
  });
  $('limitSelect').addEventListener('change', () => loadHistory());
  $('refreshBtn').addEventListener('click', () => loadHistory());

  await loadHistory();
}

async function loadHistory() {
  const name = store.currentPersona;
  const limit = parseInt($('limitSelect').value, 10);
  const params = new URLSearchParams({ limit: String(limit) });
  if (skillFilter) params.set('skill_name', skillFilter);

  try {
    const data = await get(`/personas/${name}/skill-history?${params}`);
    historyData = data.history || [];
    updateSkillFilter();
    renderStats();
    renderHistory();
  } catch (e) {
    toast('加载历史失败: ' + e.message, 'error');
    $('historyList').innerHTML = `<div style="color:var(--danger);padding:12px">加载失败: ${e.message}</div>`;
  }
}

function updateSkillFilter() {
  const skills = [...new Set(historyData.map(h => h.skill_name))].sort();
  const sel = $('skillFilter');
  const current = sel.value;
  sel.innerHTML = `<option value="">全部技能</option>` +
    skills.map(s => `<option value="${s}"${s === current ? ' selected' : ''}>${s}</option>`).join('');
}

function renderStats() {
  const total = historyData.length;
  const success = historyData.filter(h => h.success).length;
  const failed = total - success;
  const avgDuration = total > 0
    ? Math.round(historyData.reduce((sum, h) => sum + (h.duration_ms || 0), 0) / total)
    : 0;
  const successRate = total > 0 ? Math.round(success / total * 100) : 0;

  $('statsGrid').innerHTML = `
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
  const filtered = skillFilter
    ? historyData.filter(h => h.skill_name === skillFilter)
    : historyData;
  const el = $('historyList');

  if (!filtered.length) {
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
      ${filtered.map(h => {
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
