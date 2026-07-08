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
  `;

  const form = $('globalSettingsForm');
  form.addEventListener('submit', (event) => event.preventDefault());
  const autoSave = createAutoSave({
    root: form,
    statusEl: $('gsAutoSaveStatus'),
    save: () => handleSave(),
    onError: () => toast('保存失败', 'error'),
  });
  $('gsResetBtn').addEventListener('click', () => {
    fillForm(currentConfig);
    autoSave.schedule();
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
}

function renderField(f) {
  if (f.type === 'select') {
    return `
      <div class="form-group">
        <label for="gs_${f.key}">${f.label}</label>
        <select id="gs_${f.key}" name="${f.key}">
          ${f.options.map(o => `<option value="${o}">${o}</option>`).join('')}
        </select>
      </div>
    `;
  }
  if (f.type === 'number') {
    return `
      <div class="form-group">
        <label for="gs_${f.key}">${f.label}</label>
        <div class="number-input-group">
          <button type="button" class="number-spin-btn" data-spin-target="gs_${f.key}" data-spin-dir="-1">−</button>
          <input id="gs_${f.key}" name="${f.key}" type="number" placeholder="${f.placeholder || ''}"${f.min !== undefined ? ` min="${f.min}"` : ''}${f.max !== undefined ? ` max="${f.max}"` : ''}>
          <button type="button" class="number-spin-btn" data-spin-target="gs_${f.key}" data-spin-dir="1">+</button>
        </div>
      </div>
    `;
  }
  return `
    <div class="form-group">
      <label for="gs_${f.key}">${f.label}</label>
      <input id="gs_${f.key}" name="${f.key}" type="${f.type}" placeholder="${f.placeholder || ''}">
    </div>
  `;
}

function fillForm(cfg) {
  for (const f of FIELDS) {
    const el = $(`gs_${f.key}`);
    if (!el) continue;
    el.value = cfg[f.key] ?? f.defaultVal ?? '';
  }
}

async function loadConfig() {
  try {
    const data = await get('/global-config');
    currentConfig = data || {};
    fillForm(currentConfig);
  } catch {
    toast('加载全局配置失败', 'error');
  }
}

function collectFormData() {
  const result = {};
  for (const f of FIELDS) {
    const el = $(`gs_${f.key}`);
    if (!el) continue;
    if (f.type === 'number') {
      let value = parseInt(el.value, 10) || f.defaultVal || 0;
      if (f.min !== undefined) value = Math.max(f.min, value);
      if (f.max !== undefined) value = Math.min(f.max, value);
      result[f.key] = value;
    } else {
      result[f.key] = el.value || f.defaultVal || '';
    }
  }
  return result;
}

async function handleSave() {
  const data = collectFormData();
  try {
    const res = await post('/global-config', data);
    currentConfig = data;
    return res;
  } catch (error) {
    if (error?.name === 'AbortError') return;
    throw error;
  }
}
