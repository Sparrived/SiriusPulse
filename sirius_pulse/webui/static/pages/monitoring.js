import { store } from '../store.js';
import { get, post } from '../app.js';
import { toast, flashSuccess, $ } from '../components.js';

export async function init(container) {
  container.innerHTML = `
    <div class="stat-grid" id="monitorStats"></div>
    <div class="card" style="margin-top:20px">
      <div class="card-header">
        <div>
          <div class="card-title">人格实例监控</div>
          <div class="card-subtitle">点击卡片查看详细指标</div>
        </div>
      </div>
      <div id="personaMonitorGrid"></div>
    </div>
  `;

  await loadOverview();
}

async function loadOverview() {
  const statsEl = $('monitorStats');
  const gridEl = $('personaMonitorGrid');

  try {
    const res = await get('/monitoring/overview');
    const total = res.total_personas || 0;
    const running = res.running_personas || 0;
    const stopped = total - running;
    const personas = res.personas || [];

    statsEl.innerHTML = `
      <div class="stat-card">
        <div class="stat-label">◎ 人格总数</div>
        <div class="stat-value">${total}</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">◉ 运行中</div>
        <div class="stat-value" style="color:var(--success)">${running}</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">◌ 已停止</div>
        <div class="stat-value" style="color:var(--text-3)">${stopped}</div>
      </div>
    `;

    if (!personas.length) {
      gridEl.innerHTML = '<div style="padding:24px;text-align:center;color:var(--text-3)">暂无人格实例</div>';
      return;
    }

    gridEl.style.cssText = 'display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:16px;padding:12px 0';

    gridEl.innerHTML = personas.map(p => {
      const uptimeStr = formatUptime(p.uptime_seconds);
      return `
        <div class="card" style="cursor:pointer" data-name="${p.name}">
          <div style="padding:16px">
            <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
              <span style="font-size:16px;font-weight:600">${p.name}</span>
              <span style="font-size:12px;padding:2px 8px;border-radius:4px;background:${p.running ? 'rgba(var(--success-rgb,34,197,94),0.15)' : 'rgba(var(--text-3-rgb,156,163,175),0.15)'};color:${p.running ? 'var(--success)' : 'var(--text-3)'}">
                ${p.running ? '运行中' : '已停止'}
              </span>
            </div>
            <div style="display:grid;grid-template-columns:1fr 1fr;gap:8px;font-size:13px">
              <div>
                <span style="color:var(--text-3)">PID</span>
                <span style="float:right;font-family:var(--font-mono)">${p.pid || '—'}</span>
              </div>
              <div>
                <span style="color:var(--text-3)">运行时长</span>
                <span style="float:right;font-family:var(--font-mono)">${uptimeStr}</span>
              </div>
            </div>
          </div>
        </div>
      `;
    }).join('');

    gridEl.querySelectorAll('.card[data-name]').forEach(card => {
      card.addEventListener('click', () => {
        navTo('monitoring-detail', card.dataset.name);
      });
    });
  } catch {
    statsEl.innerHTML = '<div style="color:var(--danger);padding:12px">加载监控概览失败</div>';
    gridEl.innerHTML = '';
  }
}

function formatUptime(seconds) {
  if (!seconds || seconds <= 0) return '—';
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (h > 0) return `${h}时${m}分`;
  return `${m}分`;
}
