import { store } from '../store.js';
import { get, post } from '../app.js';
import { toast, flashSuccess, $ } from '../components.js';

const TASK_GROUPS = [
  {
    id: 'cognition',
    title: '认知分析',
    icon: '◎',
    tasks: [
      { key: 'cognition_analyze', label: '认知分析' },
      { key: 'memory_extract', label: '记忆提取' },
    ],
  },
  {
    id: 'chat',
    title: '对话生成',
    icon: '💬',
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
    icon: '🧬',
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
    icon: '⏣',
    tasks: [
      { key: 'plugin_analyze', label: '插件分析' },
      { key: 'plugin_generate', label: '插件生成' },
      { key: 'plugin_render', label: '插件渲染' },
      { key: 'plugin_raw', label: '插件原生调用' },
    ],
  },
];

const PARAM_FIELDS = [
  { key: 'temperature', label: 'Temperature', type: 'number', step: '0.1', min: '0', max: '2' },
  { key: 'max_tokens', label: 'Max Tokens', type: 'number', step: '1', min: '1', max: '65536' },
  { key: 'timeout', label: 'Timeout (s)', type: 'number', step: '1', min: '1', max: '300' },
  { key: 'fallback_model', label: 'Fallback Model', type: 'text' },
];

let defaults = {};
let taskParams = {};

export async function init(container) {
  const name = store.currentPersona;
  if (!name) {
    container.innerHTML = `
      <div class="card">
        <div class="card-header"><div class="card-title">参数调优</div></div>
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
          <div class="card-title">参数调优</div>
          <div class="card-subtitle">调整 ${name} 各任务的生成参数</div>
        </div>
        <div style="display:flex;gap:8px">
          <button class="btn btn-ghost" id="tpReset">重置全部</button>
          <button class="btn btn-primary" id="tpSave" disabled>保存</button>
        </div>
      </div>
      <div id="tpContent">
        <div style="padding:20px;color:var(--text-3)">加载中...</div>
      </div>
    </div>
  `;

  $('tpSave')?.addEventListener('click', () => saveParams(name));
  $('tpReset')?.addEventListener('click', () => resetParams(name));
  await loadParams(name);
}

async function loadParams(name) {
  try {
    const data = await get(`/personas/${name}/task-params`);
    defaults = data.defaults || {};
    taskParams = data.task_params || {};
    renderParams();
    $('tpSave').disabled = false;
  } catch (e) {
    $('tpContent').innerHTML = `<div style="padding:20px;color:var(--danger)">加载失败: ${e.message}</div>`;
  }
}

function renderParams() {
  const el = $('tpContent');
  let html = '';

  for (const group of TASK_GROUPS) {
    const groupId = `tp-group-${group.id}`;
    html += `
      <div class="tp-group" style="margin-bottom:16px;border:1px solid var(--border);border-radius:10px;overflow:hidden">
        <div class="tp-group-header" data-group="${groupId}" style="display:flex;align-items:center;gap:10px;padding:12px 16px;cursor:pointer;user-select:none;background:var(--bg-secondary);transition:background 0.15s">
          <span class="tp-chevron" style="font-size:11px;transition:transform 0.2s;color:var(--text-3)">▶</span>
          <span style="font-size:15px">${group.icon}</span>
          <span style="font-size:14px;font-weight:600;color:var(--text-1)">${group.title}</span>
          <span style="font-size:12px;color:var(--text-3);margin-left:auto">${group.tasks.length} 个任务</span>
        </div>
        <div id="${groupId}" class="tp-group-body" style="display:none;padding:0">
    `;

    for (const task of group.tasks) {
      html += renderTaskRow(task);
    }

    html += `</div></div>`;
  }

  el.innerHTML = html;
  bindEvents(el);
}

function renderTaskRow(task) {
  const params = taskParams[task.key] || {};
  const defs = defaults[task.key] || {};

  let html = `
    <div class="tp-task-row" style="display:grid;grid-template-columns:160px repeat(4,1fr);gap:0;border-top:1px solid var(--border-light)">
      <div style="padding:10px 16px;display:flex;align-items:center;font-size:13px;color:var(--text-1);font-weight:500;background:var(--bg-primary)">
        ${task.label}
      </div>
  `;

  for (const field of PARAM_FIELDS) {
    const saved = params[field.key];
    const defVal = defs[field.key];
    const hasOverride = saved !== null && saved !== undefined && saved !== '';
    const displayVal = hasOverride ? String(saved) : '';

    let defDisplay;
    if (field.key === 'fallback_model') {
      defDisplay = defVal || '留空表示无';
    } else {
      defDisplay = defVal != null ? String(defVal) : '';
    }

    html += `
      <div class="tp-cell" data-task="${task.key}" data-field="${field.key}" style="padding:8px 10px;border-left:1px solid var(--border-light);background:var(--bg-primary);display:flex;flex-direction:column;gap:3px">
        <div class="tp-label" style="font-size:11px;color:var(--text-3);display:flex;align-items:center;gap:4px">
          ${field.label}
          <span class="tp-indicator" style="display:${hasOverride ? 'inline' : 'none'};color:var(--accent);font-size:10px">●</span>
        </div>
        <div style="display:flex;align-items:center;gap:4px">
          <input
            type="${field.type}"
            class="tp-input"
            data-task="${task.key}"
            data-field="${field.key}"
            value="${displayVal}"
            placeholder="${defDisplay}"
            ${field.step ? `step="${field.step}"` : ''}
            ${field.min != null ? `min="${field.min}"` : ''}
            ${field.max != null ? `max="${field.max}"` : ''}
            style="flex:1;min-width:0;padding:5px 8px;border:1px solid ${hasOverride ? 'var(--accent)' : 'var(--border)'};border-radius:6px;background:var(--bg-secondary);color:var(--text-1);font-size:12px;font-family:var(--font-mono)"
          >
          <button class="tp-clear" data-task="${task.key}" data-field="${field.key}" title="恢复默认" style="border:none;background:none;cursor:pointer;color:var(--text-3);font-size:14px;padding:2px 4px;line-height:1;visibility:${hasOverride ? 'visible' : 'hidden'}">×</button>
        </div>
      </div>
    `;
  }

  html += `</div>`;
  return html;
}

function bindEvents(container) {
  container.addEventListener('click', (e) => {
    const header = e.target.closest('.tp-group-header');
    if (header) {
      toggleGroup(header);
      return;
    }

    const clearBtn = e.target.closest('.tp-clear');
    if (clearBtn) {
      clearOverride(clearBtn);
      return;
    }
  });

  container.addEventListener('input', (e) => {
    const input = e.target.closest('.tp-input');
    if (input) {
      updateCellState(input);
    }
  });
}

function toggleGroup(header) {
  const groupId = header.dataset.group;
  const body = document.getElementById(groupId);
  const chevron = header.querySelector('.tp-chevron');
  if (!body) return;

  const isOpen = body.style.display !== 'none';
  body.style.display = isOpen ? 'none' : 'block';
  if (chevron) chevron.style.transform = isOpen ? '' : 'rotate(90deg)';
  header.style.background = isOpen ? 'var(--bg-secondary)' : 'var(--bg-tertiary)';
}

function updateCellState(input) {
  const cell = input.closest('.tp-cell');
  if (!cell) return;
  const indicator = cell.querySelector('.tp-indicator');
  const clearBtn = cell.querySelector('.tp-clear');
  const hasValue = input.value.trim() !== '';

  input.style.borderColor = hasValue ? 'var(--accent)' : 'var(--border)';
  if (indicator) indicator.style.display = hasValue ? 'inline' : 'none';
  if (clearBtn) clearBtn.style.visibility = hasValue ? 'visible' : 'hidden';
}

function clearOverride(btn) {
  const task = btn.dataset.task;
  const field = btn.dataset.field;
  const cell = btn.closest('.tp-cell');
  if (!cell) return;

  const input = cell.querySelector('.tp-input');
  if (input) {
    input.value = '';
    input.style.borderColor = 'var(--border)';
  }
  const indicator = cell.querySelector('.tp-indicator');
  if (indicator) indicator.style.display = 'none';
  btn.style.visibility = 'hidden';
}

async function saveParams(name) {
  const taskTemperatures = {};
  const taskMaxTokens = {};
  const taskTimeout = {};
  const taskFallbackModel = {};

  document.querySelectorAll('.tp-input').forEach(input => {
    const task = input.dataset.task;
    const field = input.dataset.field;
    const raw = input.value.trim();

    if (raw === '') return;

    if (field === 'temperature') {
      const v = parseFloat(raw);
      if (!isNaN(v)) taskTemperatures[task] = v;
    } else if (field === 'max_tokens') {
      const v = parseInt(raw, 10);
      if (!isNaN(v) && v > 0) taskMaxTokens[task] = v;
    } else if (field === 'timeout') {
      const v = parseFloat(raw);
      if (!isNaN(v) && v > 0) taskTimeout[task] = v;
    } else if (field === 'fallback_model') {
      taskFallbackModel[task] = raw;
    }
  });

  try {
    await post(`/personas/${name}/task-params`, {
      task_temperatures: taskTemperatures,
      task_max_tokens: taskMaxTokens,
      task_timeout: taskTimeout,
      task_fallback_model: taskFallbackModel,
    });
    flashSuccess($('tpSave'));
    toast('参数已保存', 'success');
  } catch (e) {
    toast('保存失败: ' + e.message, 'error');
  }
}

async function resetParams(name) {
  if (!confirm('确定要重置所有任务参数为默认值吗？')) return;

  try {
    await post(`/personas/${name}/task-params`, {
      task_temperatures: {},
      task_max_tokens: {},
      task_timeout: {},
      task_fallback_model: {},
    });
    toast('已重置为默认值', 'success');
    await loadParams(name);
  } catch (e) {
    toast('重置失败: ' + e.message, 'error');
  }
}
