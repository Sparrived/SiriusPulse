import { store } from '../store.js';
import { get, post } from '../app.js';
import { toast, flashSuccess, $ } from '../components.js';

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

let orchestrationData = null;
let modelChoices = [];

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
        <button class="btn btn-primary" id="orchSave">保存</button>
      </div>
      <div id="orchContent">
        <div style="padding:20px;color:var(--text-3)">加载中...</div>
      </div>
    </div>
  `;

  await loadOrchestration(name);

  $('orchSave').addEventListener('click', () => saveOrchestration(name));
}

async function loadOrchestration(name) {
  try {
    const data = await get(`/personas/${name}/orchestration`);
    orchestrationData = data;
    modelChoices = data.model_choices || [];
    renderOrchestration(data);
  } catch (e) {
    $('orchContent').innerHTML = `<div style="padding:20px;color:var(--danger)">加载失败: ${e.message}</div>`;
  }
}

function renderOrchestration(data) {
  const el = $('orchContent');
  const taskModels = data.task_models || {};
  const taskEnabled = data.task_enabled || {};

  let html = `
    <div style="margin-bottom:24px">
      <div style="font-size:14px;font-weight:600;margin-bottom:12px;color:var(--text-1)">通用模型设置</div>
      <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:16px">
        <div class="form-group">
          <label>分析模型</label>
          <div class="select-wrap">
            <select id="gen_analysis_model">${modelOptions(data.analysis_model)}</select>
          </div>
        </div>
        <div class="form-group">
          <label>对话模型</label>
          <div class="select-wrap">
            <select id="gen_chat_model">${modelOptions(data.chat_model)}</select>
          </div>
        </div>
        <div class="form-group">
          <label>记忆模型</label>
          <div class="select-wrap">
            <select id="gen_memory_model">${modelOptions(data.memory_model)}</select>
          </div>
        </div>
        <div class="form-group">
          <label>插件模型</label>
          <div class="select-wrap">
            <select id="gen_plugin_model">${modelOptions(data.plugin_model)}</select>
          </div>
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
          <div class="select-wrap" style="flex:1">
            <select class="task-model" data-task="${task.key}"${!isOverridden ? ' disabled' : ''}>
              <option value="__inherit__">继承通用</option>
              ${modelOptions(taskModel, true)}
            </select>
          </div>
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
  setupOverrideListeners();
}

function modelOptions(selected, skipInherit) {
  return modelChoices.map(m => {
    const val = typeof m === 'object' ? m.value : m;
    const label = typeof m === 'object' ? m.label : m;
    const isSelected = val === selected ? ' selected' : '';
    return `<option value="${val}"${isSelected}>${label}</option>`;
  }).join('');
}

function setupOverrideListeners() {
  document.querySelectorAll('.task-override').forEach(cb => {
    cb.addEventListener('change', () => {
      const task = cb.dataset.task;
      const modelSelect = document.querySelector(`.task-model[data-task="${task}"]`);
      if (cb.checked) {
        modelSelect.disabled = false;
      } else {
        modelSelect.disabled = true;
        modelSelect.value = '__inherit__';
      }
    });
  });
}

async function saveOrchestration(name) {
  const taskModels = {};
  const taskEnabled = {};
  document.querySelectorAll('.task-model').forEach(select => {
    const task = select.dataset.task;
    const enabled = document.querySelector(`.task-enabled[data-task="${task}"]`).checked;
    const isOverridden = document.querySelector(`.task-override[data-task="${task}"]`).checked;
    taskModels[task] = isOverridden ? select.value : '__inherit__';
    taskEnabled[task] = enabled;
  });

  try {
    await post(`/personas/${name}/orchestration`, {
      analysis_model: $('gen_analysis_model').value,
      chat_model: $('gen_chat_model').value,
      memory_model: $('gen_memory_model').value,
      plugin_model: $('gen_plugin_model').value,
      task_models: taskModels,
      task_enabled: taskEnabled,
    });
    flashSuccess($('orchSave'));
    toast('模型编排已保存', 'success');
  } catch (e) {
    toast('保存失败: ' + e.message, 'error');
  }
}
