import { get, post } from '../app.js';
import { toast, flashSuccess } from '../components.js';
import { createScopedPage } from '../page-context.js';

/**
 * AMKR 运维页。
 *
 * 本页**只做巡检与登记**，不做模型或采样参数编排：任务指向哪个模型、用什么
 * temperature，全部由运维在 AMKR 自带 WebUI 里配置。这里回答两个问题——连得上
 * 吗、任务名登记齐了吗——并提供跳转 AMKR 的外链。
 */

const scopedPage = createScopedPage();

export function dispose() {
  scopedPage.use(null, null);
}
const $ = scopedPage.$;

function escapeHtml(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

export async function init(container, params = {}) {
  scopedPage.use(params?.ctx, container);
  container.innerHTML = `
    <div class="card">
      <div class="card-header">
        <div>
          <div class="card-title">AMKR 连接</div>
          <div class="card-subtitle">所有模型调用都经由 AMKR，供应商与 Key 池在 AMKR 侧维护</div>
        </div>
        <div style="display:flex;gap:8px">
          <button type="button" class="btn btn-sm" id="amkrRefreshBtn">刷新</button>
          <button type="button" class="btn btn-sm btn-primary" id="amkrRegisterBtn">注册任务名</button>
        </div>
      </div>
      <div id="amkrOverview"><div class="card-subtitle">读取中…</div></div>
    </div>
    <div class="card" style="margin-top:16px">
      <div class="card-header">
        <div>
          <div class="card-title">任务登记</div>
          <div class="card-subtitle">本框架只创建缺失的任务名，已存在的任务不会被动到</div>
        </div>
      </div>
      <div id="amkrWorkspaces"></div>
    </div>
  `;

  scopedPage.on($('amkrRefreshBtn'), 'click', () => loadStatus());
  scopedPage.on($('amkrRegisterBtn'), 'click', (event) => registerTasks(event.currentTarget));

  await loadStatus();
}

async function loadStatus() {
  const overview = $('amkrOverview');
  const workspaces = $('amkrWorkspaces');
  try {
    const data = await get('/amkr/status');
    renderOverview(overview, data);
    renderWorkspaces(workspaces, data);
  } catch (error) {
    if (error?.name === 'AbortError') return;
    overview.innerHTML = '<div class="card-subtitle">读取 AMKR 状态失败</div>';
    workspaces.innerHTML = '';
    toast('读取 AMKR 状态失败', 'error');
  }
}

function renderOverview(root, data) {
  if (!root) return;
  if (!data.configured) {
    root.innerHTML = `
      <div class="card-subtitle" style="color:var(--danger,#e5534b)">
        尚未配置 AMKR 本地授权 Key。请先在「全局设置」里填写，再回到本页。
      </div>
    `;
    return;
  }

  const rows = [
    ['连接地址', escapeHtml(data.base_url || '—')],
    ['状态', data.reachable
      ? '<span class="tag tag-success">已连接</span>'
      : `<span class="tag tag-danger">不可达</span>${data.error ? ` ${escapeHtml(data.error)}` : ''}`],
    ['版本', escapeHtml(data.version || '—')],
    ['工作空间前缀', escapeHtml(data.workspace_base || '—')],
    ['访问方式', `<code>X-AMKR-Workspace: ${escapeHtml(data.workspace_base)}/&lt;人格&gt;</code>`],
    ['运维接口', data.ops_enabled
      ? '<span class="tag tag-success">已启用</span>'
      : '<span class="tag tag-danger">已关闭（--no-ops）</span>'],
  ];

  const link = data.reachable && data.ui_url
    ? `<a class="btn btn-sm" href="${escapeHtml(data.ui_url)}" target="_blank" rel="noopener">打开 AMKR 运维台 ↗</a>`
    : '';

  root.innerHTML = `
    <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px">
      ${rows.map(([label, value]) => `
        <div>
          <div class="card-subtitle">${label}</div>
          <div>${value}</div>
        </div>
      `).join('')}
    </div>
    <div style="margin-top:16px">${link}</div>
  `;
}

function renderWorkspaces(root, data) {
  if (!root) return;
  const known = Array.isArray(data.known_tasks) ? data.known_tasks : [];
  const items = Array.isArray(data.workspaces) ? data.workspaces : [];

  const knownBlock = `
    <div class="card-subtitle" style="margin-bottom:12px">
      本框架定义 ${known.length} 个任务名：
      ${known.map(name => `<span class="tag">${escapeHtml(name)}</span>`).join(' ')}
    </div>
  `;

  if (!items.length) {
    root.innerHTML = `${knownBlock}<div class="card-subtitle">还没有人格，无法登记任务。</div>`;
    return;
  }

  root.innerHTML = knownBlock + items.map(item => {
    const total = (item.registered?.length || 0) + (item.missing?.length || 0);
    const badge = item.error
      ? `<span class="tag tag-danger">读取失败</span>`
      : item.missing?.length
        ? `<span class="tag tag-danger">缺 ${item.missing.length} 项</span>`
        : `<span class="tag tag-success">已齐备</span>`;
    const missing = (item.missing || [])
      .map(name => `<span class="tag tag-danger">${escapeHtml(name)}</span>`)
      .join(' ');
    const errorLine = item.error
      ? `<div class="card-subtitle" style="color:var(--danger,#e5534b)">${escapeHtml(item.error)}</div>`
      : '';
    return `
      <div style="padding:12px 0;border-top:1px solid var(--border,#333)">
        <div style="display:flex;justify-content:space-between;align-items:center;gap:12px">
          <div>
            <div><strong>${escapeHtml(item.persona || '(默认)')}</strong> ${badge}</div>
            <div class="card-subtitle">工作空间 <code>${escapeHtml(item.workspace || '')}</code> · ${total} 个任务</div>
          </div>
          <button type="button" class="btn btn-sm" data-amkr-persona="${escapeHtml(item.persona || '')}">注册缺失项</button>
        </div>
        ${errorLine}
        ${missing ? `<div style="margin-top:8px">${missing}</div>` : ''}
      </div>
    `;
  }).join('');

  scopedPage.$$('[data-amkr-persona]').forEach(button => {
    scopedPage.on(button, 'click', () => registerTasks(button, button.dataset.amkrPersona));
  });
}

async function registerTasks(button, persona = '') {
  if (button?.disabled) return;
  if (button) button.disabled = true;
  try {
    const body = persona ? { persona } : {};
    const data = await post('/amkr/register', body);
    const results = data?.results || {};
    const failures = Object.entries(results).filter(([, value]) => value?.error);
    const created = Object.values(results)
      .reduce((sum, value) => sum + (value?.created?.length || 0), 0);

    if (failures.length) {
      toast(`注册失败：${failures.map(([name]) => name).join('、')}`, 'error');
    } else if (created === 0) {
      toast('任务名已齐备，无需新建');
      if (button) flashSuccess(button);
    } else {
      toast(`已新建 ${created} 个任务名`);
      if (button) flashSuccess(button);
    }
    await loadStatus();
  } catch (error) {
    if (error?.name !== 'AbortError') toast('注册任务名失败', 'error');
  } finally {
    if (button) button.disabled = false;
  }
}
