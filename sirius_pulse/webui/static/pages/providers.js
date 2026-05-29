import { store } from '../store.js';
import { get, post } from '../app.js';
import { toast, animateNumber, flashSuccess, $, ModelSelect } from '../components.js';

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

const PROBE_CACHE_TTL = 30 * 60 * 1000;

let providers = [];
let editingIdx = null;
let currentModal = null;
let modalModels = [];
let probeStatus = {};
let _root = null;
let _modelsDevCache = {};
let _healthSelects = {};

function _getModelTags(modelId) {
  for (const models of Object.values(_modelsDevCache)) {
    const found = models.find(m => m.id === modelId);
    if (found) {
      const tags = [];
      if (found.tool_call) tags.push('函数调用');
      if (found.reasoning) tags.push('推理');
      if (found.vision) tags.push('视觉');
      if (found.audio) tags.push('音频');
      return tags;
    }
  }
  return [];
}

function _renderModelTag(modelId, extra = '') {
  const tags = _getModelTags(modelId);
  const tagsHtml = tags.map(t => `<span class="cap-tag-inline">${t}</span>`).join('');
  return `<span class="tag">${modelId}${tagsHtml ? ` ${tagsHtml}` : ''}${extra}</span>`;
}

function _mountHealthSelects() {
  _healthSelects = {};
  _root.querySelectorAll('[data-health-idx]').forEach(container => {
    const idx = parseInt(container.dataset.healthIdx, 10);
    const p = providers[idx];
    if (!p) return;
    const opts = (p.models || []).map(m => ({
      value: m,
      label: m,
      tags: _getModelTags(m),
    }));
    const sel = new ModelSelect({
      options: opts,
      value: p.healthcheck_model || '',
      placeholder: '— 请选择 —',
    });
    sel.mount(container);
    _healthSelects[idx] = sel;
  });
}

export async function init(container) {
  _root = container;
  container.innerHTML = `
    <div class="card">
      <div class="card-header">
        <div>
          <div class="card-title">Provider 管理</div>
        </div>
        <div style="display:flex;gap:8px">
          <button class="btn btn-sm" id="refreshModelsBtn" title="从 models.dev 自动获取模型列表">刷新模型</button>
          <button class="btn btn-sm" id="probeAllBtn">全部检测</button>
          <button class="btn btn-primary btn-sm" id="addProviderBtn">+ 添加 Provider</button>
        </div>
      </div>
      <div id="providerList" style="display:flex;flex-direction:column;gap:16px"></div>
    </div>
  `;

  container.querySelector('#addProviderBtn').addEventListener('click', addProvider);
  container.querySelector('#probeAllBtn').addEventListener('click', () => probeAll({ force: true }));
  container.querySelector('#refreshModelsBtn').addEventListener('click', refreshModels);

  await loadProviders();
  await autoProbeAll();
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
    
    // 预加载所有 Provider 类型的 models.dev 数据
    const providerTypes = [...new Set(providers.map(p => p.platform_type))];
    await Promise.all(providerTypes.map(type => loadModelsDevForType(type)));
    
    renderList();
  } catch (e) {
    console.error('[providers] loadProviders 失败:', e);
    toast('加载 Provider 列表失败', 'error');
  }
}

async function loadModelsDevForType(providerType) {
  if (_modelsDevCache[providerType]) return;
  try {
    const data = await get(`/providers/models-dev/${encodeURIComponent(providerType)}`);
    if (Array.isArray(data.models)) {
      _modelsDevCache[providerType] = data.models;
    }
  } catch (e) {
    console.warn('[providers] 加载 models.dev 模型失败:', providerType, e);
  }
}

function renderList() {
  const el = _root.querySelector('#providerList');
  if (!el) return;
  if (!providers.length) {
    el.innerHTML = '<div style="color:var(--text-3);padding:24px;text-align:center">暂无 Provider。点击上方按钮添加。</div>';
    return;
  }
  el.innerHTML = providers.map((p, i) => {
    if (editingIdx === i) return renderEditCard(p, i);
    return renderReadonlyCard(p, i);
  }).join('');
  bindCardEvents();
  _mountHealthSelects();
}

function _isProbeCacheValid(name) {
  const ps = probeStatus[name];
  if (!ps || !ps.timestamp) return false;
  return Date.now() - ps.timestamp < PROBE_CACHE_TTL;
}

function _getProbeCacheRemaining(name) {
  const ps = probeStatus[name];
  if (!ps || !ps.timestamp) return 0;
  return Math.max(0, PROBE_CACHE_TTL - (Date.now() - ps.timestamp));
}

function renderReadonlyCard(p, i) {
  const typeLabel = TYPE_LABEL_MAP[p.platform_type] || p.platform_type;
  const masked = maskKey(p.api_key);
  const hasKey = !!masked;
  const models = p.models || [];
  const name = p.name || '';
  const ps = probeStatus[name];
  const cacheValid = _isProbeCacheValid(name);
  const probeHtml = ps && cacheValid
    ? ps.ok
      ? `<span style="display:inline-flex;align-items:center;gap:4px;font-size:11px;color:var(--success)"><span class="status-dot running"></span>可用 ${ps.latency}ms</span>`
      : `<span style="display:inline-flex;align-items:center;gap:4px;font-size:11px;color:var(--danger)"><span class="status-dot error"></span>不可用</span>`
    : `<span style="display:inline-flex;align-items:center;gap:4px;font-size:11px;color:var(--text-3)"><span class="status-dot"></span>未检测</span>`;

  return `
    <div class="stat-card" data-idx="${i}" style="text-align:left">
      <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:12px">
        <div style="display:flex;align-items:center;gap:10px">
          <span class="tag ${p.enabled ? 'tag-success' : 'tag-danger'}" data-action="toggle-enabled" data-idx="${i}" style="cursor:pointer;user-select:none">${p.enabled ? '已启用' : '已禁用'}</span>
          <span class="tag tag-accent">${typeLabel}</span>
          ${probeHtml}
        </div>
        <div style="display:flex;gap:8px">
          <button class="btn btn-sm" data-action="probe" data-idx="${i}" data-name="${name}"${!hasKey ? ' disabled title="请先配置 API Key"' : ''}>可用性检查</button>
          <button class="btn btn-sm" data-action="edit" data-idx="${i}">编辑</button>
          <button class="btn btn-sm btn-danger" data-action="delete" data-idx="${i}">删除</button>
        </div>
      </div>
      <div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:12px">
        ${models.length ? models.map(m => _renderModelTag(m)).join('') : '<span style="color:var(--text-3);font-size:12px">无模型</span>'}
      </div>
      <div style="display:flex;gap:24px;font-size:12px;color:var(--text-2)">
        <div><span style="color:var(--text-3)">API Key:</span> ${hasKey ? masked : '<span style="color:var(--warn)">未配置</span>'}</div>
        <div><span style="color:var(--text-3)">Base URL:</span> ${p.base_url || '—'}</div>
      </div>
    </div>
  `;
}

function renderEditCard(p, i) {
  const isNew = p._new;
  const readonly = !isNew;
  const devModels = _modelsDevCache[p.platform_type] || [];
  const currentModels = new Set(p.models || []);
  return `
    <div class="stat-card" data-idx="${i}" style="text-align:left;border:1px solid var(--accent)">
      <div style="display:flex;flex-direction:column;gap:14px">
        <div class="form-group" style="margin:0">
          <label>平台类型</label>
          <select id="pv_type_${i}" ${readonly ? 'disabled' : ''}>
            ${TYPE_OPTIONS.map(o => `<option value="${o.value}" ${o.value === p.platform_type ? 'selected' : ''}>${o.label}</option>`).join('')}
          </select>
        </div>
        <div class="form-group" style="margin:0">
          <label>Base URL</label>
          <input type="text" id="pv_url_${i}" value="${p.base_url || ''}" placeholder="自动填充" ${readonly ? 'disabled' : ''}>
        </div>
        <div class="form-group" style="margin:0">
          <label>API Key</label>
          <input type="password" id="pv_key_${i}" value="${p.api_key || ''}" placeholder="sk-...">
        </div>
        <div class="form-group" style="margin:0">
          <label>模型列表</label>
          <div style="display:flex;flex-wrap:wrap;gap:6px;margin-bottom:8px" id="pv_models_${i}">
            ${(p.models || []).map((m, mi) => _renderModelTag(m, `<span data-remove-model="${mi}" style="cursor:pointer;margin-left:4px;color:var(--danger)">&times;</span>`)).join('')}
          </div>
          <div style="display:flex;gap:8px;margin-bottom:8px">
            <input type="text" id="pv_newModel_${i}" placeholder="输入模型名后回车添加" style="flex:1">
            <button type="button" class="btn btn-sm" data-action="addModel" data-idx="${i}">添加</button>
          </div>
          ${devModels.length ? `
          <div style="border:1px solid var(--border);border-radius:6px;padding:10px;background:var(--bg-1)">
            <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px">
              <span style="font-size:12px;color:var(--text-2);font-weight:600">models.dev 可用模型 (${devModels.length})</span>
              <input type="text" id="pv_devSearch_${i}" placeholder="搜索模型名…" style="flex:1;padding:4px 8px;font-size:12px;border:1px solid var(--border);border-radius:4px;background:var(--bg-0)">
            </div>
            <div style="display:flex;gap:4px;flex-wrap:wrap;margin-bottom:8px" id="pv_devFilters_${i}">
              <span class="cap-filter cap-tool" data-filter-cap="tool_call" data-idx="${i}">函数调用</span>
              <span class="cap-filter cap-reason" data-filter-cap="reasoning" data-idx="${i}">推理</span>
              <span class="cap-filter cap-vision" data-filter-cap="vision" data-idx="${i}">视觉</span>
              <span class="cap-filter cap-audio" data-filter-cap="audio" data-idx="${i}">音频</span>
            </div>
            <div style="display:grid;grid-template-columns:repeat(4,1fr);gap:6px;max-height:320px;overflow-y:auto" id="pv_devModels_${i}">
              ${devModels.map(m => {
                const added = currentModels.has(m.id);
                const badges = [];
                if (m.tool_call) badges.push('<span class="cap-tag cap-tool">函数调用</span>');
                if (m.reasoning) badges.push('<span class="cap-tag cap-reason">推理</span>');
                if (m.vision) badges.push('<span class="cap-tag cap-vision">视觉</span>');
                if (m.audio) badges.push('<span class="cap-tag cap-audio">音频</span>');
                const ctxLabel = m.context > 0 ? (m.context >= 1000000 ? `${(m.context/1000000).toFixed(0)}M tokens` : `${Math.round(m.context/1000)}K tokens`) : '';
                const costLabel = m.input_cost > 0 ? `¥${(m.input_cost * 7.25).toFixed(1)}/¥${(m.output_cost * 7.25).toFixed(1)}` : '';
                return `<div class="model-card${added ? ' model-card-added' : ''}" data-dev-model="${m.id}" data-idx="${i}" data-cap-tool_call="${m.tool_call}" data-cap-reasoning="${m.reasoning}" data-cap-vision="${m.vision}" data-cap-audio="${m.audio}" style="cursor:${added ? 'default' : 'pointer'};opacity:${added ? '0.55' : '1'}">
                  <div style="font-size:12px;font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-bottom:6px" title="${m.name}">${m.id}</div>
                  <div style="display:flex;align-items:center;gap:3px;flex-wrap:wrap;margin-bottom:4px">${badges.join('') || '<span style="font-size:10px;color:var(--text-3)">—</span>'}</div>
                  <div style="display:flex;justify-content:space-between;font-size:10px;color:var(--text-3)">
                    <span>${ctxLabel}</span><span>${costLabel}</span>
                  </div>
                  ${added ? '<div style="position:absolute;top:4px;right:6px;font-size:10px;color:var(--success)">✓ 已添加</div>' : ''}
                </div>`;
              }).join('')}
            </div>
          </div>
          ` : '<div id="pv_devModelsWrap_${i}" style="font-size:11px;color:var(--text-3)">正在加载 models.dev 模型列表…</div>'}
        </div>
        <div class="form-group" style="margin:0">
          <label>Healthcheck Model</label>
          <div id="msel_health_${i}" data-health-idx="${i}"></div>
          <div style="font-size:11px;color:var(--text-3);margin-top:4px">请先在上方模型列表中添加模型，再选择用于连通性检测的模型</div>
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
  const el = _root.querySelector('#providerList');
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
      else if (action === 'toggle-enabled') toggleEnabled(idx);
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
      const urlInput = el.querySelector(`#pv_url_${idx}`);
      if (BUILTIN_TYPES.includes(newType) && urlInput) {
        urlInput.value = DEFAULT_URLS[newType] || '';
      }
      loadModelsDevForEdit(idx);
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

  el.querySelectorAll('[data-dev-model]').forEach(span => {
    span.addEventListener('click', (e) => {
      e.stopPropagation();
      const model = span.dataset.devModel;
      const idx = parseInt(span.dataset.idx, 10);
      if (!providers[idx].models) providers[idx].models = [];
      const pos = providers[idx].models.indexOf(model);
      if (pos !== -1) {
        providers[idx].models.splice(pos, 1);
      } else {
        providers[idx].models.push(model);
      }
      const scrollContainer = el.querySelector(`#pv_devModels_${idx}`);
      const scrollTop = scrollContainer ? scrollContainer.scrollTop : 0;
      const searchInput = el.querySelector(`#pv_devSearch_${idx}`);
      const searchQuery = searchInput ? searchInput.value : '';
      const filterBar = el.querySelector(`#pv_devFilters_${idx}`);
      const activeCaps = filterBar
        ? [...filterBar.querySelectorAll('.cap-filter-active')].map(t => t.dataset.filterCap)
        : [];
      renderList();
      requestAnimationFrame(() => {
        const newContainer = _root.querySelector(`#pv_devModels_${idx}`);
        if (newContainer) newContainer.scrollTop = scrollTop;
        const newSearch = _root.querySelector(`#pv_devSearch_${idx}`);
        if (newSearch && searchQuery) newSearch.value = searchQuery;
        const newFilterBar = _root.querySelector(`#pv_devFilters_${idx}`);
        if (newFilterBar && activeCaps.length) {
          newFilterBar.querySelectorAll('[data-filter-cap]').forEach(t => {
            if (activeCaps.includes(t.dataset.filterCap)) t.classList.add('cap-filter-active');
          });
        }
        applyDevFilters(_root, idx);
      });
    });
  });

  el.querySelectorAll('[id^="pv_devSearch_"]').forEach(input => {
    input.addEventListener('input', () => {
      const idx = parseInt(input.id.replace('pv_devSearch_', ''), 10);
      applyDevFilters(el, idx);
    });
  });

  el.querySelectorAll('[data-filter-cap]').forEach(tag => {
    tag.addEventListener('click', (e) => {
      e.stopPropagation();
      tag.classList.toggle('cap-filter-active');
      const idx = parseInt(tag.dataset.idx, 10);
      applyDevFilters(el, idx);
    });
  });
}

function applyDevFilters(root, idx) {
  const container = root.querySelector(`#pv_devModels_${idx}`);
  if (!container) return;
  const searchInput = root.querySelector(`#pv_devSearch_${idx}`);
  const query = searchInput ? searchInput.value.trim().toLowerCase() : '';
  const filterBar = root.querySelector(`#pv_devFilters_${idx}`);
  const activeCaps = new Set();
  if (filterBar) {
    filterBar.querySelectorAll('.cap-filter-active').forEach(t => {
      activeCaps.add(t.dataset.filterCap);
    });
  }
  container.querySelectorAll('[data-dev-model]').forEach(card => {
    const model = card.dataset.devModel;
    const title = card.querySelector('[title]');
    const name = title ? title.getAttribute('title').toLowerCase() : '';
    const textMatch = !query || model.toLowerCase().includes(query) || name.includes(query);
    let capMatch = true;
    if (activeCaps.size > 0) {
      capMatch = true;
      for (const cap of activeCaps) {
        if (card.getAttribute(`data-cap-${cap}`) !== 'true') { capMatch = false; break; }
      }
    }
    card.style.display = (textMatch && capMatch) ? '' : 'none';
  });
}

function startEdit(idx) {
  editingIdx = idx;
  renderList();
  const urlInput = _root.querySelector(`#pv_url_${idx}`);
  if (BUILTIN_TYPES.includes(providers[idx].platform_type) && urlInput && !urlInput.value) {
    urlInput.value = DEFAULT_URLS[providers[idx].platform_type] || '';
  }
  loadModelsDevForEdit(idx);
}

async function loadModelsDevForEdit(idx) {
  const p = providers[idx];
  const providerType = p.platform_type;
  await loadModelsDevForType(providerType);
  if (editingIdx === idx) renderList();
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
    _renderModelTag(m, `<span data-remove-modal-model="${mi}" style="cursor:pointer;margin-left:4px;color:var(--danger)">&times;</span>`)
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
  p.platform_type = _root.querySelector(`#pv_type_${idx}`).value;
  p.base_url = _root.querySelector(`#pv_url_${idx}`).value.trim();
  p.api_key = _root.querySelector(`#pv_key_${idx}`).value.trim();
  p.healthcheck_model = _healthSelects[idx]?.value || '';
  delete p._new;
  editingIdx = null;
  await saveAll();
}

async function deleteProvider(idx) {
  providers.splice(idx, 1);
  editingIdx = null;
  await saveAll();
}

async function toggleEnabled(idx) {
  providers[idx].enabled = !providers[idx].enabled;
  await saveAll();
}

function addModelToProvider(idx) {
  const input = _root.querySelector(`#pv_newModel_${idx}`);
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
    name: p.name || undefined,
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
    flashSuccess(_root.querySelector('#addProviderBtn'));
    await loadProviders();
  } catch (e) {
    console.error('[providers] saveAll 失败:', e);
    toast('保存失败', 'error');
  }
}

function maskKey(key) {
  if (key === '' || key === null || key === undefined) return '';
  if (key.length <= 8) return '****';
  return key.slice(0, 4) + '****' + key.slice(-4);
}

async function probeProvider(btn) {
  const name = btn.dataset.name;
  if (!name) return;

  if (_isProbeCacheValid(name)) {
    const remaining = Math.ceil(_getProbeCacheRemaining(name) / 1000);
    toast(`${name} 检测结果已缓存 (${remaining}s 后过期)`, 'info');
    return;
  }

  btn.disabled = true;
  btn.textContent = '检查中…';

  try {
    const res = await post('/providers/probe', { name });
    if (res.success) {
      probeStatus[name] = { ok: true, latency: res.latency_ms || 0, timestamp: Date.now() };
      toast(`${name} 可用 (${res.latency_ms}ms)`, 'success');
    } else {
      probeStatus[name] = { ok: false, latency: 0, timestamp: Date.now() };
      toast(`${name} 不可用: ${res.error || '未知错误'}`, 'error');
    }
  } catch (e) {
    probeStatus[name] = { ok: false, latency: 0, timestamp: Date.now() };
    toast(`检查失败: ${e.message}`, 'error');
  }

  renderList();
}

async function probeAll(options = {}) {
  const { force = false, silent = false } = options;
  const btn = _root.querySelector('#probeAllBtn');
  if (!providers.length) {
    if (!silent) toast('暂无 Provider 可检测', 'warning');
    return;
  }

  if (btn) {
    btn.disabled = true;
    btn.textContent = '检测中…';
  }

  const tasks = providers
    .filter(p => p.name && p.enabled)
    .filter(p => force || !_isProbeCacheValid(p.name))
    .map(p => p.name);

  if (!tasks.length) {
    if (btn) {
      btn.disabled = false;
      btn.textContent = '全部检测';
    }
    if (!silent) toast('所有检测结果均在缓存有效期内', 'info');
    return;
  }

  let okCount = 0;
  let failCount = 0;

  for (const name of tasks) {
    try {
      const res = await post('/providers/probe', { name });
      if (res.success) {
        probeStatus[name] = { ok: true, latency: res.latency_ms || 0, timestamp: Date.now() };
        okCount++;
      } else {
        probeStatus[name] = { ok: false, latency: 0, timestamp: Date.now() };
        failCount++;
      }
    } catch {
      probeStatus[name] = { ok: false, latency: 0, timestamp: Date.now() };
      failCount++;
    }
    renderList();
  }

  if (btn) {
    btn.disabled = false;
    btn.textContent = '全部检测';
  }
  if (!silent) {
    toast(`检测完成: ${okCount} 可用, ${failCount} 不可用`, okCount > 0 ? 'success' : 'error');
  }
}

async function autoProbeAll() {
  await probeAll({ force: false, silent: true });
}

async function refreshModels() {
  const btn = _root.querySelector('#refreshModelsBtn');
  btn.disabled = true;
  btn.textContent = '刷新中…';

  try {
    const res = await post('/providers/refresh-models', { force: true });
    if (res.success) {
      const raw = Array.isArray(res.providers) ? res.providers : [];
      providers = raw.map(p => ({
        ...p,
        platform_type: p.platform_type || p.type || 'openai-compatible',
      }));
      editingIdx = null;
      renderList();
      if (res.changed) {
        const total = providers.reduce((s, p) => s + (p.models ? p.models.length : 0), 0);
        toast(`模型列表已刷新，共 ${total} 个模型`, 'success');
      } else {
        toast('模型列表无变化', 'info');
      }
    } else {
      toast(`刷新失败: ${res.error || '未知错误'}`, 'error');
    }
  } catch (e) {
    console.error('[providers] refreshModels 失败:', e);
    toast(`刷新失败: ${e.message}`, 'error');
  }

  btn.disabled = false;
  btn.textContent = '刷新模型';
}
