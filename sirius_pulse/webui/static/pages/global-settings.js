import { store } from '../store.js';
import { get, post } from '../app.js';
import { toast, animateNumber } from '../components.js';
import { createScopedPage } from '../page-context.js';
import { createAutoSave } from '../autosave.js';

const scopedPage = createScopedPage();

export function dispose() {
  scopedPage.use(null, null);
}
const $ = scopedPage.$;

const FIELDS = [
  { key: 'webui_host', label: 'WebUI 监听地址', type: 'text', placeholder: '0.0.0.0', defaultVal: '0.0.0.0' },
  { key: 'webui_port', label: 'WebUI 端口', type: 'number', placeholder: '8080', defaultVal: 8080 },
  { key: 'log_level', label: '日志级别', type: 'select', options: ['DEBUG', 'INFO', 'WARNING', 'ERROR'], defaultVal: 'INFO' },
];

// AMKR 连接配置。模型、供应商与 Key 池都在 AMKR 侧维护，这里只填「怎么连上它」。
const AMKR_FIELDS = [
  { key: 'amkr_base_url', label: 'AMKR 地址', type: 'text', placeholder: 'http://127.0.0.1:8000', defaultVal: 'http://127.0.0.1:8000' },
  { key: 'amkr_local_api_key', label: 'AMKR 本地授权 Key', type: 'password', placeholder: '留空或保持掩码表示不修改', defaultVal: '' },
  { key: 'amkr_workspace', label: '工作空间前缀', type: 'text', placeholder: 'sirius-pulse', defaultVal: 'sirius-pulse' },
  { key: 'amkr_public_url', label: 'AMKR 浏览器地址（可选）', type: 'text', placeholder: '留空表示与 AMKR 地址相同', defaultVal: '' },
  { key: 'amkr_ui_enabled', label: '启用 AMKR 自带 WebUI', type: 'checkbox', defaultVal: true },
];

let currentConfig = {};

export async function init(container, params = {}) {
  scopedPage.use(params?.ctx, container);
  container.innerHTML = `
    <div class="card">
      <div class="card-header">
        <div>
          <div class="card-title">全局设置</div>
          <div class="card-subtitle">WebUI 服务器和 NapCat 基础配置</div>
        </div>
      </div>
      <form id="globalSettingsForm">
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:16px">
          ${FIELDS.map(f => renderField(f)).join('')}
        </div>
        <div style="margin-top:24px;display:flex;justify-content:flex-end;gap:12px">
          <span id="gsAutoSaveStatus" style="color:var(--text-3);font-size:12px;align-self:center"></span>
          <button type="button" class="btn" id="gsResetBtn">重置</button>
        </div>
      </form>
    </div>
    <div class="card" style="margin-top:16px">
      <div class="card-header">
        <div>
          <div class="card-title">AMKR 连接</div>
          <div class="card-subtitle">所有模型调用都经由 AMKR；供应商与 Key 池在 AMKR 侧维护</div>
        </div>
      </div>
      <form id="amkrSettingsForm">
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:16px">
          ${AMKR_FIELDS.map(f => renderField(f, 'ak')).join('')}
        </div>
        <div style="margin-top:24px;display:flex;justify-content:flex-end;gap:12px">
          <span id="akAutoSaveStatus" style="color:var(--text-3);font-size:12px;align-self:center"></span>
          <button type="button" class="btn" id="akResetBtn">重置</button>
        </div>
      </form>
    </div>
  `;

  const form = $('globalSettingsForm');
  form.addEventListener('submit', (event) => event.preventDefault());
  const autoSave = createAutoSave({
    root: form,
    statusEl: $('gsAutoSaveStatus'),
    save: () => handleSave(FIELDS, 'gs'),
    onError: () => toast('保存失败', 'error'),
  });
  $('gsResetBtn').addEventListener('click', () => {
    fillForm(FIELDS, currentConfig, 'gs');
    autoSave.schedule();
  });

  const amkrForm = $('amkrSettingsForm');
  amkrForm.addEventListener('submit', (event) => event.preventDefault());
  const amkrAutoSave = createAutoSave({
    root: amkrForm,
    statusEl: $('akAutoSaveStatus'),
    save: () => handleSave(AMKR_FIELDS, 'ak'),
    onError: () => toast('保存失败', 'error'),
  });
  $('akResetBtn').addEventListener('click', () => {
    fillForm(AMKR_FIELDS, currentConfig, 'ak');
    amkrAutoSave.schedule();
  });

  // 数字调节按钮事件
  scopedPage.$$('[data-spin-target]').forEach(btn => {
    btn.addEventListener('click', () => {
      const target = $(btn.dataset.spinTarget);
      if (!target) return;
      const dir = parseInt(btn.dataset.spinDir, 10);
      const step = parseFloat(target.step) || 1;
      const min = target.min !== '' ? parseFloat(target.min) : -Infinity;
      const max = target.max !== '' ? parseFloat(target.max) : Infinity;
      const cur = parseFloat(target.value) || 0;
      target.value = Math.min(max, Math.max(min, cur + step * dir));
      target.dispatchEvent(new Event('change', { bubbles: true }));
    });
  });

  await loadConfig();
  autoSave.markReady();
  amkrAutoSave.markReady();
}

function renderField(f, prefix = 'gs') {
  if (f.type === 'select') {
    return `
      <div class="form-group">
        <label for="${prefix}_${f.key}">${f.label}</label>
        <select id="${prefix}_${f.key}" name="${f.key}">
          ${f.options.map(o => `<option value="${o}">${o}</option>`).join('')}
        </select>
      </div>
    `;
  }
  if (f.type === 'checkbox') {
    return `
      <div class="form-group">
        <label for="${prefix}_${f.key}" style="display:flex;align-items:center;gap:8px">
          <input id="${prefix}_${f.key}" name="${f.key}" type="checkbox">
          <span>${f.label}</span>
        </label>
      </div>
    `;
  }
  if (f.type === 'number') {
    return `
      <div class="form-group">
        <label for="${prefix}_${f.key}">${f.label}</label>
        <div class="number-input-group">
          <button type="button" class="number-spin-btn" data-spin-target="${prefix}_${f.key}" data-spin-dir="-1">−</button>
          <input id="${prefix}_${f.key}" name="${f.key}" type="number" placeholder="${f.placeholder || ''}"${f.min !== undefined ? ` min="${f.min}"` : ''}${f.max !== undefined ? ` max="${f.max}"` : ''}>
          <button type="button" class="number-spin-btn" data-spin-target="${prefix}_${f.key}" data-spin-dir="1">+</button>
        </div>
      </div>
    `;
  }
  if (f.type === 'password') {
    return `
      <div class="form-group">
        <label for="${prefix}_${f.key}">${f.label}</label>
        <input id="${prefix}_${f.key}" name="${f.key}" type="password" autocomplete="new-password" placeholder="${f.placeholder || ''}">
      </div>
    `;
  }
  return `
    <div class="form-group">
      <label for="${prefix}_${f.key}">${f.label}</label>
      <input id="${prefix}_${f.key}" name="${f.key}" type="${f.type}" placeholder="${f.placeholder || ''}">
    </div>
  `;
}

function fillForm(fields, cfg, prefix = 'gs') {
  for (const f of fields) {
    const el = $(`${prefix}_${f.key}`);
    if (!el) continue;
    if (f.type === 'checkbox') {
      el.checked = cfg[f.key] ?? f.defaultVal ?? false;
      continue;
    }
    el.value = cfg[f.key] ?? f.defaultVal ?? '';
  }
}

async function loadConfig() {
  try {
    const data = await get('/global-config');
    currentConfig = data || {};
    fillForm(FIELDS, currentConfig, 'gs');
    fillForm(AMKR_FIELDS, currentConfig, 'ak');
  } catch {
    toast('加载全局配置失败', 'error');
  }
}

function collectFormData(fields, prefix = 'gs') {
  const result = {};
  for (const f of fields) {
    const el = $(`${prefix}_${f.key}`);
    if (!el) continue;
    if (f.type === 'checkbox') {
      result[f.key] = el.checked;
    } else if (f.type === 'number') {
      let value = parseInt(el.value, 10) || f.defaultVal || 0;
      if (f.min !== undefined) value = Math.max(f.min, value);
      if (f.max !== undefined) value = Math.min(f.max, value);
      result[f.key] = value;
    } else {
      result[f.key] = el.value ?? f.defaultVal ?? '';
    }
  }
  return result;
}

async function handleSave(fields, prefix) {
  const data = collectFormData(fields, prefix);
  try {
    const res = await post('/global-config', data);
    Object.assign(currentConfig, data);
    return res;
  } catch (error) {
    if (error?.name === 'AbortError') return;
    throw error;
  }
}
