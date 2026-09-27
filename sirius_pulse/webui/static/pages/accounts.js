import { get, post } from '../app.js';
import { toast, confirmDanger } from '../components.js';
import { createAutoSave } from '../autosave.js';
import { createScopedPage } from '../page-context.js';
import { store } from '../store.js';

const scopedPage = createScopedPage();
const $ = scopedPage.$;

const KIND_LABELS = { github: 'GitHub' };

// 模块级持有，供 save() 与渲染共用；也满足 autosave 作用域静态检查。
let autoSave = null;
let meta = { kinds: ['github'], default_env: { github: 'GH_TOKEN' } };

export function dispose() {
  autoSave?.destroy();
  autoSave = null;
  scopedPage.use(null, null);
}

export async function init(container, params = {}) {
  scopedPage.use(params?.ctx, container);

  if (!store.currentPersona) {
    container.innerHTML = `
      <div class="card">
        <div class="card-header"><div class="card-title">人格账户</div></div>
        <div style="padding:40px;text-align:center;color:var(--text-3)">
          <div style="font-size:16px;margin-bottom:8px">请先选择人格</div>
          <div style="font-size:13px">在顶部导航栏中选择要配置的人格</div>
        </div>
      </div>
    `;
    return;
  }

  container.innerHTML = `
    <div class="card">
      <div class="card-header">
        <div>
          <div class="card-title">人格账户</div>
          <div class="card-subtitle">她自己对外用的账号；凭据只留在人格目录里，读取时一律显示掩码</div>
        </div>
        <span id="accountsSaveStatus" style="color:var(--text-3);font-size:12px"></span>
      </div>
      <div id="accountsBody" class="accounts-body">
        <div style="padding:20px;color:var(--text-3)">加载中...</div>
      </div>
    </div>
  `;

  try {
    const data = await get('/persona/accounts');
    if (!scopedPage.isActive()) return;
    meta = {
      kinds: data.kinds?.length ? data.kinds : ['github'],
      default_env: data.default_env || { github: 'GH_TOKEN' },
    };
    render(data.accounts || []);

    autoSave = createAutoSave({
      root: $('accountsForm'),
      statusEl: $('accountsSaveStatus'),
      save,
      onError: (error) => toast('保存失败: ' + error.message, 'error'),
    });
    autoSave.markReady();

    // 事件委托挂在稳定容器上：增删行会重排内部节点，逐行绑定会随重渲染失效。
    scopedPage.on($('accountsBody'), 'click', onBodyClick);
  } catch (e) {
    if (e?.name === 'AbortError') return;
    const body = $('accountsBody');
    if (body) body.innerHTML = `<div style="padding:20px;color:var(--danger)">加载失败: ${e.message}</div>`;
  }
}

function onBodyClick(event) {
  const addBtn = event.target.closest('[data-action="add"]');
  if (addBtn) {
    const form = $('accountsForm');
    if (!form) return;
    const rows = readRows();
    rows.push({ kind: meta.kinds[0] || 'github', name: '', username: '', secret: '', env: '' });
    render(rows);
    autoSave?.flush();
    return;
  }

  const removeBtn = event.target.closest('[data-action="remove"]');
  if (!removeBtn) return;
  const index = Number(removeBtn.dataset.index);
  const rows = readRows();
  const label = rows[index]?.name || rows[index]?.kind || '这个账号';
  if (!confirmDanger(`确定删除「${label}」及其凭据？`)) return;
  rows.splice(index, 1);
  render(rows);
  autoSave?.flush();
}

function readRows() {
  const rows = scopedPage.$$('[data-account-row]');
  return rows.map((row) => ({
    kind: row.querySelector('[data-field="kind"]')?.value || 'github',
    name: row.querySelector('[data-field="name"]')?.value || '',
    username: row.querySelector('[data-field="username"]')?.value || '',
    secret: row.querySelector('[data-field="secret"]')?.value || '',
    env: row.querySelector('[data-field="env"]')?.value || '',
  }));
}

function render(accounts) {
  const body = $('accountsBody');
  if (!body) return;

  // 字段值来自她自己的配置，插入 HTML 前必须转义，否则一个引号就能改写整个表单。
  const esc = (value) =>
    String(value ?? '').replace(/[&<>"']/g, (ch) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[ch]));

  const kindOptions = (selected) =>
    meta.kinds
      .map((kind) => `<option value="${esc(kind)}"${kind === selected ? ' selected' : ''}>${esc(KIND_LABELS[kind] || kind)}</option>`)
      .join('');

  const rows = accounts
    .map((account, index) => {
      const defaultEnv = meta.default_env?.[account.kind] || '';
      return `
      <div class="accounts-row" data-account-row>
        <div class="form-group">
          <label>类型</label>
          <div class="select-wrap"><select data-field="kind">${kindOptions(account.kind)}</select></div>
        </div>
        <div class="form-group">
          <label>备注名</label>
          <input type="text" data-field="name" value="${esc(account.name)}" placeholder="例如 LiveSirius">
        </div>
        <div class="form-group">
          <label>账号</label>
          <input type="text" data-field="username" value="${esc(account.username)}" placeholder="登录名，可留空">
        </div>
        <div class="form-group">
          <label>凭据</label>
          <input type="password" data-field="secret" value="${esc(account.secret)}"
                 autocomplete="new-password" placeholder="Token / 密码">
          <div class="accounts-hint">读回来永远是掩码；原样保存不会覆盖已存的凭据。清空即删除。</div>
        </div>
        <div class="form-group">
          <label>环境变量名</label>
          <input type="text" data-field="env" value="${esc(account.env)}" placeholder="${esc(defaultEnv)}">
          <div class="accounts-hint">留空则用 ${esc(defaultEnv || '默认值')}；Bash 工具里可直接读。</div>
        </div>
        <div class="accounts-row-actions">
          <button type="button" class="btn btn-sm btn-danger" data-action="remove" data-index="${index}">删除</button>
        </div>
      </div>`;
    })
    .join('');

  body.innerHTML = `
    <form id="accountsForm" class="accounts-form">
      <div class="accounts-list">
        ${rows || '<div class="accounts-empty">还没有账号。她要对外做事时，先在这里给她一个身份。</div>'}
      </div>
      <div class="accounts-actions">
        <button type="button" class="btn btn-sm" data-action="add">＋ 添加账号</button>
      </div>
    </form>
  `;
}

async function save() {
  const accounts = readRows();
  const data = await post('/persona/accounts', { accounts });
  // 服务端会把掩码还原成真实凭据再存；用它回填，页面不会停留在半掩码状态。
  if (Array.isArray(data?.accounts) && scopedPage.isActive()) render(data.accounts);
}
