import { store } from '../store.js';
import { get, post } from '../app.js';
import { toast, flashSuccess, $, ModelSelect } from '../components.js';

const TASK_GROUPS = [
  {
    title: '分析类',
    generalKey: 'analysis_model',
    tasks: [
      { key: 'cognition_analyze', label: '认知分析（情绪+意图）' },
      { key: 'memory_extract', label: '记忆提取' },
    ],
  },
  {
    title: '记忆维护',
    generalKey: 'memory_model',
    tasks: [
      { key: 'diary_generate', label: '日记生成' },
      { key: 'diary_consolidate', label: '日记合并' },
      { key: 'biography_distill', label: '传记蒸馏' },
      { key: 'biography_update', label: '传记更新' },
    ],
  },
  {
    title: '插件与技能',
    generalKey: 'plugin_model',
    tasks: [
      { key: 'plugin_generate', label: '插件生成' },
      { key: 'plugin_analyze', label: '插件分析' },
      { key: 'plugin_render', label: '插件渲染' },
      { key: 'plugin_raw', label: '插件原生调用' },
    ],
  },
];

const PARAM_GROUPS = [
  {
    id: 'cognition',
    title: '认知分析',
    tasks: [
      { key: 'cognition_analyze', label: '认知分析' },
      { key: 'memory_extract', label: '记忆提取' },
    ],
  },
  {
    id: 'chat',
    title: '对话生成',
    tasks: [
      { key: 'response_generate', label: '回复生成' },
      { key: 'proactive_generate', label: '主动发言' },
      { key: 'passive_skill', label: '被动技能' },
      { key: 'sidekick_execute', label: '助手执行' },
      { key: 'github_monitor_notify', label: 'GitHub 监控' },
    ],
  },
  {
    id: 'memory',
    title: '记忆维护',
    tasks: [
      { key: 'diary_generate', label: '日记生成' },
      { key: 'diary_consolidate', label: '日记整合' },
      { key: 'topic_cluster', label: '主题聚类' },
      { key: 'biography_distill', label: '传记提炼' },
      { key: 'biography_update', label: '传记更新' },
    ],
  },
  {
    id: 'plugin',
    title: '插件系统',
    tasks: [
      { key: 'plugin_analyze', label: '插件分析' },
      { key: 'plugin_generate', label: '插件生成' },
      { key: 'plugin_render', label: '插件渲染' },
      { key: 'plugin_raw', label: '插件原生调用' },
    ],
  },
];

const NUMERIC_PARAMS = [
  { key: 'temperature', label: 'Temp', step: '0.1', min: '0', max: '2' },
  { key: 'max_tokens', label: 'Tokens', step: '1', min: '1', max: '65536' },
  { key: 'timeout', label: '超时(s)', step: '1', min: '1', max: '300' },
];

function _stripProviderPrefix(value) {
  if (!value) return '';
  const idx = value.indexOf('/');
  return idx >= 0 ? value.substring(idx + 1) : value;
}

function _resolveCompositeValue(bareName, options) {
  if (!bareName) return '';
  const exact = options.find(o => o.value === bareName);
  if (exact) return exact.value;
  const suffix = options.find(o => o.value.endsWith('/' + bareName));
  return suffix ? suffix.value : bareName;
}

let orchestrationData = null;
let taskParamDefaults = {};
let taskParamOverrides = {};
let modelChoices = [];
const modelSelects = {};

export async function init(container, params) {
  const name = store.currentPersona;
  if (!name) {
    container.innerHTML = `
      <div class="card">
        <div class="card-header">
          <div class="card-title">模型编排</div>
        </div>
        <div style="padding:40px;text-align:center;color:var(--text-3)">
          <div style="font-size:48px;margin-bottom:16px">✦</div>
          <div style="font-size:16px;margin-bottom:8px">请先选择人格</div>
          <div style="font-size:13px">在侧边栏中选择要配置的人格</div>
        </div>
      </div>
    `;
    return;
  }

  container.innerHTML = `
    <div class="card">
      <div class="card-header">
        <div>
          <div class="card-title">模型编排</div>
          <div class="card-subtitle">配置 ${name} 的模型分配与任务参数</div>
        </div>
        <div style="display:flex;gap:8px">
          <button class="btn btn-ghost" id="orchReset">重置参数</button>
          <button class="btn btn-primary" id="orchSave" disabled>保存</button>
        </div>
      </div>
      <div id="orchContent">
        <div style="padding:20px;color:var(--text-3)">加载中...</div>
      </div>
    </div>
  `;

  $('orchSave')?.addEventListener('click', () => saveOrchestration(name));
  $('orchReset')?.addEventListener('click', () => resetTaskParams(name));
  await loadOrchestration(name);
}

async function loadOrchestration(name) {
  try {
    const [orchData, paramsData] = await Promise.all([
      get(`/personas/${name}/orchestration`),
      get(`/personas/${name}/task-params`),
    ]);
    orchestrationData = orchData;
    modelChoices = orchData.model_choices || [];
    taskParamDefaults = paramsData.defaults || {};
    taskParamOverrides = paramsData.task_params || {};
    renderOrchestration(orchData);
    $('orchSave').disabled = false;
  } catch (e) {
    $('orchContent').innerHTML = `<div style="padding:20px;color:var(--danger)">加载失败: ${e.message}</div>`;
    $('orchSave').disabled = true;
  }
}

// ── 模型编排区域 ──

function renderOrchestration(data) {
  const el = $('orchContent');
  const taskModels = data.task_models || {};
  const taskEnabled = data.task_enabled || {};
  const opts = _mselOptions();

  let html = `
    <div style="margin-bottom:24px">
      <div style="font-size:14px;font-weight:600;margin-bottom:12px;color:var(--text-1)">通用模型设置</div>
      <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:16px">
        <div class="form-group">
          <label>分析模型</label>
          <div id="msel_analysis"></div>
        </div>
        <div class="form-group">
          <label>对话模型</label>
          <div id="msel_chat"></div>
        </div>
        <div class="form-group">
          <label>记忆模型</label>
          <div id="msel_memory"></div>
        </div>
        <div class="form-group">
          <label>插件模型</label>
          <div id="msel_plugin"></div>
        </div>
      </div>
    </div>
  `;

  for (const group of TASK_GROUPS) {
    html += `
      <div style="margin-bottom:24px">
        <div style="font-size:14px;font-weight:600;margin-bottom:12px;color:var(--text-1)">${group.title}</div>
        <div style="display:flex;flex-direction:column;gap:8px">
    `;

    for (const task of group.tasks) {
      const taskModel = taskModels[task.key];
      const isEnabled = taskEnabled[task.key] !== false;
      const isOverridden = taskModel && taskModel !== '__inherit__';
      html += `
        <div style="display:flex;align-items:center;gap:12px;padding:8px 12px;background:var(--bg-secondary);border-radius:8px">
          <label style="display:flex;align-items:center;gap:8px;cursor:pointer;min-width:180px;font-size:13px">
            <input type="checkbox" class="task-enabled" data-task="${task.key}"${isEnabled ? ' checked' : ''}>
            ${task.label}
          </label>
          <div class="task-model-select" data-task="${task.key}" data-value="${taskModel || '__inherit__'}" style="flex:1;${!isOverridden ? 'opacity:0.5;pointer-events:none' : ''}"></div>
          <label style="display:flex;align-items:center;gap:4px;font-size:12px;color:var(--text-2);cursor:pointer">
            <input type="checkbox" class="task-override" data-task="${task.key}"${isOverridden ? ' checked' : ''}>
            覆盖
          </label>
        </div>
      `;
    }

    html += `</div></div>`;
  }

  // ── 参数调优折叠区 ──
  html += `
    <div style="margin-top:8px;border:1px solid var(--border);border-radius:10px;overflow:hidden">
      <div id="tpToggle" style="display:flex;align-items:center;gap:10px;padding:12px 16px;cursor:pointer;user-select:none;background:var(--bg-secondary);transition:background 0.15s">
        <span id="tpChevron" style="font-size:11px;transition:transform 0.2s;color:var(--text-3)">▶</span>
        <span style="font-size:14px;font-weight:600;color:var(--text-1)">参数调优</span>
        <span style="font-size:12px;color:var(--text-3);margin-left:auto">temperature / tokens / timeout / fallback</span>
      </div>
      <div id="tpBody" style="display:none;padding:0"></div>
    </div>
  `;

  el.innerHTML = html;
  _mountModelSelects(data);
  setupOverrideListeners();
  renderParamTuning();
  bindParamToggle();
  bindParamEvents();
}

function _mselOptions() {
  return modelChoices.map(m => {
    const val = typeof m === 'object' ? m.value : m;
    const label = typeof m === 'object' ? m.label : m;
    const tags = (typeof m === 'object' && Array.isArray(m.tags)) ? m.tags : [];
    return { value: val, label, tags };
  });
}

function _mountModelSelects(data) {
  const fields = ['analysis', 'chat', 'memory', 'plugin'];
  const baseOpts = _mselOptions();

  for (const field of fields) {
    const container = $(`msel_${field}`);
    if (!container) continue;
    const key = `${field}_model`;
    const bareValue = data[key] || '';
    const value = _resolveCompositeValue(bareValue, baseOpts);

    const valueInChoices = baseOpts.some(o => o.value === value);
    const opts = [...baseOpts];
    if (value && !valueInChoices) {
      opts.unshift({ value: value, label: `${value} (当前配置)`, tags: [] });
    }

    const sel = new ModelSelect({ options: opts, value });
    sel.mount(container);
    modelSelects[key] = sel;
  }

  const taskOpts = [
    { value: '__inherit__', label: '继承通用', tags: [] },
    ...baseOpts,
  ];
  document.querySelectorAll('.task-model-select').forEach(container => {
    const task = container.dataset.task;
    const bareValue = container.dataset.value || '__inherit__';
    const value = bareValue === '__inherit__' ? bareValue : _resolveCompositeValue(bareValue, taskOpts);
    const key = `task_${task}`;

    const valueInChoices = taskOpts.some(o => o.value === value);
    const opts = [...taskOpts];
    if (value && value !== '__inherit__' && !valueInChoices) {
      opts.splice(1, 0, { value: value, label: `${value} (当前配置)`, tags: [] });
    }

    const sel = new ModelSelect({ options: opts, value, placeholder: '继承通用' });
    sel.mount(container);
    modelSelects[key] = sel;
  });
}

function setupOverrideListeners() {
  document.querySelectorAll('.task-override').forEach(cb => {
    cb.addEventListener('change', () => {
      const task = cb.dataset.task;
      const container = document.querySelector(`.task-model-select[data-task="${task}"]`);
      if (!container) return;

      if (cb.checked) {
        container.style.opacity = '1';
        container.style.pointerEvents = 'auto';
      } else {
        container.style.opacity = '0.5';
        container.style.pointerEvents = 'none';
        const key = `task_${task}`;
        if (modelSelects[key]) {
          modelSelects[key].setValue('__inherit__');
        }
      }
    });
  });
}

// ── 参数调优区域 ──

function renderParamTuning() {
  const body = $('tpBody');
  if (!body) return;

  let html = '';
  for (const group of PARAM_GROUPS) {
    html += `
      <div style="border-top:1px solid var(--border-light)">
        <div style="padding:8px 16px;font-size:12px;font-weight:600;color:var(--text-2);background:var(--bg-tertiary)">${group.title}</div>
    `;
    for (const task of group.tasks) {
      html += renderParamRow(task);
    }
    html += `</div>`;
  }
  body.innerHTML = html;

  // 挂载 fallback ModelSelect
  const fbOpts = [
    { value: '', label: '无 fallback', tags: [] },
    ..._mselOptions(),
  ];
  document.querySelectorAll('.fb-model-select').forEach(container => {
    const task = container.dataset.task;
    const overrides = taskParamOverrides[task] || {};
    const saved = overrides.fallback_model || '';
    const value = saved ? _resolveCompositeValue(saved, fbOpts) : '';

    const valueInChoices = fbOpts.some(o => o.value === value);
    const opts = [...fbOpts];
    if (value && !valueInChoices) {
      opts.splice(1, 0, { value, label: `${value} (当前配置)`, tags: [] });
    }

    const sel = new ModelSelect({ options: opts, value, placeholder: '无 fallback' });
    sel.mount(container);
    modelSelects[`fb_${task}`] = sel;
  });
}

function renderParamRow(task) {
  const overrides = taskParamOverrides[task.key] || {};
  const defs = taskParamDefaults[task.key] || {};

  let cells = `<div style="min-width:140px;padding:8px 12px;font-size:13px;color:var(--text-1);font-weight:500">${task.label}</div>`;

  // 数值参数
  for (const f of NUMERIC_PARAMS) {
    const saved = overrides[f.key];
    const hasOverride = saved !== null && saved !== undefined && saved !== '';
    const defVal = defs[f.key];
    const defDisplay = defVal != null ? String(defVal) : '';

    cells += `
      <div style="display:flex;flex-direction:column;gap:2px;padding:6px 4px;min-width:0">
        <div style="font-size:10px;color:var(--text-3);display:flex;align-items:center;gap:3px">
          ${f.label}
          <span class="tp-dot" style="display:${hasOverride ? 'inline' : 'none'};color:var(--accent);font-size:9px">●</span>
        </div>
        <div style="display:flex;align-items:center;gap:2px">
          <input type="number" class="tp-num" data-task="${task.key}" data-field="${f.key}"
            value="${hasOverride ? String(saved) : ''}" placeholder="${defDisplay}"
            step="${f.step}" min="${f.min}" max="${f.max}"
            style="width:72px;padding:4px 6px;border:1px solid ${hasOverride ? 'var(--accent)' : 'var(--border)'};border-radius:5px;background:var(--bg-secondary);color:var(--text-1);font-size:11px;font-family:var(--font-mono)">
          <button class="tp-clr" data-task="${task.key}" data-field="${f.key}"
            style="border:none;background:none;cursor:pointer;color:var(--text-3);font-size:13px;padding:1px 3px;line-height:1;visibility:${hasOverride ? 'visible' : 'hidden'}">×</button>
        </div>
      </div>
    `;
  }

  // fallback_model ModelSelect
  const fbSaved = overrides.fallback_model;
  const hasFb = fbSaved !== null && fbSaved !== undefined && fbSaved !== '';
  cells += `
    <div style="display:flex;flex-direction:column;gap:2px;padding:6px 4px;min-width:0;flex:1">
      <div style="font-size:10px;color:var(--text-3);display:flex;align-items:center;gap:3px">
        Fallback
        <span class="tp-dot" style="display:${hasFb ? 'inline' : 'none'};color:var(--accent);font-size:9px">●</span>
      </div>
      <div class="fb-model-select" data-task="${task.key}" style="min-width:160px;max-width:260px"></div>
    </div>
  `;

  return `
    <div class="tp-row" data-task="${task.key}"
      style="display:grid;grid-template-columns:140px repeat(3,minmax(80px,1fr)) minmax(180px,1.5fr);gap:0;padding:0 12px;border-top:1px solid var(--border-light);align-items:center">
      ${cells}
    </div>
  `;
}

function bindParamToggle() {
  const toggle = $('tpToggle');
  const body = $('tpBody');
  const chevron = $('tpChevron');
  if (!toggle || !body) return;

  toggle.addEventListener('click', () => {
    const isOpen = body.style.display !== 'none';
    body.style.display = isOpen ? 'none' : 'block';
    if (chevron) chevron.style.transform = isOpen ? '' : 'rotate(90deg)';
    toggle.style.background = isOpen ? 'var(--bg-secondary)' : 'var(--bg-tertiary)';
  });
}

function bindParamEvents() {
  const content = $('orchContent');
  if (!content) return;

  content.addEventListener('input', (e) => {
    const input = e.target.closest('.tp-num');
    if (!input) return;
    const cell = input.closest('div');
    if (!cell) return;
    const dot = cell.querySelector('.tp-dot');
    const clr = cell.querySelector('.tp-clr');
    const has = input.value.trim() !== '';
    input.style.borderColor = has ? 'var(--accent)' : 'var(--border)';
    if (dot) dot.style.display = has ? 'inline' : 'none';
    if (clr) clr.style.visibility = has ? 'visible' : 'hidden';
  });

  content.addEventListener('click', (e) => {
    const clr = e.target.closest('.tp-clr');
    if (!clr) return;
    const task = clr.dataset.task;
    const field = clr.dataset.field;
    const input = document.querySelector(`.tp-num[data-task="${task}"][data-field="${field}"]`);
    if (!input) return;
    const cell = input.closest('div');
    if (!cell) return;
    input.value = '';
    input.style.borderColor = 'var(--border)';
    const dot = cell.querySelector('.tp-dot');
    if (dot) dot.style.display = 'none';
    clr.style.visibility = 'hidden';
  });
}

// ── 保存 ──

async function saveOrchestration(name) {
  // 1) 模型编排
  const taskModels = {};
  const taskEnabled = {};
  document.querySelectorAll('.task-model-select').forEach(container => {
    const task = container.dataset.task;
    const enabled = document.querySelector(`.task-enabled[data-task="${task}"]`)?.checked ?? true;
    const isOverridden = document.querySelector(`.task-override[data-task="${task}"]`)?.checked ?? false;
    const key = `task_${task}`;
    const sel = modelSelects[key];
    const rawVal = isOverridden && sel ? sel.value : '__inherit__';
    taskModels[task] = rawVal === '__inherit__' ? rawVal : _stripProviderPrefix(rawVal);
    taskEnabled[task] = enabled;
  });

  // 2) 任务参数
  const taskTemperatures = {};
  const taskMaxTokens = {};
  const taskTimeout = {};
  const taskFallbackModel = {};

  document.querySelectorAll('.tp-num').forEach(input => {
    const task = input.dataset.task;
    const field = input.dataset.field;
    const raw = input.value.trim();
    if (raw === '') return;
    const v = field === 'max_tokens' ? parseInt(raw, 10) : parseFloat(raw);
    if (isNaN(v)) return;
    if (field === 'temperature') taskTemperatures[task] = v;
    else if (field === 'max_tokens' && v > 0) taskMaxTokens[task] = v;
    else if (field === 'timeout' && v > 0) taskTimeout[task] = v;
  });

  for (const [key, sel] of Object.entries(modelSelects)) {
    if (!key.startsWith('fb_')) continue;
    const task = key.substring(3);
    const raw = sel.value?.trim();
    if (raw) taskFallbackModel[task] = _stripProviderPrefix(raw);
  }

  try {
    await post(`/personas/${name}/orchestration`, {
      analysis_model: _stripProviderPrefix(modelSelects.analysis_model?.value || ''),
      chat_model: _stripProviderPrefix(modelSelects.chat_model?.value || ''),
      memory_model: _stripProviderPrefix(modelSelects.memory_model?.value || ''),
      plugin_model: _stripProviderPrefix(modelSelects.plugin_model?.value || ''),
      task_models: taskModels,
      task_enabled: taskEnabled,
    });

    await post(`/personas/${name}/task-params`, {
      task_temperatures: taskTemperatures,
      task_max_tokens: taskMaxTokens,
      task_timeout: taskTimeout,
      task_fallback_model: taskFallbackModel,
    });

    flashSuccess($('orchSave'));
    toast('模型编排与参数已保存', 'success');
  } catch (e) {
    toast('保存失败: ' + e.message, 'error');
  }
}

async function resetTaskParams(name) {
  if (!confirm('确定要重置所有任务参数为默认值吗？')) return;
  try {
    await post(`/personas/${name}/task-params`, {
      task_temperatures: {},
      task_max_tokens: {},
      task_timeout: {},
      task_fallback_model: {},
    });
    toast('任务参数已重置为默认值', 'success');
    await loadOrchestration(name);
  } catch (e) {
    toast('重置失败: ' + e.message, 'error');
  }
}
