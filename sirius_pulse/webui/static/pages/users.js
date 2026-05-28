import { store } from '../store.js';
import { get } from '../app.js';
import { toast, $ } from '../components.js';

let usersData = null;
let activeGroup = '';

export async function init(container) {
  container.innerHTML = `
    <div class="card">
      <div class="card-header">
        <div class="card-title">用户画像</div>
        <div style="display:flex;gap:12px;align-items:center">
          <select id="usersGroupFilter" class="btn btn-sm">
            <option value="">全部群组</option>
          </select>
        </div>
      </div>
      <div class="stat-grid" id="usersStats"></div>
    </div>
    <div id="usersGrid" style="display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:16px;margin-top:20px"></div>
  `;

  $('usersGroupFilter').addEventListener('change', (e) => {
    activeGroup = e.target.value;
    loadData();
  });

  await loadData();
}

async function loadData() {
  const name = store.currentPersona;
  if (!name) {
    toast('请先选择一个人格', 'error');
    return;
  }
  try {
    const params = activeGroup ? `?group_id=${encodeURIComponent(activeGroup)}` : '';
    usersData = await get(`/personas/${name}/users${params}`);
    renderStats();
    renderGroups();
    renderCards();
  } catch (e) {
    toast('加载用户数据失败', 'error');
  }
}

function renderStats() {
  const users = usersData.users || [];
  const groups = usersData.groups || [];
  $('usersStats').innerHTML = `
    <div class="stat-card">
      <div class="stat-label">用户总数</div>
      <div class="stat-value">${users.length}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">群组数量</div>
      <div class="stat-value">${groups.length}</div>
    </div>
  `;
}

function renderGroups() {
  const groups = usersData.groups || [];
  const sel = $('usersGroupFilter');
  sel.innerHTML = `<option value="">全部群组</option>` +
    groups.map(g => `<option value="${g}"${g === activeGroup ? ' selected' : ''}>${g}</option>`).join('');
}

function hashColor(str) {
  let hash = 0;
  for (let i = 0; i < str.length; i++) {
    hash = str.charCodeAt(i) + ((hash << 5) - hash);
  }
  const h = Math.abs(hash) % 360;
  return `hsl(${h}, 55%, 55%)`;
}

function formatDate(iso) {
  if (!iso) return '—';
  try {
    return new Date(iso).toLocaleDateString('zh-CN');
  } catch {
    return '—';
  }
}

function engagementColor(rate) {
  const pct = Math.round(rate * 100);
  if (pct >= 70) return 'var(--success)';
  if (pct >= 40) return 'var(--accent)';
  return 'var(--danger)';
}

function calcFamiliarity(count) {
  return Math.log(count + 1) / Math.log(51);
}

function renderCards() {
  const users = usersData.users || [];
  const grid = $('usersGrid');

  if (!users.length) {
    grid.innerHTML = `
      <div class="card" style="grid-column:1/-1">
        <div style="color:var(--text-3);padding:40px;text-align:center">暂无用户数据</div>
      </div>
    `;
    return;
  }

  grid.innerHTML = users.map(u => {
    const pct = Math.round((u.engagement_rate || 0) * 100);
    const color = engagementColor(u.engagement_rate || 0);
    const fam = calcFamiliarity(u.interaction_count || 0);
    const famPct = Math.min(100, Math.round(fam * 100));
    const avatarLetter = (u.name || u.user_id || '?')[0];
    const avatarBg = hashColor(u.user_id || 'x');

    return `
      <div class="card">
        <div style="display:flex;align-items:center;gap:14px;margin-bottom:16px">
          <div style="width:48px;height:48px;border-radius:50%;background:${avatarBg};display:flex;align-items:center;justify-content:center;font-size:20px;font-weight:700;color:#fff;flex-shrink:0">${avatarLetter}</div>
          <div style="min-width:0">
            <div style="font-weight:600;font-size:15px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${u.name || '未知'}</div>
            <div style="font-size:12px;color:var(--text-3);font-family:var(--font-mono)">${u.user_id || '—'}</div>
          </div>
        </div>
        <div style="display:flex;gap:16px;font-size:12px;color:var(--text-2);margin-bottom:14px">
          <div>交互 <strong>${(u.interaction_count || 0).toLocaleString()}</strong> 次</div>
          <div>最近 ${formatDate(u.last_interaction_at)}</div>
        </div>
        <div style="margin-bottom:12px">
          <div style="display:flex;justify-content:space-between;font-size:12px;margin-bottom:4px">
            <span style="color:var(--text-3)">互动率</span>
            <span style="color:${color};font-weight:600">${pct}%</span>
          </div>
          <div style="height:6px;border-radius:3px;background:var(--border);overflow:hidden">
            <div style="height:100%;width:${pct}%;background:${color};border-radius:3px;transition:width 0.4s"></div>
          </div>
        </div>
        <div>
          <div style="display:flex;justify-content:space-between;font-size:12px;margin-bottom:4px">
            <span style="color:var(--text-3)">熟悉度</span>
            <span style="color:var(--text-2);font-weight:600">${famPct}%</span>
          </div>
          <div style="height:6px;border-radius:3px;background:var(--border);overflow:hidden">
            <div style="height:100%;width:${famPct}%;background:var(--accent);border-radius:3px;transition:width 0.4s"></div>
          </div>
        </div>
      </div>
    `;
  }).join('');
}
