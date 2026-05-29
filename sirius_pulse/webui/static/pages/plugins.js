import { store } from '../store.js';
import { get, post, put } from '../app.js';
import { toast, flashSuccess, $ } from '../components.js';

let currentModal = null;
let _pluginScheduleData = {};

export async function init(container) {
  container.innerHTML = `
    <div class="card">
      <div class="card-header">
        <div>
          <div class="card-title">插件管理</div>
          <div class="card-subtitle">管理系统插件的启用状态和配置</div>
        </div>
        <button class="btn btn-sm" id="reloadPlugins">刷新</button>
      </div>
      <div id="pluginList" style="padding:16px">
        <div style="color:var(--text-3)">加载中...</div>
      </div>
    </div>
  `;

  await loadPlugins();
  $('reloadPlugins').addEventListener('click', () => loadPlugins());
}

async function loadPlugins() {
  const el = $('pluginList');
  try {
    const res = await get('/plugins');
    const plugins = res.plugins || [];

    if (!plugins.length) {
      el.innerHTML = '<div style="padding:24px;text-align:center;color:var(--text-3)">暂无插件</div>';
      return;
    }

    el.innerHTML = `<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:12px">${plugins.map(p => `
      <div class="card" data-plugin="${p.name}" style="margin:0;cursor:pointer">
        <div style="padding:16px">
          <div style="display:flex;align-items:center;gap:12px;margin-bottom:8px">
            <span class="plugin-toggle tag" data-name="${p.name}" style="font-size:11px;background:${p.enabled ? 'var(--success)' : 'var(--text-3)'};color:#fff;padding:2px 8px;border-radius:4px;flex-shrink:0" onclick="event.stopPropagation()">${p.enabled ? '已启用' : '已禁用'}</span>
            <span class="tag" style="font-size:11px;background:var(--accent);color:#fff;padding:2px 8px;border-radius:4px;flex-shrink:0">${p.version || '—'}</span>
            <span style="font-size:15px;font-weight:600;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${p.display_name || p.name}</span>
          </div>
          ${p.description ? `<div style="font-size:13px;color:var(--text-2);margin-bottom:12px;line-height:1.4">${p.description}</div>` : ''}
          <div style="display:flex;justify-content:space-between;align-items:center">
            <div style="display:flex;gap:12px;font-size:12px;color:var(--text-2)">
              <span>命令: ${(p.commands || []).length}</span>
              <span>参数: ${(p.parameters || []).length}</span>
            </div>
            <button class="btn btn-sm btn-primary plugin-detail-btn" data-name="${p.name}" onclick="event.stopPropagation()">详情</button>
          </div>
        </div>
      </div>
    `).join('')}</div>`;

    el.querySelectorAll('.plugin-toggle').forEach(tag => {
      tag.addEventListener('click', (e) => {
        e.stopPropagation();
        const name = tag.dataset.name;
        const newState = tag.textContent === '已启用' ? false : true;
        tag.textContent = newState ? '已启用' : '已禁用';
        tag.style.background = newState ? 'var(--success)' : 'var(--text-3)';
        togglePlugin(name, newState, tag);
      });
    });

    el.querySelectorAll('.plugin-detail-btn').forEach(btn => {
      btn.addEventListener('click', () => openDetail(btn.dataset.name));
    });
  } catch {
    el.innerHTML = '<div style="color:var(--danger);padding:12px">插件列表加载失败</div>';
  }
}

async function togglePlugin(name, enabled) {
  try {
    const res = await post(`/plugins/${name}/toggle`, { enabled });
    if (res.success) {
      toast(`${name} 已${enabled ? '启用' : '禁用'}`, 'success');
    } else {
      toast(res.error || '操作失败', 'error');
    }
  } catch (e) {
    toast('操作失败: ' + e.message, 'error');
  }
  await loadPlugins();
}

async function openDetail(name) {
  closeModal();
  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay';
  overlay.innerHTML = `
    <div class="modal" style="max-width:720px;max-height:85vh;overflow-y:auto">
      <div class="modal-header">
        <span id="modalTitle" style="font-size:16px;font-weight:600">加载中...</span>
        <button class="btn btn-sm" id="modalClose">✕</button>
      </div>
      <div class="modal-body" id="modalBody">
        <div style="padding:20px;text-align:center;color:var(--text-3)">加载中...</div>
      </div>
      <div class="modal-footer" id="modalFooter"></div>
    </div>
  `;
  document.body.appendChild(overlay);
  currentModal = overlay;

  overlay.addEventListener('click', (e) => {
    if (e.target === overlay) closeModal();
  });
  overlay.querySelector('#modalClose').addEventListener('click', closeModal);

  try {
    const detail = await get(`/plugins/${name}`);
    renderModalContent(detail);
  } catch {
    $('modalBody').innerHTML = '<div style="color:var(--danger);padding:12px">加载失败</div>';
  }
}

function closeModal() {
  if (currentModal) {
    currentModal.remove();
    currentModal = null;
  }
  _pluginScheduleData = {};
}

function renderModalContent(d) {
  $('modalTitle').textContent = d.display_name || d.name;

  const commands = d.commands || [];
  const parameters = d.parameters || [];
  const nlExamples = d.nl_examples || [];
  const permissions = d.permissions || {};
  const settings = d.settings || {};

  $('modalBody').innerHTML = `
    <div class="stat-grid" style="margin-bottom:16px">
      <div class="stat-card">
        <div class="stat-label">版本</div>
        <div class="stat-value" style="font-size:14px">${d.version || '—'}</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">作者</div>
        <div class="stat-value" style="font-size:14px">${d.author || '—'}</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">命令数</div>
        <div class="stat-value">${commands.length}</div>
      </div>
      <div class="stat-card">
        <div class="stat-label">状态</div>
        <div class="stat-value" style="font-size:14px;color:${d.enabled ? 'var(--success)' : 'var(--text-3)'}">${d.enabled ? '已启用' : '已禁用'}</div>
      </div>
    </div>

    ${commands.length ? `
      <div style="margin-bottom:16px">
        <div style="font-size:14px;font-weight:600;margin-bottom:8px">命令列表</div>
        <div style="display:grid;gap:8px">
          ${commands.map(c => `
            <div style="padding:10px 12px;background:var(--surface-2,rgba(255,255,255,0.03));border-radius:6px">
              <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">
                <span style="font-weight:500;font-size:13px">${c.name}</span>
                <span class="tag" style="font-size:11px">${c.pattern_type || 'text'}</span>
              </div>
              ${c.description ? `<div style="font-size:12px;color:var(--text-2);margin-bottom:4px">${c.description}</div>` : ''}
              ${(c.patterns || []).length ? `<div style="display:flex;flex-wrap:wrap;gap:4px;margin-top:4px">${c.patterns.map(pt => `<code style="font-size:11px;padding:2px 6px;background:var(--surface-3,rgba(255,255,255,0.06));border-radius:4px">${pt}</code>`).join('')}</div>` : ''}
            </div>
          `).join('')}
        </div>
      </div>
    ` : ''}

    ${parameters.length ? `
      <div style="margin-bottom:16px">
        <div style="font-size:14px;font-weight:600;margin-bottom:8px">参数列表</div>
        <div style="display:grid;gap:8px">
          ${parameters.map(p => `
            <div style="padding:10px 12px;background:var(--surface-2,rgba(255,255,255,0.03));border-radius:6px;display:flex;justify-content:space-between;align-items:center">
              <div>
                <span style="font-weight:500;font-size:13px">${p.name}</span>
                ${p.description ? `<span style="font-size:12px;color:var(--text-2);margin-left:8px">${p.description}</span>` : ''}
              </div>
              <div style="display:flex;gap:6px;align-items:center">
                <span class="tag" style="font-size:11px">${p.type || 'string'}</span>
                ${p.required ? '<span class="tag tag-accent" style="font-size:11px">必填</span>' : ''}
              </div>
            </div>
          `).join('')}
        </div>
      </div>
    ` : ''}

    ${nlExamples.length ? `
      <div style="margin-bottom:16px">
        <div style="font-size:14px;font-weight:600;margin-bottom:8px">自然语言示例</div>
        <div style="display:grid;gap:6px">
          ${nlExamples.map(ex => `
            <div style="font-size:13px;padding:8px 12px;background:var(--surface-2,rgba(255,255,255,0.03));border-radius:6px;color:var(--text-2)">"${ex}"</div>
          `).join('')}
        </div>
      </div>
    ` : ''}

    <div style="margin-bottom:16px">
      <div style="font-size:14px;font-weight:600;margin-bottom:8px">权限配置</div>
      <form id="permForm" style="display:grid;gap:12px">
        <label style="display:flex;align-items:center;gap:8px;font-size:13px;cursor:pointer">
          <input type="checkbox" name="developer_only" ${permissions.developer_only ? 'checked' : ''}>
          仅开发者可用
        </label>
        <label style="display:flex;align-items:center;gap:8px;font-size:13px;cursor:pointer">
          <input type="checkbox" name="hidden_from_intent" ${permissions.hidden_from_intent ? 'checked' : ''}>
          意图识别中隐藏
        </label>
        <div class="form-group" style="margin:0">
          <label>频率限制 (次/分钟)</label>
          <input type="number" name="rate_limit" value="${permissions.rate_limit_calls_per_minute || 60}" min="1" max="1000">
        </div>
        <div class="form-group" style="margin:0">
          <label>群组黑名单</label>
          <input type="text" name="group_blacklist" placeholder="群号用逗号分隔" value="${(permissions.group_blacklist || []).join(',')}">
        </div>
      </form>
    </div>

    <div id="pluginSettingsSection" style="display:none">
      <div style="font-size:14px;font-weight:600;margin-bottom:8px">自定义配置</div>
      <form id="settingsForm" style="display:grid;gap:12px"></form>
    </div>
  `;

  // 渲染自定义配置表单（异步，因为可能需要获取 active_repos 列表）
  _renderPluginSettings(settings, parameters);

  $('modalFooter').innerHTML = `
    <button class="btn" id="modalCancel">取消</button>
    <button class="btn btn-primary" id="modalSave">保存配置</button>
  `;

  $('modalCancel').addEventListener('click', closeModal);
  $('modalSave').addEventListener('click', () => savePluginConfig(d.name, settings));
}

async function _renderPluginSettings(settings, parameters) {
  const section = document.getElementById('pluginSettingsSection');
  const form = document.getElementById('settingsForm');
  if (!section || !form) return;

  // 检查是否有 active_repos 参数，若有则预取仓库列表
  let repoOptions = null;
  if ((parameters || []).some(p => p.name === 'active_repos')) {
    try {
      const reposRes = await get('/plugins/monitor_repos');
      repoOptions = reposRes.repos || [];
    } catch (e) {
      console.warn('获取 monitor 仓库列表失败', e);
    }
  }

  // 初始化 schedule 数据（自动识别所有 schedule 类型配置）
  _pluginScheduleData = {};
  if (settings) {
    for (const [key, value] of Object.entries(settings)) {
      if (Array.isArray(value) && value.length > 0 &&
          typeof value[0] === 'object' && 'time' in value[0] && 'duration' in value[0]) {
        _pluginScheduleData[key] = value.map(s => ({ ...s }));
      }
    }
  }

  // 若无 settings 但有 parameters 定义，用 default 值填充
  let effectiveSettings = settings;
  if (!Object.keys(settings || {}).length && (parameters || []).length > 0) {
    effectiveSettings = {};
    parameters.forEach(p => {
      if (p.default !== undefined && p.default !== null) {
        effectiveSettings[p.name] = p.default;
      }
    });
  }

  if (!Object.keys(effectiveSettings || {}).length) {
    section.style.display = 'none';
    form.innerHTML = '';
    return;
  }

  section.style.display = 'block';
  form.innerHTML = _buildPluginSettingsForm(effectiveSettings, parameters, repoOptions);

  // 绑定 list 类型按钮事件
  form.querySelectorAll('[data-list-add]').forEach(btn => {
    btn.addEventListener('click', () => {
      const key = btn.dataset.listAdd;
      const container = document.getElementById('pluginList_' + key);
      if (!container) return;
      const idx = container.querySelectorAll('[data-list-key]').length;
      const div = document.createElement('div');
      div.style.cssText = 'display:flex;gap:4px;margin-bottom:4px';
      div.innerHTML = `
        <input type="text" data-list-key="${key}" data-list-index="${idx}"
          style="background:var(--surface-2);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px 8px;font-size:13px;flex:1">
        <button class="btn btn-sm" data-list-remove="${key}" style="padding:2px 8px">✕</button>
      `;
      container.appendChild(div);
      div.querySelector('[data-list-remove]').addEventListener('click', () => div.remove());
    });
  });

  // 绑定已有 list 项的删除按钮
  form.querySelectorAll('[data-list-remove]').forEach(btn => {
    btn.addEventListener('click', () => btn.parentElement.remove());
  });

  // 绑定 schedule 相关按钮
  form.querySelectorAll('[data-schedule-add]').forEach(btn => {
    btn.addEventListener('click', () => {
      const key = btn.dataset.scheduleAdd;
      if (!_pluginScheduleData[key]) _pluginScheduleData[key] = [];
      _pluginScheduleData[key].push({ time: '22:00', duration: 1440 });
      _refreshScheduleList(key);
    });
  });

  form.querySelectorAll('[data-schedule-remove]').forEach(btn => {
    btn.addEventListener('click', () => {
      const key = btn.dataset.scheduleRemoveKey;
      const idx = parseInt(btn.dataset.scheduleRemove, 10);
      if (_pluginScheduleData[key]) {
        _pluginScheduleData[key].splice(idx, 1);
        _refreshScheduleList(key);
      }
    });
  });

  _bindScheduleInputs();
}

function _refreshScheduleList(key) {
  const container = document.getElementById('pluginSchedule_' + key);
  if (!container || !_pluginScheduleData[key]) return;

  container.innerHTML = _pluginScheduleData[key].map((s, i) => `
    <div style="display:flex;gap:8px;align-items:center;margin-bottom:6px">
      <input type="time" value="${s.time || '22:00'}" data-schedule-key="${key}" data-schedule-idx="${i}" data-schedule-field="time"
        style="background:var(--surface-2);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px 8px;font-size:13px">
      <span style="color:var(--text-3);font-size:12px">分析时长</span>
      <input type="number" value="${s.duration || 1440}" min="1" max="10080" data-schedule-key="${key}" data-schedule-idx="${i}" data-schedule-field="duration"
        style="background:var(--surface-2);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px 8px;font-size:13px;width:80px">
      <span style="color:var(--text-3);font-size:12px">分钟</span>
      <button class="btn btn-sm" data-schedule-remove="${i}" data-schedule-remove-key="${key}" style="padding:2px 8px;font-size:11px">✕</button>
    </div>
  `).join('');

  _bindScheduleInputs();

  container.querySelectorAll('[data-schedule-remove]').forEach(btn => {
    btn.addEventListener('click', () => {
      const rmIdx = parseInt(btn.dataset.scheduleRemove, 10);
      if (_pluginScheduleData[key]) {
        _pluginScheduleData[key].splice(rmIdx, 1);
        _refreshScheduleList(key);
      }
    });
  });
}

function _bindScheduleInputs() {
  document.querySelectorAll('[data-schedule-key]').forEach(inp => {
    inp.addEventListener('change', () => {
      const key = inp.dataset.scheduleKey;
      const idx = parseInt(inp.dataset.scheduleIdx, 10);
      const field = inp.dataset.scheduleField;
      if (!_pluginScheduleData[key] || !_pluginScheduleData[key][idx]) return;
      if (field === 'duration') {
        _pluginScheduleData[key][idx][field] = parseInt(inp.value, 10) || 1440;
      } else {
        _pluginScheduleData[key][idx][field] = inp.value;
      }
    });
  });
}

function _formatConfigKey(key) {
  return key.replace(/_/g, ' ').replace(/^\w/, c => c.toUpperCase());
}

function _buildPluginSettingsForm(settings, parameters, repoOptions) {
  const knownParams = new Map();
  (parameters || []).forEach(p => knownParams.set(p.name, p));
  const fields = [];
  const renderedKeys = new Set();

  // 第一步：根据 parameters 定义渲染表单
  for (const param of (parameters || [])) {
    const key = param.name;
    const value = settings?.[key];
    if (renderedKeys.has(key)) continue;
    renderedKeys.add(key);

    const type = param.type || 'str';
    const desc = param.description || '';
    const defaultVal = param.default;

    // active_repos 特殊处理：渲染为仓库复选框
    if (key === 'active_repos' && repoOptions && repoOptions.length > 0) {
      const selectedRepos = Array.isArray(value) ? value : [];
      const selectedSet = new Set(selectedRepos);
      const checkboxes = repoOptions.map(repo => {
        const checked = selectedSet.has(repo) ? ' checked' : '';
        return `
          <label style="display:flex;align-items:center;gap:6px;cursor:pointer;padding:3px 0;font-size:13px">
            <input type="checkbox" data-repo-check="${repo}"${checked}>
            <span>${repo}</span>
          </label>
        `;
      }).join('');
      fields.push(`
        <div class="form-group" style="margin:0">
          <label style="font-weight:500;display:block;margin-bottom:6px">active_repos <span style="color:var(--text-3);font-size:11px">${desc}</span></label>
          <div id="activeReposCheckboxes" style="max-height:200px;overflow-y:auto;background:var(--surface-2);border:1px solid var(--border);border-radius:6px;padding:8px 12px">
            ${checkboxes || '<span style="color:var(--text-3);font-size:12px">无可用仓库</span>'}
          </div>
        </div>
      `);
      continue;
    }

    if (type === 'boolean') {
      fields.push(`
        <div class="form-group" style="margin:0">
          <label style="display:flex;align-items:center;gap:8px;cursor:pointer">
            <input type="checkbox" data-setting-key="${key}" ${value ? 'checked' : ''}>
            <span>${param.name}</span>
          </label>
          ${desc ? `<span style="color:var(--text-3);font-size:11px;margin-left:24px">${desc}</span>` : ''}
        </div>
      `);
    } else if (type === 'int' || type === 'number') {
      fields.push(`
        <div class="form-group" style="margin:0">
          <label>${param.name}</label>
          <input type="number" data-setting-key="${key}" value="${value ?? defaultVal ?? 0}"
            style="background:var(--surface-2);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px 8px;font-size:13px;width:150px">
          ${desc ? `<span style="color:var(--text-3);font-size:11px;margin-left:8px">${desc}</span>` : ''}
        </div>
      `);
    } else if (type === 'string' || type === 'str') {
      fields.push(`
        <div class="form-group" style="margin:0">
          <label>${param.name}</label>
          <input type="text" data-setting-key="${key}" value="${value ?? defaultVal ?? ''}"
            style="background:var(--surface-2);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px 8px;font-size:13px;width:100%">
          ${desc ? `<span style="color:var(--text-3);font-size:11px">${desc}</span>` : ''}
        </div>
      `);
    } else if (type === 'list' || type === 'array') {
      const listVal = Array.isArray(value) ? value : (defaultVal ? [defaultVal] : []);
      fields.push(`
        <div class="form-group" style="margin:0">
          <label>${param.name}</label>
          <div id="pluginList_${key}" style="margin-bottom:8px">
            ${listVal.map((v, i) => `
              <div style="display:flex;gap:4px;margin-bottom:4px">
                <input type="text" value="${v}" data-list-key="${key}" data-list-index="${i}"
                  style="background:var(--surface-2);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px 8px;font-size:13px;flex:1">
                <button class="btn btn-sm" data-list-remove="${key}" style="padding:2px 8px">✕</button>
              </div>
            `).join('')}
          </div>
          <button class="btn btn-sm" data-list-add="${key}" style="padding:4px 12px;font-size:12px">+ 添加</button>
          ${desc ? `<span style="color:var(--text-3);font-size:11px;margin-left:8px">${desc}</span>` : ''}
        </div>
      `);
    }
  }

  // 第二步：处理 settings 中存在但 parameters 中没有定义的复杂类型
  for (const [key, value] of Object.entries(settings || {})) {
    if (renderedKeys.has(key)) continue;
    renderedKeys.add(key);

    // 检测 schedule 类型（数组，每个元素包含 time 和 duration）
    if (Array.isArray(value) && value.length > 0 &&
        typeof value[0] === 'object' && 'time' in value[0] && 'duration' in value[0]) {
      const scheduleId = 'pluginSchedule_' + key;
      fields.push(`
        <div class="form-group" style="margin:0">
          <label style="font-weight:500;display:block;margin-bottom:8px">${_formatConfigKey(key)}</label>
          <div id="${scheduleId}" style="margin-bottom:8px">
            ${value.map((s, i) => `
              <div style="display:flex;gap:8px;align-items:center;margin-bottom:6px">
                <input type="time" value="${s.time || '22:00'}" data-schedule-key="${key}" data-schedule-idx="${i}" data-schedule-field="time"
                  style="background:var(--surface-2);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px 8px;font-size:13px">
                <span style="color:var(--text-3);font-size:12px">分析时长</span>
                <input type="number" value="${s.duration || 1440}" min="1" max="10080" data-schedule-key="${key}" data-schedule-idx="${i}" data-schedule-field="duration"
                  style="background:var(--surface-2);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px 8px;font-size:13px;width:80px">
                <span style="color:var(--text-3);font-size:12px">分钟</span>
                <button class="btn btn-sm" data-schedule-remove="${i}" data-schedule-remove-key="${key}" style="padding:2px 8px;font-size:11px">✕</button>
              </div>
            `).join('')}
          </div>
          <button class="btn btn-sm" data-schedule-add="${key}" style="padding:4px 12px;font-size:12px">+ 添加定时</button>
        </div>
      `);
      continue;
    }

    // 其他未知类型：回退为 JSON 字符串输入
    fields.push(`
      <div class="form-group" style="margin:0">
        <label>${key}</label>
        <input type="text" data-setting-key="${key}" value="${typeof value === 'object' ? JSON.stringify(value) : value}">
      </div>
    `);
  }

  return fields.join('') || '<div style="color:var(--text-3);font-size:13px">暂无可配置项</div>';
}

async function savePluginConfig(name, originalSettings) {
  const saveBtn = $('modalSave');
  saveBtn.disabled = true;
  saveBtn.textContent = '保存中...';

  try {
    const permForm = $('permForm');
    if (permForm) {
      const bl = permForm.group_blacklist.value.trim();
      const permissions = {
        developer_only: permForm.developer_only.checked,
        hidden_from_intent: permForm.hidden_from_intent.checked,
        rate_limit_calls_per_minute: parseInt(permForm.rate_limit.value, 10) || 60,
        group_blacklist: bl ? bl.split(',').map(s => s.trim()).filter(Boolean) : [],
      };
      await put(`/plugins/${name}/config`, permissions);
    }

    // 收集自定义配置
    const newSettings = {};

    // 处理 schedule 配置（支持多个 key）
    for (const [key, schedule] of Object.entries(_pluginScheduleData)) {
      if (Array.isArray(schedule) && schedule.length > 0) {
        newSettings[key] = schedule;
      }
    }

    // 处理 data-setting-key 输入项
    document.querySelectorAll('[data-setting-key]').forEach(input => {
      const key = input.dataset.settingKey;
      if (input.type === 'checkbox') {
        newSettings[key] = input.checked;
      } else if (input.type === 'number') {
        newSettings[key] = parseFloat(input.value) || 0;
      } else {
        newSettings[key] = input.value;
      }
    });

    // 收集 list 类型配置（data-list-key 输入框）
    const listKeys = new Set();
    document.querySelectorAll('[data-list-key]').forEach(inp => {
      listKeys.add(inp.dataset.listKey);
    });
    listKeys.forEach(key => {
      const values = [];
      document.querySelectorAll(`[data-list-key="${key}"]`).forEach(inp => {
        if (inp.value.trim()) values.push(inp.value.trim());
      });
      newSettings[key] = values;
    });

    // 收集 active_repos 复选项
    const repoChecks = document.querySelectorAll('[data-repo-check]');
    if (repoChecks.length > 0) {
      const checkedRepos = [];
      repoChecks.forEach(cb => {
        if (cb.checked) checkedRepos.push(cb.dataset.repoCheck);
      });
      newSettings['active_repos'] = checkedRepos;
    }

    // 保存自定义配置（如果有）
    if (Object.keys(newSettings).length > 0) {
      await post(`/plugins/${name}/settings`, { settings: newSettings });
    }

    flashSuccess(saveBtn);
    toast('配置已保存', 'success');
    setTimeout(closeModal, 1200);
  } catch (e) {
    toast('保存失败: ' + e.message, 'error');
    saveBtn.disabled = false;
    saveBtn.textContent = '保存配置';
  }
}
