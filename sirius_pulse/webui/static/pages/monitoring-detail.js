import { store } from '../store.js';
import { get, post } from '../app.js';
import { toast, flashSuccess, $ } from '../components.js';

const HEALTH_ICON = { ok: '✅', down: '❌', warning: '⚠️' };

export async function init(container, params) {
  const name = params?.name || store.currentPersona;
  if (!name) {
    container.innerHTML = `
      <div class="card">
        <div style="padding:40px;text-align:center;color:var(--text-3)">
          <div style="font-size:48px;margin-bottom:16px">✦</div>
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
          <div class="card-title">${name}</div>
          <div class="card-subtitle">监控详情</div>
        </div>
      </div>
    </div>
    <div class="stat-grid" id="metricsStats"></div>
    <div class="card" style="margin-top:20px">
      <div class="card-header">
        <div class="card-title">健康检查</div>
      </div>
      <div id="healthChecks" style="padding:16px"></div>
    </div>
  `;

  await Promise.all([loadMetrics(name), loadHealth(name)]);
}

async function loadMetrics(name) {
  const el = $('metricsStats');
  try {
    const res = await get(`/monitoring/${name}/metrics`);
    const token = res.token_usage || {};
    const memory = res.memory || {};
    const cognition = res.cognition || {};
    const uptimeStr = formatUptime(res.uptime_seconds);

    el.innerHTML = `
      <div class="stat-card">
        <div class="stat-label">▸ Token 输入</div>
        <div class="stat-value text-mono">${(token.total_input || 0).toLocaleString()}</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">◂ Token 输出</div>
        <div class="stat-value text-mono">${(token.total_output || 0).toLocaleString()}</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">△ 调用次数</div>
        <div class="stat-value text-mono">${(token.call_count || 0).toLocaleString()}</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">◫ 日记条目</div>
        <div class="stat-value text-mono">${(memory.diary_count || 0).toLocaleString()}</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">◱ 名词解释</div>
        <div class="stat-value text-mono">${(memory.glossary_count || 0).toLocaleString()}</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">◐ 用户画像</div>
        <div class="stat-value text-mono">${(memory.user_count || 0).toLocaleString()}</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">◎ 认知事件</div>
        <div class="stat-value text-mono">${(cognition.event_count || 0).toLocaleString()}</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">◷ 运行时长</div>
        <div class="stat-value">${uptimeStr}</div>
      </div>
    `;
  } catch {
    el.innerHTML = '<div style="color:var(--danger);padding:12px">指标加载失败</div>';
  }
}

async function loadHealth(name) {
  const el = $('healthChecks');
  try {
    const res = await get(`/monitoring/${name}/health`);
    const checks = res.checks || {};
    const overall = res.healthy;

    el.innerHTML = `
      <div style="margin-bottom:16px;font-size:15px;font-weight:600">
        总体状态: <span style="color:${overall ? 'var(--success)' : 'var(--danger)'}">${overall ? '✅ 健康' : '❌ 异常'}</span>
      </div>
      <div style="display:grid;gap:12px">
        ${renderCheckItem('进程', checks.process)}
        ${renderCheckItem('配置文件', checks.config)}
        ${renderCheckItem('记忆系统', checks.memory)}
      </div>
    `;
  } catch {
    el.innerHTML = '<div style="color:var(--danger);padding:12px">健康检查加载失败</div>';
  }
}

function renderCheckItem(label, check) {
  if (!check) return '';
  const icon = HEALTH_ICON[check.status] || '⚠️';
  const detail = check.status === 'down' && check.pid !== undefined
    ? `PID: ${check.pid || '无'}`
    : check.files && check.files.length
      ? `缺失: ${check.files.join(', ')}`
      : check.status === 'ok' ? '正常' : check.status;

  return `
    <div style="display:flex;justify-content:space-between;align-items:center;padding:12px 16px;background:var(--surface-2,rgba(255,255,255,0.03));border-radius:8px">
      <div style="display:flex;align-items:center;gap:10px">
        <span style="font-size:18px">${icon}</span>
        <span style="font-weight:500">${label}</span>
      </div>
      <span style="font-size:13px;color:var(--text-2)">${detail}</span>
    </div>
  `;
}

function formatUptime(seconds) {
  if (!seconds || seconds <= 0) return '—';
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (h > 24) {
    const d = Math.floor(h / 24);
    return `${d}天${h % 24}时`;
  }
  if (h > 0) return `${h}时${m}分`;
  return `${m}分`;
}
