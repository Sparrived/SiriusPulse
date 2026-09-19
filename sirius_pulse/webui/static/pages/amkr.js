import { get, post } from '../app.js';
import { toast, flashSuccess, confirmDanger } from '../components.js';
import { createScopedPage } from '../page-context.js';

/**
 * AMKR 运维页。
 *
 * 本页**只做巡检与登记**，不做模型或采样参数编排：任务指向哪个模型、用什么
 * temperature，全部由运维在 AMKR 自带 WebUI 里配置。这里回答——连得上吗、任务名
 * 登记齐了吗、各空间有没有推理凭据——并提供跳转 AMKR 的外链。
 *
 * 「推理凭据」一栏是本页的存在理由之一：模型调用用的是钉死在单个空间上的推理
 * key，不是全局管理员 key。空间若建于 AMKR 支持该能力之前，本地只有面板 key，
 * 而 key 拿不回来了，只能在这里轮换补上。缺凭据的人格**起不来**，所以这一栏必须
 * 能一眼看出，并给出补救按钮。
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
      <div id="amkrKeyReveal" style="margin-top:12px"></div>
    </div>
    <div class="card" style="margin-top:16px">
      <div class="card-header">
        <div>
          <div class="card-title">工作空间面板</div>
          <div class="card-subtitle">
            内嵌的是 AMKR 自带面板，只能看到所选人格自己的空间：用量读数与任务增删改都在 AMKR 侧完成
          </div>
        </div>
        <div style="display:flex;gap:8px;align-items:center">
          <select id="amkrPanelPersona" class="btn btn-sm" aria-label="选择人格"></select>
          <button type="button" class="btn btn-sm btn-primary" id="amkrPanelBtn">打开面板</button>
        </div>
      </div>
      <div id="amkrPanelHint" class="card-subtitle" style="margin-top:8px">
        面板按需加载，点「打开面板」后才会向 AMKR 取地址。
      </div>
      <div id="amkrPanelFrame" style="margin-top:12px"></div>
    </div>
  `;

  scopedPage.on($('amkrRefreshBtn'), 'click', () => loadStatus());
  scopedPage.on($('amkrRegisterBtn'), 'click', (event) => registerTasks(event.currentTarget));
  scopedPage.on($('amkrPanelBtn'), 'click', (event) => openPanel(event.currentTarget));

  await loadStatus();
}

async function loadStatus() {
  const overview = $('amkrOverview');
  const workspaces = $('amkrWorkspaces');
  try {
    const data = await get('/amkr/status');
    renderOverview(overview, data);
    renderWorkspaces(workspaces, data);
    renderPanelPersonas(data);
  } catch (error) {
    if (error?.name === 'AbortError') return;
    overview.innerHTML = '<div class="card-subtitle">读取 AMKR 状态失败</div>';
    workspaces.innerHTML = '';
    toast('读取 AMKR 状态失败', 'error');
  }
}

function renderPanelPersonas(data) {
  const select = $('amkrPanelPersona');
  if (!select) return;
  const items = Array.isArray(data.workspaces) ? data.workspaces : [];
  const previous = select.value;
  select.innerHTML = items
    .map(item => `<option value="${escapeHtml(item.persona || '')}">${escapeHtml(item.persona || '(默认)')}</option>`)
    .join('');
  if (previous && items.some(item => item.persona === previous)) select.value = previous;

  // 面板是浏览器直接去连 AMKR 的：若地址是回环，只有从服务器本机打开本页才连得上。
  const hint = $('amkrPanelHint');
  // 面板是**浏览器**去连 AMKR 的，因此这里看的是浏览器侧地址，而不是容器自己
  // 用的那个（同机部署时后端常走回环，但那在用户浏览器里指向用户自己的机器）。
  const browserBase = data.browser_base_url || data.base_url || '';
  if (hint && /^https?:\/\/(127\.0\.0\.1|localhost|\[::1\])(:|\/|$)/i.test(browserBase)) {
    hint.textContent =
      '注意：浏览器侧 AMKR 地址是回环地址，只有从部署本框架的机器上打开本页才能加载面板；'
      + '远程访问请把 AMKR 暴露在同一域名下的路径（反向代理），并在全局设置里填「AMKR 浏览器地址」。';
  }
}

/**
 * 取面板地址并把它塞进 iframe。
 *
 * 地址（fragment 里是明文面板 key）只从后端的管理员接口拿，绝不写进本页源码；
 * 也因此这里按需加载——不必每次打开运维页都把凭据取回来。
 */
async function openPanel(button) {
  const persona = $('amkrPanelPersona')?.value || '';
  const hint = $('amkrPanelHint');
  const frame = $('amkrPanelFrame');
  if (!persona || !frame) return;
  if (button?.disabled) return;
  if (button) button.disabled = true;
  try {
    const data = await get(`/amkr/panel?persona=${encodeURIComponent(persona)}`);
    frame.innerHTML = `
      <iframe
        src="${escapeHtml(data.url)}"
        title="AMKR 工作空间面板 · ${escapeHtml(persona)}"
        referrerpolicy="no-referrer"
        style="width:100%;height:720px;border:0;border-radius:8px;background:var(--surface-2)"
      ></iframe>
    `;
    if (hint) hint.textContent = `正在显示「${persona}」工作空间的面板。`;
  } catch (error) {
    if (error?.name === 'AbortError') return;
    frame.innerHTML = '';
    if (hint) hint.textContent = `无法打开面板：${error?.message || '未知错误'}`;
    toast('打开工作空间面板失败', 'error');
  } finally {
    if (button) button.disabled = false;
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
    ['模型调用凭据', '<code>amkr_ik_…</code> 推理 key（按空间）'],
    ['管理凭据', '<code>amkr_local_api_key</code>（仅建空间/注册任务）'],
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
    const panelLine = item.panel_ready
      ? ''
      : '<div class="card-subtitle">尚无面板 key（AMKR 只在建空间时返回一次）</div>';
    // 推理 key 缺失会让该人格**起不来**（provider 拿不到凭据），因此这条比面板 key
    // 更要紧，排在上面并给出补救按钮。
    const inferenceLine = item.inference_ready
      ? ''
      : `<div class="card-subtitle" style="color:var(--danger,#e5534b)">
           尚无推理 key，该人格的模型调用无法发起。
           <button type="button" class="btn btn-sm" style="margin-left:8px"
                   data-amkr-rotate="${escapeHtml(item.persona || '')}">轮换推理 key</button>
         </div>`;
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
        ${inferenceLine}
        ${panelLine}
        ${missing ? `<div style="margin-top:8px">${missing}</div>` : ''}
      </div>
    `;
  }).join('');

  scopedPage.$$('[data-amkr-persona]').forEach(button => {
    scopedPage.on(button, 'click', () => registerTasks(button, button.dataset.amkrPersona));
  });
  scopedPage.$$('[data-amkr-rotate]').forEach(button => {
    scopedPage.on(button, 'click', () => rotateInferenceKey(button, button.dataset.amkrRotate));
  });
}

/**
 * 轮换某人格的推理 key，并把新 key 显示出来。
 *
 * 旧 key 立即失效，新 key 明文只回这一次——所以必须当场显示，并提示要存好。
 * 后端轮换完会通知该人格重建 provider，因此不需要用户再手动重启。
 */
async function rotateInferenceKey(button, persona) {
  if (button?.disabled) return;
  if (!persona) return;
  const confirmed = confirmDanger(
    `确定为「${persona}」轮换推理 key 吗？\n\n`
    + '旧 key 会立即失效。本框架会立刻换用新 key，但若还有别处（如监控脚本）在用这把 key，'
    + '它们会开始收到 401。'
  );
  if (!confirmed) return;
  if (button) button.disabled = true;
  try {
    const data = await post('/amkr/rotate-inference-key', { persona });
    const key = data?.inference_key || '';
    if (key) {
      revealKey(persona, key);
    }
    toast(`已轮换「${persona}」的推理 key`);
    await loadStatus();
  } catch (error) {
    if (error?.name !== 'AbortError') {
      toast(`轮换推理 key 失败：${error?.message || '未知错误'}`, 'error');
    }
  } finally {
    if (button) button.disabled = false;
  }
}

/**
 * 就地展示一把只回一次的新 key。
 *
 * 不用 ``window.prompt``：它是单行输入框，没有可靠的全选，用户误点确定就永久丢了
 * 这把 key（AMKR 不再重发）。这里给一个可全选的只读框加复制按钮，并留在页面上直到
 * 下一次操作——丢了就只能再轮换一次，而那会作废刚发出去的这把。
 */
function revealKey(persona, key) {
  const root = $('amkrKeyReveal');
  if (!root) return;
  root.innerHTML = `
    <div style="padding:12px;border:1px solid var(--border,#333);border-radius:8px">
      <div><strong>「${escapeHtml(persona)}」的新推理 key</strong>
        <span class="tag tag-danger">只显示这一次</span></div>
      <div class="card-subtitle" style="margin:4px 0 8px">
        旧 key 已失效。请立即存进该人格的配置或密钥管理；离开本页后 AMKR 与本站都不再返回它。
      </div>
      <div style="display:flex;gap:8px;align-items:center">
        <input id="amkrKeyValue" class="btn btn-sm" readonly
               style="flex:1;font-family:monospace;text-align:left"
               value="${escapeHtml(key)}" />
        <button type="button" class="btn btn-sm btn-primary" id="amkrKeyCopy">复制</button>
      </div>
    </div>
  `;
  const input = $('amkrKeyValue');
  if (input) input.select();
  scopedPage.on($('amkrKeyCopy'), 'click', async () => {
    try {
      await navigator.clipboard.writeText(key);
      toast('推理 key 已复制');
    } catch {
      // 剪贴板不可用（非安全上下文等）时退回手动全选，不让复制失败静默无事发生。
      input?.select();
      toast('无法自动复制，请手动全选复制', 'error');
    }
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
