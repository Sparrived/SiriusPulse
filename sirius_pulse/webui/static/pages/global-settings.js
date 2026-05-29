import { store } from '../store.js';
import { get, post } from '../app.js';
import { toast, animateNumber, flashSuccess, $ } from '../components.js';

const FIELDS = [
  { key: 'webui_host', label: 'WebUI 监听地址', type: 'text', placeholder: '0.0.0.0', defaultVal: '0.0.0.0' },
  { key: 'webui_port', label: 'WebUI 端口', type: 'number', placeholder: '8080', defaultVal: 8080 },
  { key: 'log_level', label: '日志级别', type: 'select', options: ['DEBUG', 'INFO', 'WARNING', 'ERROR'], defaultVal: 'INFO' },
  { key: 'napcat_install_dir', label: 'NapCat 安装目录', type: 'text', placeholder: '' },
  { key: 'napcat_base_port', label: 'NapCat 起始端口', type: 'number', placeholder: '3001', defaultVal: 3001 },
];

let currentConfig = {};

export async function init(container) {
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
          <button type="button" class="btn" id="gsResetBtn">重置</button>
          <button type="submit" class="btn btn-primary" id="gsSaveBtn">保存设置</button>
        </div>
      </form>
    </div>
  `;

  $('globalSettingsForm').addEventListener('submit', handleSave);
  $('gsResetBtn').addEventListener('click', () => fillForm(currentConfig));

  await loadConfig();
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
      result[f.key] = parseInt(el.value, 10) || f.defaultVal || 0;
    } else {
      result[f.key] = el.value || f.defaultVal || '';
    }
  }
  return result;
}

async function handleSave(e) {
  e.preventDefault();
  const btn = $('gsSaveBtn');
  const data = collectFormData();
  try {
    const res = await post('/global-config', data);
    currentConfig = data;
    toast(res.message || '保存成功', 'success');
    flashSuccess(btn);
  } catch {
    toast('保存失败', 'error');
  }
}
