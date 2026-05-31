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
          <div class="card-subtitle">配置 ${name} 的模型分配策略</div>
        </div>
        <button class="btn btn-primary" id="orchSave" disabled>保存</button>
      </div>
      <div id="orchContent">
        <div style="padding:20px;color:var(--text-3)">加载中...</div>
      </div>
    </div>
  `;

  const saveBtn = $('orchSave');
  if (saveBtn) {
    saveBtn.addEventListener('click', () => saveOrchestration(name));
  }
  await loadOrchestration(name);
}

async function loadOrchestration(name) {
  try {
    const data = await get(`/personas/${name}/orchestration`);
    orchestrationData = data;
    modelChoices = data.model_choices || [];
    renderOrchestration(data);
    const saveBtn = $('orchSave');
    if (saveBtn) saveBtn.disabled = false;
  } catch (e) {
    const el = $('orchContent');
    if (el) el.innerHTML = `<div style="padding:20px;color:var(--danger)">加载失败: ${e.message}</div>`;
    const saveBtn = $('orchSave');
    if (saveBtn) saveBtn.disabled = true;
  }
}

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

  el.innerHTML = html;
  _mountModelSelects(data);
  setupOverrideListeners();
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
  
  // 挂载通用模型选择器
  for (const field of fields) {
    const container = $(`msel_${field}`);
    if (!container) continue;
    const key = `${field}_model`;
    const bareValue = data[key] || '';
    const value = _resolveCompositeValue(bareValue, baseOpts);
    
    // 如果当前值不在选项列表中，添加一个额外的选项
    const valueInChoices = baseOpts.some(o => o.value === value);
    const opts = [...baseOpts];
    if (value && !valueInChoices) {
      opts.unshift({ value: value, label: `${value} (当前配置)`, tags: [] });
    }
    
    const sel = new ModelSelect({
      options: opts,
      value: value,
    });
    sel.mount(container);
    modelSelects[key] = sel;
  }
  
  // 挂载任务模型选择器
  const taskOpts = [
    { value: '__inherit__', label: '继承通用', tags: [] },
    ...baseOpts,
  ];
  document.querySelectorAll('.task-model-select').forEach(container => {
    const task = container.dataset.task;
    const bareValue = container.dataset.value || '__inherit__';
    const value = bareValue === '__inherit__' ? bareValue : _resolveCompositeValue(bareValue, taskOpts);
    const key = `task_${task}`;
    
    // 如果当前值不在选项列表中，添加一个额外的选项
    const valueInChoices = taskOpts.some(o => o.value === value);
    const opts = [...taskOpts];
    if (value && value !== '__inherit__' && !valueInChoices) {
      opts.splice(1, 0, { value: value, label: `${value} (当前配置)`, tags: [] });
    }
    
    const sel = new ModelSelect({
      options: opts,
      value: value,
      placeholder: '继承通用',
    });
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
        // 重置为继承通用
        const key = `task_${task}`;
        if (modelSelects[key]) {
          modelSelects[key].setValue('__inherit__');
        }
      }
    });
  });
}

async function saveOrchestration(name) {
  const taskModels = {};
  const taskEnabled = {};
  document.querySelectorAll('.task-model-select').forEach(container => {
    const task = container.dataset.task;
    const enabled = document.querySelector(`.task-enabled[data-task="${task}"]`).checked;
    const isOverridden = document.querySelector(`.task-override[data-task="${task}"]`).checked;
    const key = `task_${task}`;
    const sel = modelSelects[key];
    const rawVal = isOverridden && sel ? sel.value : '__inherit__';
    taskModels[task] = rawVal === '__inherit__' ? rawVal : _stripProviderPrefix(rawVal);
    taskEnabled[task] = enabled;
  });

  try {
    await post(`/personas/${name}/orchestration`, {
      analysis_model: _stripProviderPrefix(modelSelects.analysis_model?.value || ''),
      chat_model: _stripProviderPrefix(modelSelects.chat_model?.value || ''),
      memory_model: _stripProviderPrefix(modelSelects.memory_model?.value || ''),
      plugin_model: _stripProviderPrefix(modelSelects.plugin_model?.value || ''),
      task_models: taskModels,
      task_enabled: taskEnabled,
    });
    flashSuccess($('orchSave'));
    toast('模型编排已保存', 'success');
  } catch (e) {
    toast('保存失败: ' + e.message, 'error');
  }
}
