import { store } from '../store.js';
import { get, post } from '../app.js';
import { toast, animateNumber, flashSuccess, $ } from '../components.js';

const BUILTIN_TYPES = ['deepseek', 'aliyun-bailian', 'bigmodel', 'siliconflow', 'volcengine-ark', 'ytea'];

const TYPE_OPTIONS = [
  { value: 'openai-compatible', label: 'OpenAI Compatible' },
  { value: 'deepseek', label: 'DeepSeek' },
  { value: 'aliyun-bailian', label: '阿里云百炼' },
  { value: 'bigmodel', label: '智谱 BigModel' },
  { value: 'siliconflow', label: 'SiliconFlow' },
  { value: 'volcengine-ark', label: '火山方舟' },
  { value: 'ytea', label: 'YTea' },
];

const DEFAULT_URLS = {
  'openai-compatible': 'https://api.openai.com',
  'deepseek': 'https://api.deepseek.com',
  'aliyun-bailian': 'https://dashscope.aliyuncs.com/compatible-mode',
  'bigmodel': 'https://open.bigmodel.cn/api/paas/v4',
  'siliconflow': 'https://api.siliconflow.cn',
  'volcengine-ark': 'https://ark.cn-beijing.volces.com/api/v3',
  'ytea': 'https://api.ytea.top',
};

const TYPE_LABEL_MAP = Object.fromEntries(TYPE_OPTIONS.map(o => [o.value, o.label]));

let providers = [];
let editingIdx = null;
let currentModal = null;
let modalModels = [];
let probeStatus = {};

export async function init(container) {
  container.innerHTML = `
    <div class="card">
      <div class="card-header">
        <div>
          <div class="card-title">Provider 管理</div>
        </div>
        <div style="display:flex;gap:8px">
          <button class="btn btn-sm" id="probeAllBtn">全部检测</button>
          <button class="btn btn-primary btn-sm" id="addProviderBtn">+ 添加 Provider</button>
        </div>
      </div>
      <div id="providerList" style="display:flex;flex-direction:column;gap:16px"></div>
    </div>
  `;

  $('addProviderBtn').addEventListener('click', addProvider);
  $('probeAllBtn').addEventListener('click', probeAll);

  await loadProviders();
}

async function loadProviders() {
  try {
    const data = await get('/providers');
    const raw = Array.isArray(data.providers) ? data.providers : [];
    providers = raw.map(p => ({
      ...p,
      platform_type: p.platform_type || p.type || 'openai-compatible',
    }));
    editingIdx = null;
    renderList();
  } catch {
    toast('加载 Provider 列表失败', 'error');
  }
}

function renderList() {
  const el = $('providerList');
  if (!providers.length) {
    el.innerHTML = '<div style="color:var(--text-3);padding:24px;text-align:center">暂无 Provider。点击上方按钮添加。</div>';
    return;
  }
  el.innerHTML = providers.map((p, i) => {
    if (editingIdx === i) return renderEditCard(p, i);
    return renderReadonlyCard(p, i);
  }).join('');
  bindCardEvents();
}

function renderReadonlyCard(p, i) {
  const typeLabel = TYPE_LABEL_MAP[p.platform_type] || p.platform_type;
  const masked = maskKey(p.api_key);
  const models = p.models || [];
  const name = p.name || '';
  const ps = probeStatus[name];
  const probeHtml = ps
    ? ps.ok
      ? `<span style="display:inline-flex;align-items:center;gap:4px;font-size:11px;color:var(--success)"><span class="status-dot running"></span>可用 ${ps.latency}ms</span>`
      : `<span style="display:inline-flex;align-items:center;gap:4px;font-size:11px;color:var(--danger)"><span class="status-dot error"></span>不可用</span>`
    : `<span style="display:inline-flex;align-items:center;gap:4px;font-size:11px;color:var(--text-3)"><span class="status-dot"></span>未检测</span>`;

  return `
    <div class="stat-card" data-idx="${i}" style="text-align:left">
      <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:12px">
        <div style="display:flex;align-items:center;gap:10px">
          <span class="tag tag-accent">${typeLabel}</span>
          <span style="font-size:12px;color:${p.enabled ? 'var(--success)' : 'var(--text-3)'}">${p.enabled ? '已启用' : '已禁用'}</span>
          ${probeHtml}
        </div>
        <div style="display:flex;gap:8px">
          <button class="btn btn-sm" data-action="probe" data-idx="${i}" data-name="${name}">可用性检查</button>
          <button class="btn btn-sm" data-action="edit" data-idx="${i}">编辑</button>
          <button class="btn btn-sm btn-danger" data-action="delete" data-idx="${i}">删除</button>
        </div>
      </div>
      <div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:12px">
        ${models.length ? models.map(m => `<span class="tag">${m}</span>`).join('') : '<span style="color:var(--text-3);font-size:12px">无模型</span>'}
      </div>
      <div style="display:flex;gap:24px;font-size:12px;color:var(--text-2)">
        <div><span style="color:var(--text-3)">API Key:</span> ${masked}</div>
        <div><span style="color:var(--text-3)">Base URL:</span> ${p.base_url || '—'}</div>
      </div>
    </div>
  `;
}

function renderEditCard(p, i) {
  const isNew = p._new;
  const typeDisabled = BUILTIN_TYPES.includes(p.platform_type) && !isNew ? 'disabled' : '';
  return `
    <div class="stat-card" data-idx="${i}" style="text-align:left;border:1px solid var(--accent)">
      <div style="display:flex;flex-direction:column;gap:14px">
        <div class="form-group" style="margin:0">
          <label>平台类型</label>
          <select id="pv_type_${i}" ${typeDisabled}>
            ${TYPE_OPTIONS.map(o => `<option value="${o.value}" ${o.value === p.platform_type ? 'selected' : ''}>${o.label}</option>`).join('')}
          </select>
        </div>
        <div class="form-group" style="margin:0">
          <label>Base URL</label>
          <input type="text" id="pv_url_${i}" value="${p.base_url || ''}" placeholder="自动填充">
        </div>
        <div class="form-group" style="margin:0">
          <label>API Key</label>
          <input type="password" id="pv_key_${i}" value="${p.api_key || ''}" placeholder="sk-...">
        </div>
        <div class="form-group" style="margin:0">
          <label>Healthcheck Model</label>
          <input type="text" id="pv_health_${i}" value="${p.healthcheck_model || ''}" placeholder="用于健康检查的模型名">
        </div>
        <div class="form-group" style="margin:0">
          <label>模型列表</label>
          <div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:8px" id="pv_models_${i}">
            ${(p.models || []).map((m, mi) => `<span class="tag" data-model-idx="${mi}">${m}<span data-remove-model="${mi}" style="cursor:pointer;margin-left:4px;color:var(--danger)">&times;</span></span>`).join('')}
          </div>
          <div style="display:flex;gap:8px">
            <input type="text" id="pv_newModel_${i}" placeholder="输入模型名后回车添加" style="flex:1">
            <button type="button" class="btn btn-sm" data-action="addModel" data-idx="${i}">添加</button>
          </div>
        </div>
        <div style="display:flex;align-items:center;gap:8px">
          <input type="checkbox" id="pv_enabled_${i}" ${p.enabled ? 'checked' : ''}>
          <label for="pv_enabled_${i}" style="margin:0;cursor:pointer">启用</label>
        </div>
        <div style="display:flex;gap:8px;justify-content:flex-end;margin-top:8px">
          <button class="btn btn-sm btn-danger" data-action="delete" data-idx="${i}">删除</button>
          <button class="btn btn-sm" data-action="cancel" data-idx="${i}">取消</button>
          <button class="btn btn-sm btn-primary" data-action="save" data-idx="${i}">保存</button>
        </div>
      </div>
    </div>
  `;
}

function bindCardEvents() {
  const el = $('providerList');
  el.querySelectorAll('[data-action]').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      const action = btn.dataset.action;
      const idx = parseInt(btn.dataset.idx, 10);
      if (action === 'edit') startEdit(idx);
      else if (action === 'cancel') cancelEdit(idx);
      else if (action === 'save') saveProvider(idx);
      else if (action === 'delete') deleteProvider(idx);
      else if (action === 'addModel') addModelToProvider(idx);
      else if (action === 'probe') probeProvider(btn);
    });
  });

  el.querySelectorAll('[data-remove-model]').forEach(span => {
    span.addEventListener('click', (e) => {
      e.stopPropagation();
      const mi = parseInt(span.dataset.removeModel, 10);
      const card = span.closest('[data-idx]');
      const idx = parseInt(card.dataset.idx, 10);
      providers[idx].models.splice(mi, 1);
      renderList();
    });
  });

  el.querySelectorAll('[id^="pv_type_"]').forEach(sel => {
    sel.addEventListener('change', () => {
      const idx = parseInt(sel.id.replace('pv_type_', ''), 10);
      const newType = sel.value;
      const urlInput = $(`pv_url_${idx}`);
      if (BUILTIN_TYPES.includes(newType) && urlInput) {
        urlInput.value = DEFAULT_URLS[newType] || '';
      }
    });
  });

  el.querySelectorAll('[id^="pv_newModel_"]').forEach(input => {
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') {
        e.preventDefault();
        const idx = parseInt(input.id.replace('pv_newModel_', ''), 10);
        addModelToProvider(idx);
      }
    });
  });
}

function startEdit(idx) {
  editingIdx = idx;
  renderList();
  const urlInput = $(`pv_url_${idx}`);
  if (BUILTIN_TYPES.includes(providers[idx].platform_type) && urlInput && !urlInput.value) {
    urlInput.value = DEFAULT_URLS[providers[idx].platform_type] || '';
  }
}

function cancelEdit(idx) {
  if (providers[idx]._new) {
    providers.splice(idx, 1);
  }
  editingIdx = null;
  renderList();
}

function addProvider() {
  if (editingIdx !== null) {
    toast('请先保存或取消当前编辑', 'warning');
    return;
  }
  openAddModal();
}

function openAddModal() {
  closeModal();
  modalModels = [];

  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay';
  overlay.innerHTML = `
    <div class="modal" style="max-width:560px">
      <div class="modal-header">
        <span style="font-size:16px;font-weight:600">添加 Provider</span>
        <button class="btn btn-sm" id="modalClose">✕</button>
      </div>
      <div class="modal-body">
        <div style="display:flex;flex-direction:column;gap:14px">
          <div class="form-group" style="margin:0">
            <label>平台类型</label>
            <select id="modal_type">
              ${TYPE_OPTIONS.map(o => `<option value="${o.value}">${o.label}</option>`).join('')}
            </select>
          </div>
          <div class="form-group" style="margin:0">
            <label>Base URL</label>
            <input type="text" id="modal_url" value="${DEFAULT_URLS['openai-compatible']}" placeholder="自动填充">
          </div>
          <div class="form-group" style="margin:0">
            <label>API Key</label>
            <input type="password" id="modal_key" placeholder="sk-...">
          </div>
          <div class="form-group" style="margin:0">
            <label>Healthcheck Model</label>
            <input type="text" id="modal_health" placeholder="用于健康检查的模型名">
          </div>
          <div class="form-group" style="margin:0">
            <label>模型列表</label>
            <div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:8px" id="modal_models_container"></div>
            <div style="display:flex;gap:8px">
              <input type="text" id="modal_newModel" placeholder="输入模型名后回车添加" style="flex:1">
              <button type="button" class="btn btn-sm" id="modalAddModelBtn">添加</button>
            </div>
          </div>
          <div style="display:flex;align-items:center;gap:8px">
            <input type="checkbox" id="modal_enabled" checked>
            <label for="modal_enabled" style="margin:0;cursor:pointer">启用</label>
          </div>
        </div>
      </div>
      <div class="modal-footer">
        <button class="btn btn-sm" id="modalCancelBtn">取消</button>
        <button class="btn btn-sm btn-primary" id="modalSaveBtn">保存</button>
      </div>
    </div>
  `;
  document.body.appendChild(overlay);
  currentModal = overlay;

  overlay.addEventListener('click', (e) => { if (e.target === overlay) closeModal(); });
  $('modalClose').addEventListener('click', closeModal);
  $('modalCancelBtn').addEventListener('click', closeModal);
  $('modalSaveBtn').addEventListener('click', saveFromModal);

  $('modal_type').addEventListener('change', () => {
    const newType = $('modal_type').value;
    const urlInput = $('modal_url');
    if (BUILTIN_TYPES.includes(newType)) {
      urlInput.value = DEFAULT_URLS[newType] || '';
    }
  });

  $('modalAddModelBtn').addEventListener('click', addModelToModal);
  $('modal_newModel').addEventListener('keydown', (e) => {
    if (e.key === 'Enter') {
      e.preventDefault();
      addModelToModal();
    }
  });

  renderModalModels();
}

function renderModalModels() {
  const container = $('modal_models_container');
  if (!container) return;
  if (!modalModels.length) {
    container.innerHTML = '<span style="color:var(--text-3);font-size:12px">无模型</span>';
    return;
  }
  container.innerHTML = modalModels.map((m, mi) =>
    `<span class="tag">${m}<span data-remove-modal-model="${mi}" style="cursor:pointer;margin-left:4px;color:var(--danger)">&times;</span></span>`
  ).join('');
  container.querySelectorAll('[data-remove-modal-model]').forEach(span => {
    span.addEventListener('click', () => {
      const mi = parseInt(span.dataset.removeModalModel, 10);
      modalModels.splice(mi, 1);
      renderModalModels();
    });
  });
}

function addModelToModal() {
  const input = $('modal_newModel');
  if (!input) return;
  const name = input.value.trim();
  if (!name) return;
  if (modalModels.includes(name)) {
    toast('模型已存在', 'warning');
    return;
  }
  modalModels.push(name);
  input.value = '';
  renderModalModels();
}

function closeModal() {
  if (currentModal) {
    currentModal.remove();
    currentModal = null;
  }
}

async function saveFromModal() {
  const platformType = $('modal_type').value;
  const baseUrl = $('modal_url').value.trim();
  const apiKey = $('modal_key').value.trim();
  const healthcheckModel = $('modal_health').value.trim();
  const enabled = $('modal_enabled').checked;

  if (!apiKey) {
    toast('请填写 API Key', 'warning');
    return;
  }

  providers.push({
    platform_type: platformType,
    base_url: baseUrl,
    api_key: apiKey,
    models: [...modalModels],
    enabled,
    healthcheck_model: healthcheckModel,
  });

  closeModal();
  await saveAll();
}

async function saveProvider(idx) {
  const p = providers[idx];
  p.platform_type = $(`pv_type_${idx}`).value;
  p.base_url = $(`pv_url_${idx}`).value.trim();
  p.api_key = $(`pv_key_${idx}`).value.trim();
  p.healthcheck_model = $(`pv_health_${idx}`).value.trim();
  p.enabled = $(`pv_enabled_${idx}`).checked;
  delete p._new;
  editingIdx = null;
  await saveAll();
}

async function deleteProvider(idx) {
  providers.splice(idx, 1);
  editingIdx = null;
  await saveAll();
}

function addModelToProvider(idx) {
  const input = $(`pv_newModel_${idx}`);
  if (!input) return;
  const name = input.value.trim();
  if (!name) return;
  if (!providers[idx].models) providers[idx].models = [];
  if (providers[idx].models.includes(name)) {
    toast('模型已存在', 'warning');
    return;
  }
  providers[idx].models.push(name);
  input.value = '';
  renderList();
}

async function saveAll() {
  const clean = providers.map(p => ({
    type: p.platform_type,
    base_url: p.base_url,
    api_key: p.api_key,
    models: p.models || [],
    enabled: p.enabled,
    healthcheck_model: p.healthcheck_model || '',
  }));
  try {
    const res = await post('/providers', { providers: clean });
    toast(res.message || '保存成功', 'success');
    flashSuccess($('addProviderBtn'));
    await loadProviders();
  } catch {
    toast('保存失败', 'error');
  }
}

function maskKey(key) {
  if (!key) return '—';
  if (key.length <= 8) return '****';
  return key.slice(0, 4) + '****' + key.slice(-4);
}

async function probeProvider(btn) {
  const name = btn.dataset.name;
  if (!name) return;

  btn.disabled = true;
  btn.textContent = '检查中…';

  try {
    const res = await post('/providers/probe', { name });
    if (res.success) {
      probeStatus[name] = { ok: true, latency: res.latency_ms || 0 };
      toast(`${name} 可用 (${res.latency_ms}ms)`, 'success');
    } else {
      probeStatus[name] = { ok: false, latency: 0 };
      toast(`${name} 不可用: ${res.error || '未知错误'}`, 'error');
    }
  } catch (e) {
    probeStatus[name] = { ok: false, latency: 0 };
    toast(`检查失败: ${e.message}`, 'error');
  }

  renderList();
}

async function probeAll() {
  const btn = $('probeAllBtn');
  if (!providers.length) {
    toast('暂无 Provider 可检测', 'warning');
    return;
  }

  btn.disabled = true;
  btn.textContent = '检测中…';

  const tasks = providers
    .filter(p => p.name && p.enabled)
    .map(p => p.name);

  let okCount = 0;
  let failCount = 0;

  for (const name of tasks) {
    try {
      const res = await post('/providers/probe', { name });
      if (res.success) {
        probeStatus[name] = { ok: true, latency: res.latency_ms || 0 };
        okCount++;
      } else {
        probeStatus[name] = { ok: false, latency: 0 };
        failCount++;
      }
    } catch {
      probeStatus[name] = { ok: false, latency: 0 };
      failCount++;
    }
    renderList();
  }

  btn.disabled = false;
  btn.textContent = '全部检测';
  toast(`检测完成: ${okCount} 可用, ${failCount} 不可用`, okCount > 0 ? 'success' : 'error');
}
