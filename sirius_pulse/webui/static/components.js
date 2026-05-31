export function toast(msg, type = 'success') {
  const container = document.getElementById('toastContainer');
  const el = document.createElement('div');
  el.className = `toast ${type}`;
  el.textContent = msg;
  container.appendChild(el);
  setTimeout(() => { el.style.opacity = '0'; setTimeout(() => el.remove(), 300); }, 3000);
}

export function animateNumber(el, target, duration = 600) {
  if (!el) return;
  const start = parseInt(el.textContent.replace(/,/g, '') || '0', 10) || 0;
  if (start === target) return;
  const startTime = performance.now();
  function tick(now) {
    const progress = Math.min((now - startTime) / duration, 1);
    const eased = 1 - Math.pow(1 - progress, 3);
    el.textContent = Math.round(start + (target - start) * eased).toLocaleString();
    if (progress < 1) requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
}

export function flashSuccess(btn) {
  if (!btn) return;
  const prev = btn.textContent;
  btn.classList.add('btn-success-flash');
  btn.textContent = '✓ ' + prev;
  btn.disabled = true;
  setTimeout(() => { btn.classList.remove('btn-success-flash'); btn.textContent = prev; btn.disabled = false; }, 1200);
}

export function applyStagger(container, childSelector) {
  if (!container) return;
  container.classList.add('animate-stagger');
  const children = childSelector ? container.querySelectorAll(childSelector) : container.children;
  Array.from(children).forEach((child, i) => child.style.setProperty('--i', String(i)));
}

export function showLoginOverlay() {
  const overlay = document.getElementById('loginOverlay');
  overlay.style.display = 'flex';
  overlay.innerHTML = `
    <div class="login-card">
      <div style="font-size:28px;margin-bottom:8px">✦</div>
      <h2 style="font-family:var(--font-display);font-size:22px;margin-bottom:4px;color:var(--text-1)">Sirius Pulse</h2>
      <p style="font-size:13px;color:var(--text-2);margin-bottom:24px">请输入管理员密码以访问控制台</p>
      <div class="form-group">
        <label>密码</label>
        <input id="loginPassword" type="password" placeholder="输入密码" autofocus>
      </div>
      <div id="loginError" style="color:var(--danger);font-size:12px;margin-bottom:12px;display:none"></div>
      <button id="loginBtn" class="btn btn-primary" style="width:100%">登录</button>
    </div>
  `;
  const pwInput = document.getElementById('loginPassword');
  const loginBtn = document.getElementById('loginBtn');
  
  async function doLogin() {
    const password = pwInput.value;
    const errEl = document.getElementById('loginError');
    if (!password) { errEl.textContent = '请输入密码'; errEl.style.display = ''; return; }
    try {
      const { post, setToken } = await import('./api.js');
      const data = await post('/auth/login', { username: 'admin', password });
      if (data.success && data.token) {
        setToken(data.token);
        overlay.style.display = 'none';
        toast('登录成功');
        window.dispatchEvent(new CustomEvent('auth:login'));
      } else {
        errEl.textContent = data.error || '登录失败';
        errEl.style.display = '';
      }
    } catch (e) {
      errEl.textContent = '网络错误';
      errEl.style.display = '';
    }
  }
  
  loginBtn.onclick = doLogin;
  pwInput.onkeydown = (e) => { if (e.key === 'Enter') doLogin(); };
  setTimeout(() => pwInput.focus(), 100);
}

export function hideLoginOverlay() {
  document.getElementById('loginOverlay').style.display = 'none';
}

export function formatHeartbeat(ts) {
  if (!ts) return '—';
  const diff = (Date.now() - new Date(ts)) / 1000;
  if (diff < 5) return '刚刚';
  if (diff < 60) return `${Math.floor(diff)}秒前`;
  if (diff < 3600) return `${Math.floor(diff / 60)}分钟前`;
  return new Date(ts).toLocaleString('zh-CN');
}

export function statCard(label, value, detail = '', icon = '') {
  return `
    <div class="stat-card">
      <div class="stat-label">${icon ? `<span>${icon}</span>` : ''}${label}</div>
      <div class="stat-value">${value}</div>
      ${detail ? `<div class="stat-detail">${detail}</div>` : ''}
    </div>
  `;
}

export const $ = (id) => document.getElementById(id);
export const $$ = (sel, root = document) => root.querySelectorAll(sel);

/**
 * 动态配置表单组件
 * 支持的参数类型: str/string, int/number, boolean, list/array, model, password, schedule
 * 支持分组: 参数可通过 group 属性分组显示
 */
export class DynamicConfigForm {
  /**
   * @param {Object} options
   * @param {string} options.containerId - 表单容器元素 ID
   * @param {Array} options.parameters - 参数定义列表
   * @param {Object} options.settings - 当前配置值
   * @param {Array} [options.modelChoices] - 模型选项列表 [{label, value}]
   * @param {Array} [options.repoOptions] - 仓库选项列表
   * @param {Function} [options.get] - GET 请求函数
   */
  constructor({ containerId, parameters, settings, modelChoices, repoOptions, get }) {
    this.containerId = containerId;
    this.parameters = parameters || [];
    this.settings = settings || {};
    this.modelChoices = modelChoices || null;
    this.repoOptions = repoOptions || null;
    this.get = get;
    this._scheduleData = {};
    this._initScheduleData();
  }

  _initScheduleData() {
    for (const [key, value] of Object.entries(this.settings)) {
      if (Array.isArray(value) && value.length > 0 &&
          typeof value[0] === 'object' && 'time' in value[0] && 'duration' in value[0]) {
        this._scheduleData[key] = value.map(s => ({ ...s }));
      }
    }
  }

  _resolveCompositeValue(bareName) {
    if (!bareName || !this.modelChoices) return bareName || '';
    const exact = this.modelChoices.find(o => o.value === bareName);
    if (exact) return exact.value;
    const suffix = this.modelChoices.find(o => o.value.endsWith('/' + bareName));
    return suffix ? suffix.value : bareName;
  }

  _stripProviderPrefix(value) {
    if (!value) return '';
    const idx = value.indexOf('/');
    return idx >= 0 ? value.substring(idx + 1) : value;
  }

  /**
   * 异步初始化（获取远程数据）
   */
  async init() {
    if (!this.modelChoices && this.parameters.some(p => p.type === 'model') && this.get) {
      try {
        const res = await this.get('/models');
        this.modelChoices = res.model_choices || [];
      } catch (e) {
        console.warn('获取可用模型列表失败', e);
      }
    }
    if (!this.repoOptions && this.parameters.some(p => p.name === 'active_repos') && this.get) {
      try {
        const res = await this.get('/plugins/monitor_repos');
        this.repoOptions = res.repos || [];
      } catch (e) {
        console.warn('获取仓库列表失败', e);
      }
    }
  }

  /**
   * 渲染表单到容器
   */
  render() {
    const container = document.getElementById(this.containerId);
    if (!container) return;

    const effectiveSettings = this._getEffectiveSettings();
    if (!Object.keys(effectiveSettings).length && !this.parameters.length) {
      container.style.display = 'none';
      container.innerHTML = '';
      return;
    }

    container.style.display = 'block';
    container.innerHTML = this._buildForm(effectiveSettings);
    this._bindEvents();
  }

  _getEffectiveSettings() {
    // 合并默认值和用户设置，用户设置优先
    const defaults = {};
    this.parameters.forEach(p => {
      if (p.default !== undefined && p.default !== null) {
        defaults[p.name] = p.default;
      }
    });
    return { ...defaults, ...this.settings };
  }

  _buildForm(settings) {
    const renderedKeys = new Set();
    // 按 group 分组
    const groups = new Map(); // group -> fields[]
    const ungrouped = [];

    for (const param of this.parameters) {
      const key = param.name;
      const value = settings[key];
      if (renderedKeys.has(key)) continue;
      renderedKeys.add(key);

      const type = param.type || 'str';
      const desc = param.description || '';
      const defaultVal = param.default;
      const required = param.required || false;
      const group = param.group || '';

      let fieldHtml = '';
      if (key === 'active_repos' && this.repoOptions?.length) {
        fieldHtml = this._renderActiveRepos(key, value, desc);
      } else if (type === 'model') {
        // model 类型：有选项时渲染下拉框，否则回退到文本输入
        const modelValue = value || defaultVal || '';
        if (this.modelChoices?.length) {
          fieldHtml = this._renderModelSelect(key, modelValue, desc, required);
        } else {
          fieldHtml = this._renderText(key, param.name, modelValue, defaultVal, desc, required);
        }
      } else if (type === 'password' || type === 'secret') {
        fieldHtml = this._renderPassword(key, param.name, value, defaultVal, desc, required);
      } else if (type === 'boolean') {
        fieldHtml = this._renderCheckbox(key, param.name, value, desc);
      } else if (type === 'int' || type === 'number') {
        fieldHtml = this._renderNumber(key, param.name, value, defaultVal, desc, required);
      } else if (type === 'string' || type === 'str') {
        fieldHtml = this._renderText(key, param.name, value, defaultVal, desc, required);
      } else if (type === 'list' || type === 'array') {
        fieldHtml = this._renderList(key, param.name, value, defaultVal, desc);
      } else if (type === 'object_array' && param.fields) {
        fieldHtml = this._renderObjectArray(key, param.name, value, defaultVal, desc, param.fields);
      } else if (type === 'checkbox_group' && param.choices) {
        fieldHtml = this._renderCheckboxGroup(key, param.name, value, defaultVal, desc, param.choices);
      }

      if (fieldHtml) {
        if (group) {
          if (!groups.has(group)) groups.set(group, []);
          groups.get(group).push(fieldHtml);
        } else {
          ungrouped.push(fieldHtml);
        }
      }
    }

    // 处理 settings 中未在 parameters 定义的字段
    for (const [key, value] of Object.entries(settings)) {
      if (renderedKeys.has(key)) continue;
      renderedKeys.add(key);
      let fieldHtml = '';
      if (Array.isArray(value) && value.length > 0 &&
          typeof value[0] === 'object' && 'time' in value[0] && 'duration' in value[0]) {
        fieldHtml = this._renderSchedule(key, value);
      } else {
        fieldHtml = this._renderJson(key, value);
      }
      ungrouped.push(fieldHtml);
    }

    // 渲染分组
    const sections = [];
    // 无分组的字段放在前面
    if (ungrouped.length) {
      sections.push(`<div class="config-fields">${ungrouped.join('')}</div>`);
    }
    // 各分组
    for (const [groupName, fields] of groups) {
      sections.push(`
        <div class="config-group">
          <div class="config-group-header" data-group="${groupName}">
            <span class="config-group-title">${groupName}</span>
            <span class="config-group-toggle">▼</span>
          </div>
          <div class="config-group-body">
            <div class="config-fields">${fields.join('')}</div>
          </div>
        </div>
      `);
    }

    return sections.join('') || '<div style="color:var(--text-3);font-size:13px;text-align:center;padding:20px">暂无可配置项</div>';
  }

  _renderActiveRepos(key, value, desc) {
    const selected = new Set(Array.isArray(value) ? value : []);
    const checkboxes = this.repoOptions.map(repo =>
      `<label style="display:flex;align-items:center;gap:8px;cursor:pointer;padding:6px 8px;border-radius:4px;transition:background 0.2s;font-size:13px">
        <input type="checkbox" data-repo-check="${repo}"${selected.has(repo) ? ' checked' : ''}
          style="width:16px;height:16px;accent-color:var(--accent)">
        <span>${repo}</span>
      </label>`
    ).join('');
    return `
      <div class="config-field">
        <div class="config-field-header">
          <label class="config-field-label">${key}</label>
          ${desc ? `<span class="config-field-desc">${desc}</span>` : ''}
        </div>
        <div style="max-height:200px;overflow-y:auto;background:var(--surface-2);border:1px solid var(--border);border-radius:8px;padding:8px">
          ${checkboxes || '<span style="color:var(--text-3);font-size:12px;padding:8px;display:block;text-align:center">无可用仓库</span>'}
        </div>
      </div>
    `;
  }

  _renderModelSelect(key, value, desc, required) {
    // value 可能是裸模型名，需要匹配复合格式 provider_type/model_name
    const resolvedValue = this._resolveCompositeValue(value);
    // 如果当前值不在选项列表中，添加一个额外的选项
    const valueInChoices = this.modelChoices.some(m => m.value === resolvedValue);
    const options = [...this.modelChoices];
    if (resolvedValue && !valueInChoices) {
      options.unshift({ value: resolvedValue, label: `${resolvedValue} (当前配置)`, tags: [] });
    }
    
    return `
      <div class="config-field">
        <div class="config-field-header">
          <label class="config-field-label">${key}${required ? '<span class="config-required">*</span>' : ''}</label>
          ${desc ? `<span class="config-field-desc">${desc}</span>` : ''}
        </div>
        <div data-model-select="${key}" data-model-value="${resolvedValue || ''}"></div>
      </div>
    `;
  }

  _renderPassword(key, label, value, defaultVal, desc, required) {
    const id = `pwd_${key}`;
    return `
      <div class="config-field">
        <div class="config-field-header">
          <label class="config-field-label">${label}${required ? '<span class="config-required">*</span>' : ''}</label>
          ${desc ? `<span class="config-field-desc">${desc}</span>` : ''}
        </div>
        <div style="position:relative">
          <input type="password" id="${id}" data-setting-key="${key}" value="${value ?? defaultVal ?? ''}" class="config-input" style="padding-right:36px">
          <button type="button" class="pwd-toggle" onclick="const inp=document.getElementById('${id}');const btn=this;inp.type=inp.type==='password'?'text':'password';btn.textContent=inp.type==='password'?'🙈':'👁'"
            style="position:absolute;right:8px;top:50%;transform:translateY(-50%);background:none;border:none;cursor:pointer;font-size:16px;padding:4px;opacity:0.6">
            🙈
          </button>
        </div>
      </div>
    `;
  }

  _renderCheckbox(key, label, value, desc) {
    return `
      <div class="config-field">
        <label style="display:flex;align-items:center;gap:10px;cursor:pointer">
          <input type="checkbox" data-setting-key="${key}" ${value ? 'checked' : ''}
            style="width:18px;height:18px;accent-color:var(--accent)">
          <div>
            <span style="font-weight:500">${label}</span>
            ${desc ? `<div style="color:var(--text-3);font-size:12px;margin-top:2px">${desc}</div>` : ''}
          </div>
        </label>
      </div>
    `;
  }

  _renderNumber(key, label, value, defaultVal, desc, required) {
    return `
      <div class="config-field">
        <div class="config-field-header">
          <label class="config-field-label">${label}${required ? '<span class="config-required">*</span>' : ''}</label>
          ${desc ? `<span class="config-field-desc">${desc}</span>` : ''}
        </div>
        <div class="number-input-group">
          <button type="button" class="number-spin-btn" data-spin-target="${key}" data-spin-dir="-1">−</button>
          <input type="number" data-setting-key="${key}" value="${value ?? defaultVal ?? 0}" class="config-input">
          <button type="button" class="number-spin-btn" data-spin-target="${key}" data-spin-dir="1">+</button>
        </div>
      </div>
    `;
  }

  _renderText(key, label, value, defaultVal, desc, required) {
    return `
      <div class="config-field">
        <div class="config-field-header">
          <label class="config-field-label">${label}${required ? '<span class="config-required">*</span>' : ''}</label>
          ${desc ? `<span class="config-field-desc">${desc}</span>` : ''}
        </div>
        <input type="text" data-setting-key="${key}" value="${value ?? defaultVal ?? ''}" class="config-input">
      </div>
    `;
  }

  _renderList(key, label, value, defaultVal, desc) {
    const listVal = Array.isArray(value) ? value : (defaultVal ? [defaultVal] : []);
    return `
      <div class="config-field">
        <div class="config-field-header">
          <label class="config-field-label">${label}</label>
          ${desc ? `<span class="config-field-desc">${desc}</span>` : ''}
        </div>
        <div id="dynamicList_${key}" style="display:flex;flex-direction:column;gap:6px;margin-bottom:8px">
          ${listVal.map((v, i) => `
            <div style="display:flex;gap:6px;align-items:center">
              <input type="text" value="${v}" data-list-key="${key}" data-list-index="${i}" class="config-input" style="flex:1">
              <button class="btn btn-sm btn-ghost" data-list-remove="${key}" style="padding:6px 8px;color:var(--danger)">✕</button>
            </div>
          `).join('')}
        </div>
        <button class="btn btn-sm btn-ghost" data-list-add="${key}" style="padding:4px 12px;font-size:12px;color:var(--accent)">+ 添加</button>
      </div>
    `;
  }

  _renderSchedule(key, value) {
    const scheduleId = 'dynamicSchedule_' + key;
    return `
      <div class="config-field">
        <div class="config-field-header">
          <label class="config-field-label">${this._formatKey(key)}</label>
        </div>
        <div id="${scheduleId}" style="display:flex;flex-direction:column;gap:8px;margin-bottom:8px">
          ${value.map((s, i) => `
            <div style="display:flex;gap:8px;align-items:center;padding:8px;background:var(--surface-2);border-radius:6px">
              <input type="time" value="${s.time || '22:00'}" data-schedule-key="${key}" data-schedule-idx="${i}" data-schedule-field="time"
                class="config-input" style="width:auto">
              <span style="color:var(--text-3);font-size:12px;white-space:nowrap">时长</span>
              <div class="number-input-group" style="max-width:120px">
                <button type="button" class="number-spin-btn" data-spin-target="schedule_duration_${key}_${i}" data-spin-dir="-1">−</button>
                <input type="number" id="schedule_duration_${key}_${i}" value="${s.duration || 1440}" min="1" max="10080" data-schedule-key="${key}" data-schedule-idx="${i}" data-schedule-field="duration"
                  class="config-input">
                <button type="button" class="number-spin-btn" data-spin-target="schedule_duration_${key}_${i}" data-spin-dir="1">+</button>
              </div>
              <span style="color:var(--text-3);font-size:12px">分钟</span>
              <button class="btn btn-sm btn-ghost" data-schedule-remove="${i}" data-schedule-remove-key="${key}" style="margin-left:auto;padding:4px 8px;color:var(--danger)">✕</button>
            </div>
          `).join('')}
        </div>
        <button class="btn btn-sm btn-ghost" data-schedule-add="${key}" style="padding:4px 12px;font-size:12px;color:var(--accent)">+ 添加定时</button>
      </div>
    `;
  }

  _renderObjectArray(key, label, value, defaultVal, desc, fields) {
    const items = Array.isArray(value) ? value : (Array.isArray(defaultVal) ? defaultVal : []);
    const fieldDefs = fields || [];
    
    const renderFieldInput = (field, val, idx) => {
      const fieldType = field.type || 'str';
      const fieldVal = val ?? field.default ?? '';
      const listId = `objList_${key}_${idx}_${field.name}`;
      
      if (fieldType === 'checkbox_group' && field.choices) {
        const selected = new Set(Array.isArray(fieldVal) ? fieldVal : []);
        return `<div style="display:grid;grid-template-columns:repeat(3,1fr);gap:6px;flex:1">
          ${field.choices.map(c => `
            <label style="display:flex;align-items:center;gap:4px;cursor:pointer;font-size:12px;background:var(--surface-3);border:1px solid var(--border);border-radius:4px;padding:3px 8px;min-width:0">
              <input type="checkbox" data-obj-array-key="${key}" data-obj-idx="${idx}" data-obj-field="${field.name}" data-obj-checkbox="${c}"${selected.has(c) ? ' checked' : ''}
                style="flex-shrink:0">
              <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${c}</span>
            </label>
          `).join('')}
        </div>`;
      } else if (fieldType === 'list' || fieldType === 'array') {
        const listItems = Array.isArray(fieldVal) ? fieldVal : [];
        return `<div style="flex:1">
          <div id="${listId}" style="display:flex;flex-direction:column;gap:4px;margin-bottom:4px">
            ${listItems.map((v, li) => `
              <div style="display:flex;gap:4px;align-items:center">
                <input type="text" value="${v}" data-obj-array-key="${key}" data-obj-idx="${idx}" data-obj-field="${field.name}" data-obj-list-idx="${li}" class="config-input" style="flex:1;font-size:12px;padding:4px 6px">
                <button class="btn btn-sm btn-ghost" data-obj-list-remove="${key}" data-obj-idx="${idx}" data-obj-field="${field.name}" data-obj-list-idx="${li}" style="padding:2px 6px;color:var(--danger);font-size:11px">✕</button>
              </div>
            `).join('')}
          </div>
          <button class="btn btn-sm btn-ghost" data-obj-list-add="${key}" data-obj-idx="${idx}" data-obj-field="${field.name}" style="padding:2px 8px;font-size:11px;color:var(--accent)">+ 添加</button>
        </div>`;
      } else if (fieldType === 'password' || fieldType === 'secret') {
        return `<input type="password" data-obj-array-key="${key}" data-obj-idx="${idx}" data-obj-field="${field.name}" value="${fieldVal}" class="config-input" style="flex:1">`;
      } else if (fieldType === 'int' || fieldType === 'number') {
        return `<input type="number" data-obj-array-key="${key}" data-obj-idx="${idx}" data-obj-field="${field.name}" value="${fieldVal}" class="config-input" style="flex:1">`;
      } else {
        return `<input type="text" data-obj-array-key="${key}" data-obj-idx="${idx}" data-obj-field="${field.name}" value="${fieldVal}" placeholder="${field.description || ''}" class="config-input" style="flex:1">`;
      }
    };
    
    return `
      <div class="config-field">
        <div class="config-field-header">
          <label class="config-field-label">${label}</label>
          ${desc ? `<span class="config-field-desc">${desc}</span>` : ''}
        </div>
        <div id="objectArray_${key}" style="display:flex;flex-direction:column;gap:8px;margin-bottom:8px">
          ${items.map((item, i) => `
            <div style="border:1px solid var(--border);border-radius:8px;padding:12px;background:var(--surface-2)" data-obj-array-item="${key}" data-obj-idx="${i}">
              <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
                <span style="font-size:12px;color:var(--text-3)">#${i + 1}</span>
                <button class="btn btn-sm btn-ghost" data-obj-array-remove="${key}" data-obj-idx="${i}" style="padding:2px 8px;color:var(--danger)">✕</button>
              </div>
              <div style="display:grid;gap:8px">
                ${fieldDefs.map(f => `
                  <div style="display:flex;align-items:flex-start;gap:8px">
                    <label style="font-size:12px;color:var(--text-3);min-width:80px;padding-top:6px;flex-shrink:0">${f.name}</label>
                    ${renderFieldInput(f, item[f.name], i)}
                  </div>
                `).join('')}
              </div>
            </div>
          `).join('')}
        </div>
        <button class="btn btn-sm btn-ghost" data-obj-array-add="${key}" style="padding:4px 12px;font-size:12px;color:var(--accent)">+ 添加</button>
      </div>
    `;
  }

  _renderCheckboxGroup(key, label, value, defaultVal, desc, choices) {
    const selected = new Set(Array.isArray(value) ? value : (Array.isArray(defaultVal) ? defaultVal : []));
    return `
      <div class="config-field">
        <div class="config-field-header">
          <label class="config-field-label">${label}</label>
          ${desc ? `<span class="config-field-desc">${desc}</span>` : ''}
        </div>
        <div style="display:grid;grid-template-columns:repeat(3,1fr);gap:8px">
          ${choices.map(c => `
            <label style="display:flex;align-items:center;gap:6px;cursor:pointer;font-size:13px;background:var(--surface-2);border:1px solid var(--border);border-radius:6px;padding:6px 12px;min-width:0">
              <input type="checkbox" data-checkbox-group="${key}" data-checkbox-value="${c}"${selected.has(c) ? ' checked' : ''}
                style="width:16px;height:16px;accent-color:var(--accent);flex-shrink:0">
              <span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${c}</span>
            </label>
          `).join('')}
        </div>
      </div>
    `;
  }

  _renderJson(key, value) {
    return `
      <div class="config-field">
        <div class="config-field-header">
          <label class="config-field-label">${key}</label>
        </div>
        <input type="text" data-setting-key="${key}" value="${typeof value === 'object' ? JSON.stringify(value) : value}" class="config-input">
      </div>
    `;
  }

  _formatKey(key) {
    return key.replace(/_/g, ' ').replace(/^\w/, c => c.toUpperCase());
  }

  _bindEvents() {
    const container = document.getElementById(this.containerId);
    if (!container) return;

    // 分组折叠
    container.querySelectorAll('.config-group-header').forEach(header => {
      header.addEventListener('click', () => {
        const group = header.closest('.config-group');
        const body = group.querySelector('.config-group-body');
        const toggle = header.querySelector('.config-group-toggle');
        const isCollapsed = body.style.display === 'none';
        body.style.display = isCollapsed ? '' : 'none';
        toggle.textContent = isCollapsed ? '▼' : '▶';
        toggle.style.transform = isCollapsed ? '' : 'rotate(-90deg)';
      });
    });

    container.querySelectorAll('[data-list-add]').forEach(btn => {
      btn.addEventListener('click', () => {
        const key = btn.dataset.listAdd;
        const listContainer = document.getElementById('dynamicList_' + key);
        if (!listContainer) return;
        const idx = listContainer.querySelectorAll('[data-list-key]').length;
        const div = document.createElement('div');
        div.style.cssText = 'display:flex;gap:4px;margin-bottom:4px';
        div.innerHTML = `
          <input type="text" data-list-key="${key}" data-list-index="${idx}"
            style="background:var(--surface-2);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px 8px;font-size:13px;flex:1">
          <button class="btn btn-sm" data-list-remove="${key}" style="padding:2px 8px">✕</button>
        `;
        listContainer.appendChild(div);
        div.querySelector('[data-list-remove]').addEventListener('click', () => div.remove());
      });
    });

    container.querySelectorAll('[data-list-remove]').forEach(btn => {
      btn.addEventListener('click', () => btn.parentElement.remove());
    });

    container.querySelectorAll('[data-schedule-add]').forEach(btn => {
      btn.addEventListener('click', () => {
        const key = btn.dataset.scheduleAdd;
        if (!this._scheduleData[key]) this._scheduleData[key] = [];
        this._scheduleData[key].push({ time: '22:00', duration: 1440 });
        this.render();
      });
    });

    container.querySelectorAll('[data-schedule-remove]').forEach(btn => {
      btn.addEventListener('click', () => {
        const key = btn.dataset.scheduleRemoveKey;
        const idx = parseInt(btn.dataset.scheduleRemove, 10);
        if (this._scheduleData[key]) {
          this._scheduleData[key].splice(idx, 1);
          this.render();
        }
      });
    });

    container.querySelectorAll('[data-schedule-field]').forEach(inp => {
      inp.addEventListener('change', () => {
        const key = inp.dataset.scheduleKey;
        const idx = parseInt(inp.dataset.scheduleIdx, 10);
        const field = inp.dataset.scheduleField;
        if (this._scheduleData[key]?.[idx]) {
          this._scheduleData[key][idx][field] = field === 'duration' ? parseInt(inp.value, 10) : inp.value;
        }
      });
    });

    // Object Array 添加按钮
    container.querySelectorAll('[data-obj-array-add]').forEach(btn => {
      btn.addEventListener('click', () => {
        const key = btn.dataset.objArrayAdd;
        const param = this.parameters.find(p => p.name === key);
        if (!param || !param.fields) return;
        
        const newItem = {};
        param.fields.forEach(f => {
          if (f.type === 'checkbox_group') {
            newItem[f.name] = [];
          } else if (f.type === 'list' || f.type === 'array') {
            newItem[f.name] = [];
          } else if (f.type === 'boolean') {
            newItem[f.name] = false;
          } else {
            newItem[f.name] = f.default || '';
          }
        });
        
        if (!this.settings[key]) this.settings[key] = [];
        this.settings[key].push(newItem);
        this.render();
      });
    });

    // Object Array 删除按钮
    container.querySelectorAll('[data-obj-array-remove]').forEach(btn => {
      btn.addEventListener('click', () => {
        const key = btn.dataset.objArrayRemove;
        const idx = parseInt(btn.dataset.objIdx, 10);
        if (this.settings[key]) {
          this.settings[key].splice(idx, 1);
          this.render();
        }
      });
    });

    // Object Array 字段输入
    container.querySelectorAll('[data-obj-array-key]').forEach(inp => {
      const eventType = inp.type === 'checkbox' ? 'change' : 'input';
      inp.addEventListener(eventType, () => {
        const key = inp.dataset.objArrayKey;
        const idx = parseInt(inp.dataset.objIdx, 10);
        const field = inp.dataset.objField;
        
        if (!this.settings[key]?.[idx]) return;
        
        if (inp.dataset.objCheckbox !== undefined) {
          // checkbox_group 字段内的单个复选框
          const checkboxVal = inp.dataset.objCheckbox;
          let arr = this.settings[key][idx][field] || [];
          if (!Array.isArray(arr)) arr = [];
          if (inp.checked) {
            if (!arr.includes(checkboxVal)) arr.push(checkboxVal);
          } else {
            arr = arr.filter(v => v !== checkboxVal);
          }
          this.settings[key][idx][field] = arr;
        } else if (inp.dataset.objListIdx !== undefined) {
          // list 类型内的单个输入项
          const listIdx = parseInt(inp.dataset.objListIdx, 10);
          const arr = this.settings[key][idx][field] || [];
          if (!Array.isArray(arr)) return;
          arr[listIdx] = inp.value;
          this.settings[key][idx][field] = arr;
        } else if (inp.type === 'checkbox') {
          this.settings[key][idx][field] = inp.checked;
        } else if (inp.type === 'number') {
          this.settings[key][idx][field] = parseFloat(inp.value) || 0;
        } else {
          this.settings[key][idx][field] = inp.value;
        }
      });
    });

    // Object Array 内 List 添加按钮
    container.querySelectorAll('[data-obj-list-add]').forEach(btn => {
      btn.addEventListener('click', () => {
        const key = btn.dataset.objListAdd;
        const idx = parseInt(btn.dataset.objIdx, 10);
        const field = btn.dataset.objField;
        
        if (!this.settings[key]?.[idx]) return;
        const arr = this.settings[key][idx][field] || [];
        if (!Array.isArray(arr)) {
          this.settings[key][idx][field] = [''];
        } else {
          arr.push('');
        }
        this.render();
      });
    });

    // Object Array 内 List 删除按钮
    container.querySelectorAll('[data-obj-list-remove]').forEach(btn => {
      btn.addEventListener('click', () => {
        const key = btn.dataset.objListRemove;
        const idx = parseInt(btn.dataset.objIdx, 10);
        const field = btn.dataset.objField;
        const listIdx = parseInt(btn.dataset.objListIdx, 10);
        
        if (!this.settings[key]?.[idx]) return;
        const arr = this.settings[key][idx][field];
        if (Array.isArray(arr)) {
          arr.splice(listIdx, 1);
          this.render();
        }
      });
    });

    // Checkbox Group
    container.querySelectorAll('[data-checkbox-group]').forEach(cb => {
      cb.addEventListener('change', () => {
        const key = cb.dataset.checkboxGroup;
        const val = cb.dataset.checkboxValue;
        if (!this.settings[key]) this.settings[key] = [];
        if (!Array.isArray(this.settings[key])) this.settings[key] = [];
        
        if (cb.checked) {
          if (!this.settings[key].includes(val)) this.settings[key].push(val);
        } else {
          this.settings[key] = this.settings[key].filter(v => v !== val);
        }
      });
    });

    // 数字调节按钮
    container.querySelectorAll('[data-spin-target]').forEach(btn => {
      btn.addEventListener('click', () => {
        const target = container.querySelector(`input[data-setting-key="${btn.dataset.spinTarget}"]`);
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

    // 初始化 ModelSelect 组件
    if (this._modelSelects) {
      this._modelSelects.forEach(ms => ms.destroy());
    }
    this._modelSelects = [];
    container.querySelectorAll('[data-model-select]').forEach(el => {
      const key = el.dataset.modelSelect;
      const value = el.dataset.modelValue || '';
      const options = this.modelChoices || [];
      
      // 如果当前值不在选项列表中，添加一个额外的选项
      const valueInChoices = options.some(o => o.value === value);
      const allOptions = [...options];
      if (value && !valueInChoices) {
        allOptions.unshift({ value: value, label: `${value} (当前配置)`, tags: [] });
      }
      
      const ms = new ModelSelect({
        options: allOptions,
        value: value,
        onChange: (val) => {
          this.settings[key] = this._stripProviderPrefix(val);
        },
      });
      ms.mount(el);
      this._modelSelects.push(ms);
    });
  }

  /**
   * 收集表单所有值
   * @returns {Object} 配置对象
   */
  collectValues() {
    const values = {};

    for (const [key, schedule] of Object.entries(this._scheduleData)) {
      if (Array.isArray(schedule) && schedule.length > 0) {
        values[key] = schedule;
      }
    }

    document.querySelectorAll(`#${this.containerId} [data-setting-key]`).forEach(input => {
      const key = input.dataset.settingKey;
      if (input.type === 'checkbox') {
        values[key] = input.checked;
      } else if (input.type === 'number') {
        values[key] = parseFloat(input.value) || 0;
      } else if (input.tagName === 'SELECT') {
        values[key] = input.value || '';
      } else {
        values[key] = input.value;
      }
    });

    const listKeys = new Set();
    document.querySelectorAll(`#${this.containerId} [data-list-key]`).forEach(inp => {
      listKeys.add(inp.dataset.listKey);
    });
    listKeys.forEach(key => {
      const vals = [];
      document.querySelectorAll(`#${this.containerId} [data-list-key="${key}"]`).forEach(inp => {
        if (inp.value.trim()) vals.push(inp.value.trim());
      });
      values[key] = vals;
    });

    const repoChecks = document.querySelectorAll(`#${this.containerId} [data-repo-check]`);
    if (repoChecks.length > 0) {
      const checked = [];
      repoChecks.forEach(cb => { if (cb.checked) checked.push(cb.dataset.repoCheck); });
      values['active_repos'] = checked;
    }

    // 收集 object_array 值（从 settings 中同步）
    for (const param of this.parameters) {
      if (param.type === 'object_array' && param.fields) {
        if (this.settings[param.name]) {
          values[param.name] = this.settings[param.name];
        }
      }
    }

    // 收集 checkbox_group 值
    const checkboxGroups = new Set();
    document.querySelectorAll(`#${this.containerId} [data-checkbox-group]`).forEach(cb => {
      checkboxGroups.add(cb.dataset.checkboxGroup);
    });
    checkboxGroups.forEach(key => {
      const checked = [];
      document.querySelectorAll(`#${this.containerId} [data-checkbox-group="${key}"]:checked`).forEach(cb => {
        checked.push(cb.dataset.checkboxValue);
      });
      values[key] = checked;
    });

    // 收集 ModelSelect 值（剥离 provider 前缀，只保留裸模型名）
    if (this._modelSelects) {
      this._modelSelects.forEach(ms => {
        const el = ms._el;
        if (el) {
          const key = el.dataset.modelSelect;
          if (key && ms.value) {
            values[key] = this._stripProviderPrefix(ms.value);
          }
        }
      });
    }

    return values;
  }
}

export class ModelSelect {
  static _instances = new Set();

  static closeAll(except) {
    for (const inst of ModelSelect._instances) {
      if (inst !== except && inst._open) {
        inst._open = false;
        inst._renderDropdown();
        inst._syncTriggerText();
      }
    }
  }

  constructor({ options = [], value = '', onChange = null, placeholder = '请选择模型…' }) {
    this.options = options;
    this.value = value;
    this.onChange = onChange;
    this.placeholder = placeholder;
    this._open = false;
    this._el = null;
    this._onDocClick = this._onDocClick.bind(this);
  }

  mount(container) {
    this._el = container;
    this.render();
    ModelSelect._instances.add(this);
    document.addEventListener('click', this._onDocClick);
  }

  destroy() {
    ModelSelect._instances.delete(this);
    document.removeEventListener('click', this._onDocClick);
  }

  setValue(val) {
    this.value = val;
    this.render();
  }

  _onDocClick(e) {
    if (this._el && !this._el.contains(e.target)) {
      this._open = false;
      this._renderDropdown();
      this._syncTriggerText();
    }
  }

  _selectedLabel() {
    const opt = this.options.find(o => o.value === this.value);
    if (!opt) return this.placeholder;
    const tags = (opt.tags || []).map(t => `<span class="cap-tag-inline">${t}</span>`).join('');
    return `${opt.label}${tags ? ' ' + tags : ''}`;
  }

  _syncTriggerText() {
    if (!this._el) return;
    const textEl = this._el.querySelector('.msel-text');
    const trigger = this._el.querySelector('.msel-trigger');
    if (!textEl) return;
    const sel = this.options.find(o => o.value === this.value);
    textEl.innerHTML = sel ? this._renderTriggerContent(sel) : this.placeholder;
    if (trigger) trigger.classList.toggle('msel-placeholder', !sel);
  }

  _renderTriggerContent(opt) {
    const tags = (opt.tags || []).map(t => `<span class="cap-tag-inline">${t}</span>`).join('');
    return `<span class="msel-label">${opt.label}</span>${tags}`;
  }

  render() {
    if (!this._el) return;
    const sel = this.options.find(o => o.value === this.value);
    const triggerHtml = sel ? this._renderTriggerContent(sel) : this.placeholder;
    this._el.innerHTML = `
      <div class="msel-wrap">
        <button type="button" class="msel-trigger${this._open ? ' msel-open' : ''}${!sel ? ' msel-placeholder' : ''}">
          <span class="msel-text">${triggerHtml}</span>
          <span class="msel-arrow">▾</span>
        </button>
        <div class="msel-dropdown" style="display:${this._open ? 'block' : 'none'}">
          <input type="text" class="msel-search" placeholder="搜索模型…">
          <div class="msel-list"></div>
        </div>
      </div>
    `;
    const trigger = this._el.querySelector('.msel-trigger');
    trigger.addEventListener('click', (e) => {
      e.stopPropagation();
      const willOpen = !this._open;
      ModelSelect.closeAll(this);
      this._open = willOpen;
      this._renderDropdown();
      if (this._open) {
        const input = this._el.querySelector('.msel-search');
        if (input) { input.value = ''; input.focus(); }
      }
    });
    this._renderList('');
    const searchInput = this._el.querySelector('.msel-search');
    if (searchInput) {
      searchInput.addEventListener('input', () => this._renderList(searchInput.value));
      searchInput.addEventListener('click', (e) => e.stopPropagation());
    }
  }

  _renderDropdown() {
    const dd = this._el.querySelector('.msel-dropdown');
    const trigger = this._el.querySelector('.msel-trigger');
    if (!dd || !trigger) return;
    if (this._open) {
      const rect = trigger.getBoundingClientRect();
      const spaceBelow = window.innerHeight - rect.bottom;
      const spaceAbove = rect.top;
      const ddHeight = 280;
      if (spaceBelow < ddHeight && spaceAbove > spaceBelow) {
        dd.style.bottom = '100%';
        dd.style.top = 'auto';
        dd.style.marginBottom = '4px';
        dd.style.marginTop = '0';
      } else {
        dd.style.top = '100%';
        dd.style.bottom = 'auto';
        dd.style.marginTop = '4px';
        dd.style.marginBottom = '0';
      }
      dd.style.display = 'block';
    } else {
      dd.style.display = 'none';
    }
    trigger.classList.toggle('msel-open', this._open);
  }

  _renderList(query) {
    const list = this._el.querySelector('.msel-list');
    if (!list) return;
    const q = query.trim().toLowerCase();
    const filtered = this.options.filter(o => {
      if (!q) return true;
      return o.label.toLowerCase().includes(q) || o.value.toLowerCase().includes(q);
    });
    if (!filtered.length) {
      list.innerHTML = '<div class="msel-empty">无匹配模型</div>';
      return;
    }
    list.innerHTML = filtered.map(o => {
      const tags = (o.tags || []).map(t => `<span class="cap-tag-inline">${t}</span>`).join('');
      const active = o.value === this.value ? ' msel-active' : '';
      return `<div class="msel-option${active}" data-value="${o.value}">
        <span class="msel-option-label">${o.label}</span>
        ${tags ? `<span class="msel-option-tags">${tags}</span>` : ''}
      </div>`;
    }).join('');
    list.querySelectorAll('.msel-option').forEach(el => {
      el.addEventListener('click', (e) => {
        e.stopPropagation();
        this.value = el.dataset.value;
        this._open = false;
        this.render();
        if (this.onChange) this.onChange(this.value);
      });
    });
  }
}
