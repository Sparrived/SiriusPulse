// ── Providers ─────────────────────────────────────────
const BUILTIN_PROVIDER_TYPES = ['deepseek','aliyun-bailian','bigmodel','siliconflow','volcengine-ark','ytea'];
const PROVIDER_TYPE_OPTIONS = [
  {value:'openai-compatible',label:'OpenAI Compatible'},
  {value:'deepseek',label:'DeepSeek'},
  {value:'aliyun-bailian',label:'阿里云百炼'},
  {value:'bigmodel',label:'智谱 BigModel'},
  {value:'siliconflow',label:'SiliconFlow'},
  {value:'volcengine-ark',label:'火山方舟'},
  {value:'ytea',label:'YTea'},
];
const PROVIDER_DEFAULT_URLS = {
  'openai-compatible': 'https://api.openai.com',
  'deepseek': 'https://api.deepseek.com',
  'aliyun-bailian': 'https://dashscope.aliyuncs.com/compatible-mode',
  'bigmodel': 'https://open.bigmodel.cn/api/paas/v4',
  'siliconflow': 'https://api.siliconflow.cn',
  'volcengine-ark': 'https://ark.cn-beijing.volces.com/api/v3',
  'ytea': 'https://api.ytea.top',
};

let providerEditIndex = -1;
let providerBackup = null;

async function loadProviders() {
  try {
    const res = await get('/providers');
    providerDraft = JSON.parse(JSON.stringify(res.providers || []));
    providerEditIndex = -1;
    providerBackup = null;
    _renderProviderDraft();
    $('dashProviderCount').textContent = String(providerDraft.length);
  } catch (e) {}
}

function _providerTypeSelect(i, selected) {
  return `<select onchange="_onProviderTypeChange(${i},this.value)">
    ${PROVIDER_TYPE_OPTIONS.map(o => `<option value="${o.value}"${o.value===selected?' selected':''}>${o.label}</option>`).join('')}
  </select>`;
}

function _onProviderTypeChange(i, val) {
  providerDraft[i].type = val;
  if (BUILTIN_PROVIDER_TYPES.includes(val)) {
    providerDraft[i].base_url = PROVIDER_DEFAULT_URLS[val] || '';
  } else {
    providerDraft[i].base_url = providerDraft[i].base_url || 'https://';
  }
  _renderProviderDraft();
}

function _renderProviderModelsEdit(i) {
  const p = providerDraft[i];
  const models = p.models || [];
  const tags = models.map((m, mi) => `<span class="tag">${m} <span class="remove" onclick="providerDraft[${i}].models.splice(${mi},1);_renderProviderDraft()">✕</span></span>`).join('');
  return `
    <div class="tag-list">${tags}</div>
    <div class="pv-models-add">
      <input type="text" placeholder="添加模型名" id="pmodel-${i}" onkeydown="if(event.key==='Enter'){_addProviderModel(${i})}">
      <button class="btn small" onclick="_addProviderModel(${i})">添加</button>
    </div>
  `;
}

function _addProviderModel(i) {
  const input = $(`pmodel-${i}`);
  const v = input?.value?.trim();
  if (!v) return;
  providerDraft[i].models = providerDraft[i].models || [];
  if (!providerDraft[i].models.includes(v)) {
    providerDraft[i].models.push(v);
  }
  input.value = '';
  _renderProviderDraft();
}

function _maskKey(key) {
  if (!key) return '未设置';
  if (key.length <= 10) return '••••';
  return key.slice(0,6) + '••••' + key.slice(-4);
}

function _shortUrl(url, type) {
  if (!url) return '—';
  if (_isBuiltin(type)) {
    try { return new URL(url).hostname; } catch { return url; }
  }
  return url;
}

function _isBuiltin(type) {
  return BUILTIN_PROVIDER_TYPES.includes(type);
}

function providerToggleEnabled(i) {
  providerDraft[i].enabled = providerDraft[i].enabled === false ? true : false;
  _renderProviderDraft();
  // 自动保存，避免用户忘记点保存
  saveProviders();
}

function providerStartEdit(i) {
  providerBackup = JSON.parse(JSON.stringify(providerDraft[i]));
  providerEditIndex = i;
  _renderProviderDraft();
}

function providerCancelEdit() {
  if (providerEditIndex >= 0 && providerBackup) {
    providerDraft[providerEditIndex] = providerBackup;
  }
  providerEditIndex = -1;
  providerBackup = null;
  _renderProviderDraft();
}

function _renderProviderDraft() {
  const el = $('providerList');
  if (!providerDraft.length) {
    el.innerHTML = '<div style="color:var(--text-2);padding:10px">暂无 Provider，请点击「添加 Provider」。</div>';
    return;
  }
  el.innerHTML = providerDraft.map((p, i) => {
    const isEditing = i === providerEditIndex;
    const builtin = _isBuiltin(p.type);
    if (isEditing) {
      return `
      <div class="provider-row editing">
        <div class="pv-edit-grid">
          <div class="form-group"><label>平台</label>${_providerTypeSelect(i, p.type || '')}</div>
          <div class="form-group"><label>Base URL</label>
            ${builtin
              ? `<input type="text" value="${PROVIDER_DEFAULT_URLS[p.type]||''}" disabled style="opacity:.6">`
              : `<input type="text" value="${p.base_url||''}" oninput="providerDraft[${i}].base_url=this.value">`}
          </div>
          <div class="form-group"><label>API Key</label><input type="password" value="${p.api_key||''}" oninput="providerDraft[${i}].api_key=this.value" placeholder="sk-..."></div>
          <div class="form-group"><label>健康检查模型</label><input type="text" value="${p.healthcheck_model||''}" oninput="providerDraft[${i}].healthcheck_model=this.value"></div>
          <div class="form-group full">
            <label>模型列表</label>
            ${_renderProviderModelsEdit(i)}
          </div>
        </div>
        <div class="pv-edit-footer">
          <label style="display:flex;align-items:center;gap:6px;cursor:pointer;font-size:13px;color:var(--text)">
            <input type="checkbox"${p.enabled!==false?' checked':''} onchange="providerDraft[${i}].enabled=this.checked"> 启用
          </label>
          <div class="pv-actions">
            <button class="btn success small" onclick="saveProviders()">💾 保存</button>
            <button class="btn small" onclick="providerCancelEdit()">取消</button>
            <button class="btn small danger" onclick="providerDraft.splice(${i},1);providerEditIndex=-1;_renderProviderDraft()">删除</button>
          </div>
        </div>
      </div>`;
    }
    // 只读模式
    const modelsHtml = (p.models || []).map(m => `<span class="tag">${m}</span>`).join('');
    const urlDisplay = _shortUrl(p.base_url, p.type);
    const enabled = p.enabled !== false;
    return `
    <div class="provider-row readonly">
      <div class="pv-header">
        <div class="pv-header-left">
          <span class="pv-status ${enabled?'on':'off'}" onclick="providerToggleEnabled(${i})">${enabled?'启用':'禁用'}</span>
          <span class="pv-platform">${p.type || '未命名'}</span>
          ${builtin?'<span class="pv-badge builtin">内置</span>':''}
        </div>
        <div class="pv-actions">
          <button class="btn small" onclick="providerStartEdit(${i})">编辑</button>
          <button class="btn small danger" onclick="providerDraft.splice(${i},1);_renderProviderDraft()">删除</button>
        </div>
      </div>
      <div class="pv-models">${modelsHtml||'<span style="color:var(--text-2);font-size:12px">暂无模型</span>'}</div>
      <div class="pv-meta">
        <div class="pv-meta-item">🔑 <span class="mono">${_maskKey(p.api_key)}</span></div>
        <div class="pv-meta-item">🩺 <span class="mono">${p.healthcheck_model||'—'}</span></div>
        <div class="pv-meta-item">🔗 <span class="mono" title="${p.base_url||''}">${urlDisplay}</span></div>
      </div>
    </div>`;
  }).join('');
  mountCustomSelects(el);
}

function addProvider() {
  if (providerEditIndex >= 0) providerCancelEdit();
  const idx = providerDraft.length;
  providerDraft.push({ type: 'openai-compatible', base_url: 'https://api.openai.com', api_key: '', healthcheck_model: '', enabled: true, models: [] });
  providerStartEdit(idx);
}

async function saveProviders() {
  const res = await post('/providers', { providers: providerDraft });
  toast(res.success ? 'Provider 已保存' : res.error || '保存失败', res.success ? 'success' : 'error');
  if (res.success) {
    providerEditIndex = -1;
    providerBackup = null;
    flashSuccess(document.activeElement);
  }
  loadProviders();
}

// ── Persona ───────────────────────────────────────────
async function loadPersonaPreview() {
  if (!currentPersona) return;
  try {
    const res = await get(pApi('/persona'));
    const p = res.persona || {};
    $('pfName').value = p.name || '';
    $('pfAliases').value = (p.aliases || []).join(' ');
    $('pfSocialRole').value = p.social_role || 'caregiver';
    $('pfSummary').value = p.persona_summary || '';
    $('pfTraits').value = (p.personality_traits || []).join('，');
    $('pfStyle').value = p.communication_style || '';
    $('pfCatchphrases').value = (p.catchphrases || []).join('，');
    $('pfEmoji').value = p.emoji_preference || 'moderate';
    $('pfHumor').value = p.humor_style || 'wholesome';
    $('pfEmpathy').value = p.empathy_style || 'warm';
    $('pfBoundaries').value = (p.boundaries || []).join('，');
    $('pfTaboos').value = (p.taboo_topics || []).join('，');
    $('pfBackstory').value = p.backstory || '';
  } catch (e) {}
}

async function savePersonaForm() {
  if (!currentPersona) { toast('请先选择人格', 'error'); return; }
  const res = await post(pApi('/persona/save'), {
    persona: {
      name: $('pfName').value.trim(),
      aliases: $('pfAliases').value.split(/\s+/).filter(Boolean),
      social_role: $('pfSocialRole').value,
      persona_summary: $('pfSummary').value.trim(),
      personality_traits: $('pfTraits').value.split(/[,，]/).map(s => s.trim()).filter(Boolean),
      communication_style: $('pfStyle').value.trim(),
      catchphrases: $('pfCatchphrases').value.split(/[,，]/).map(s => s.trim()).filter(Boolean),
      emoji_preference: $('pfEmoji').value,
      humor_style: $('pfHumor').value,
      empathy_style: $('pfEmpathy').value,
      boundaries: $('pfBoundaries').value.split(/[,，]/).map(s => s.trim()).filter(Boolean),
      taboo_topics: $('pfTaboos').value.split(/[,，]/).map(s => s.trim()).filter(Boolean),
      backstory: $('pfBackstory').value.trim(),
    }
  });
  toast(res.success ? '人格已保存' : res.error || '保存失败', res.success ? 'success' : 'error');
  if (res.success) flashSuccess(document.activeElement);
  loadPersonaStatus();
}

async function savePersona(jsonStr) {
  if (!currentPersona) return;
  const res = await post(pApi('/persona/save'), { persona: JSON.parse(jsonStr) });
  toast(res.success ? '人格已保存' : res.error || '保存失败', res.success ? 'success' : 'error');
  loadPersonaStatus();
}

const interviewQuestions = [
  '如果把 TA 放进群聊，TA 更像哪类群体角色？是活跃气氛的人、冷幽默观察者、可靠收束者，还是偶尔出手的梗王？',
  'TA 在多人对话里的发言节奏如何？什么时候会抢话、接梗、补刀、收尾，什么时候会选择潜水？',
  'TA 如何区分群内不同关系层级？公开场合和私下场合，对熟人和生人会有什么明显区别？',
  '群里气氛好、被冷落、有人争执、有人单独 cue TA 时，TA 的情绪和反应路径分别是什么？',
  'TA 的群聊语言风格是什么？会不会用梗、方言、昵称、复读、反问、表情包式句法？最该避免哪些 AI 味回复？',
  'TA 在群聊中的边界与禁忌是什么？面对多人起哄、越界玩笑、道德绑架或拉踩时会怎么处理？',
  'TA 在群里最真实的小习惯或记忆点是什么？什么细节会让人一看就觉得「这人很具体」？',
  '这个群聊角色的社交气质从什么经历里长出来？哪些过去的圈子、职业或成长环境塑造了 TA 的群体互动方式？',
];
async function renderInterviewQuestions() {
  // 先渲染空表单
  $('interviewQuestions').innerHTML = interviewQuestions
    .map(
      (q, i) => `
    <div class="question-block">
      <div class="q">Q${i + 1}. ${q}</div>
      <textarea id="ivAns${i}" placeholder="请回答..."></textarea>
    </div>
  `
    )
    .join('');
  // 若有已选人格，尝试加载已有 interview 答案
  if (!currentPersona) return;
  try {
    const data = await get(pApi('/persona/interview'));
    if (data.answers) {
      interviewQuestions.forEach((_, i) => {
        const v = data.answers[String(i + 1)];
        if (v) {
          const el = $(`ivAns${i}`);
          if (el) el.value = v;
        }
      });
    }
    if (data.name) $('ivName').value = data.name;
    if (data.aliases && data.aliases.length) $('ivAliases').value = data.aliases.join(' ');
  } catch (e) {
    // 静默失败：无记录时保持空表单
  }
}

async function generatePersonaInterview() {
  if (!currentPersona) { toast('请先选择人格', 'error'); return; }
  const btn = $('ivBtn');
  if (!btn) return;
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> 生成中...';
  const answers = {};
  interviewQuestions.forEach((_, i) => {
    const v = $(`ivAns${i}`).value.trim();
    if (v) answers[String(i + 1)] = v;
  });
  const res = await post(pApi('/persona/interview'), {
    name: $('ivName').value,
    aliases: $('ivAliases').value.split(/\s+/).filter(Boolean),
    answers,
    model: $('ivModel').value,
  });
  btn.disabled = false;
  btn.innerHTML = '✨ 生成人格';
  if (res.success) {
    $('ivResult').innerHTML = `<div class="preview-box">${JSON.stringify(res.persona, null, 2)}</div><button class="btn success" onclick="savePersona(${JSON.stringify(JSON.stringify(res.persona))})">💾 保存人格</button>`;
  } else {
    toast(res.error || '生成失败', 'error');
  }
}

// ── Orchestration ─────────────────────────────────────
function _fillSelect(id, value, choices) {
  const el = $(id);
  if (!el) return;
  el.innerHTML = '';
  const opts = choices.length ? choices : [{ label: value || 'gpt-4o', value: value || 'gpt-4o' }];
  opts.forEach((c) => {
    const opt = document.createElement('option');
    const label = typeof c === 'string' ? c : c.label;
    const val = typeof c === 'string' ? c : c.value;
    opt.value = val;
    opt.textContent = label;
    if (val === value) opt.selected = true;
    el.appendChild(opt);
  });
  syncCustomSelect(id);
}

async function loadAvailableModels() {
  try {
    const res = await get('/models');
    const choices = res.model_choices || [];
    const defaultModel = 'gpt-4o-mini';
    _fillSelect('kwModel', defaultModel, choices);
    _fillSelect('ivModel', defaultModel, choices);
  } catch (e) {}
}

const ORCH_GENERAL_MAP = {
  analysis_model: ['cognition_analyze', 'memory_extract'],
  chat_model: ['response_generate', 'proactive_generate'],
  vision_model: ['vision'],
};

const ORCH_TASK_GROUPS = [
  {
    title: '分析类',
    generalKey: 'analysis_model',
    tasks: [
      { key: 'cognition_analyze', label: '认知分析' },
      { key: 'memory_extract', label: '记忆提取' },
    ],
  },
  {
    title: '生成类',
    generalKey: 'chat_model',
    tasks: [
      { key: 'response_generate', label: '回复生成' },
      { key: 'proactive_generate', label: '主动发言' },
    ],
  },
  {
    title: '其他',
    generalKey: 'vision_model',
    tasks: [
      { key: 'vision', label: '多模态' },
    ],
  },
  {
    title: '表情包',
    generalKey: 'analysis_model',
    tasks: [
      { key: 'sticker_tag_extract', label: '标签提取' },
      { key: 'sticker_preference_generate', label: '偏好生成' },
    ],
  },
];

let _orchChoices = [];
let _orchData = {};

function _getGeneralModel(generalKey) {
  const sel = $({
    analysis_model: 'orchAnalysis',
    chat_model: 'orchChat',
    vision_model: 'orchVision',
  }[generalKey]);
  return sel ? sel.value : '';
}

function _buildTaskRow(task, choices, data) {
  const tm = data.task_models || {};
  const te = data.task_enabled || {};
  const model = tm[task.key] || '';
  const enabled = te[task.key] !== false;
  const modelSelect = `<select id="orchTaskModel_${task.key}" class="task-model">` +
    `<option value="">继承通用</option>` +
    choices.map(c => {
      const val = typeof c === 'string' ? c : c.value;
      const label = typeof c === 'string' ? c : c.label;
      return `<option value="${val}"${val === model ? ' selected' : ''}>${label}</option>`;
    }).join('') +
    `</select>`;
  const customMark = model ? '<span class="task-custom-mark">自定义</span>' : '';
  return `<div class="task-row${enabled ? '' : ' disabled'}" id="orchTaskRow_${task.key}">` +
    `<div class="task-name">${task.label}${customMark}</div>` +
    `<div class="task-model">${modelSelect}</div>` +
    `<div class="task-toggle"><input type="checkbox" id="orchTaskEnabled_${task.key}"${enabled ? ' checked' : ''} onchange="onOrchTaskToggle('${task.key}')"></div>` +
    `</div>`;
}

function renderOrchestration() {
  const container = $('orchTaskList');
  if (!container) return;
  const choices = _orchChoices;
  const data = _orchData;
  container.innerHTML = ORCH_TASK_GROUPS.map(g => {
    const rows = g.tasks.map(t => _buildTaskRow(t, choices, data)).join('');
    return `<div class="task-group-title">${g.title}</div>${rows}`;
  }).join('');
  setTimeout(() => {
    ORCH_TASK_GROUPS.forEach(g => {
      g.tasks.forEach(t => {
        const sel = $(`orchTaskModel_${t.key}`);
        if (sel && !sel._customMounted) mountCustomSelect(sel);
      });
    });
  }, 0);
}

function onOrchGeneralChanged() {
  // 通用模型变更后，未自定义的任务行视觉上继承新值
  ORCH_TASK_GROUPS.forEach(g => {
    const generalModel = _getGeneralModel(g.generalKey);
    g.tasks.forEach(t => {
      const sel = $(`orchTaskModel_${t.key}`);
      if (sel && !sel.value) {
        // 继承通用：更新 custom select 的显示文本
        syncCustomSelect(`orchTaskModel_${t.key}`);
      }
    });
  });
}

function onOrchTaskToggle(taskKey) {
  const cb = $(`orchTaskEnabled_${taskKey}`);
  const row = $(`orchTaskRow_${taskKey}`);
  if (cb && row) row.classList.toggle('disabled', !cb.checked);
}

async function loadOrchestration() {
  if (!currentPersona) return;
  try {
    const res = await get(pApi('/orchestration'));
    const orch = res || {};
    const choices = orch.model_choices || [];
    _orchChoices = choices;
    _orchData = orch;
    _fillSelect('orchAnalysis', orch.analysis_model || 'gpt-4o-mini', choices);
    _fillSelect('orchChat', orch.chat_model || 'gpt-4o', choices);
    _fillSelect('orchVision', orch.vision_model || 'gpt-4o', choices);
    renderOrchestration();
  } catch (e) {}
}

async function saveOrchestration() {
  if (!currentPersona) { toast('请先选择人格', 'error'); return; }
  const payload = {
    analysis_model: $('orchAnalysis').value,
    chat_model: $('orchChat').value,
    vision_model: $('orchVision').value,
  };
  const taskModels = {};
  const taskEnabled = {};
  ORCH_TASK_GROUPS.forEach(g => {
    g.tasks.forEach(t => {
      const modelEl = $(`orchTaskModel_${t.key}`);
      const enabledEl = $(`orchTaskEnabled_${t.key}`);
      if (modelEl && modelEl.value) taskModels[t.key] = modelEl.value;
      if (enabledEl) taskEnabled[t.key] = enabledEl.checked;
    });
  });
  if (Object.keys(taskModels).length) payload.task_models = taskModels;
  if (Object.keys(taskEnabled).length) payload.task_enabled = taskEnabled;

  const res = await post(pApi('/orchestration'), payload);
  toast(res.success ? '模型编排已保存' : res.error || '失败', res.success ? 'success' : 'error');
  if (res.success) flashSuccess(document.activeElement);
}

// ── Groups ────────────────────────────────────────────
async function loadAdapters() {
  if (!currentPersona) return;
  try {
    const res = await get(pApi('/adapters'));
    const adapters = res.adapters || [];
    personaState.adapters = adapters;
    const a = adapters[0] || {};
    $('adEnabled').value = String(a.enabled !== false);
    $('adQQ').value = a.qq_number || '';
    $('adWsUrl').value = a.ws_url || 'ws://localhost:3001';
    $('adToken').value = a.token || 'napcat_ws';
    $('adRoot').value = a.root || '';
    $('adEnableGroup').value = String(a.enable_group_chat !== false);
    $('adEnablePrivate').value = String(a.enable_private_chat !== false);
    adapterGroupIds = a.allowed_group_ids || [];
    adapterPrivateIds = a.allowed_private_user_ids || [];
    renderGroups(adapterGroupIds);
    renderPrivates(adapterPrivateIds);
    clearAdaptersDirty();
  } catch (e) {
    // 保底初始化，避免 addGroup / saveAdapters 操作空对象
    adapterGroupIds = [];
    adapterPrivateIds = [];
    personaState.adapters = personaState.adapters || [{}];
  }
}

async function createBlankPersona() {
  const name = $('cpName').value.trim();
  if (!name) { toast('请输入人格标识', 'error'); return; }
  const res = await post('/personas', {
    name: name,
    persona_name: $('cpPersonaName').value.trim() || name,
  });
  if (res.success) {
    toast('人格创建成功');
    await loadPersonas();
    selectPersona(name);
    $('cpName').value = '';
    $('cpPersonaName').value = '';
  } else {
    toast(res.error || '创建失败', 'error');
  }
}

async function saveAdapters() {
  if (!currentPersona) { toast('请先选择人格', 'error'); return; }
  const res = await post(pApi('/adapters'), {
    adapters: [{
      type: 'napcat',
      enabled: $('adEnabled').value === 'true',
      qq_number: $('adQQ').value.trim(),
      ws_url: $('adWsUrl').value.trim(),
      token: $('adToken').value.trim(),
      root: $('adRoot').value.trim(),
      enable_group_chat: $('adEnableGroup').value === 'true',
      enable_private_chat: $('adEnablePrivate').value === 'true',
      allowed_group_ids: adapterGroupIds,
      allowed_private_user_ids: adapterPrivateIds,
    }]
  });
  toast(res.success ? 'Adapter 配置已保存' : res.error || '保存失败', res.success ? 'success' : 'error');
  if (res.success) {
    flashSuccess(document.activeElement);
    clearAdaptersDirty();
    const refreshed = await get(pApi('/adapters'));
    personaState.adapters = refreshed.adapters || [];
    const a = personaState.adapters[0] || {};
    adapterGroupIds = a.allowed_group_ids || [];
    adapterPrivateIds = a.allowed_private_user_ids || [];
  }
}

function renderGroups(list) {
  $('groupTags').innerHTML = list
    .map((g) => `<span class="tag">${g} <span class="remove" onclick="removeGroup('${g}')">✕</span></span>`)
    .join('');
}
function renderPrivates(list) {
  $('privateTags').innerHTML = list
    .map((u) => `<span class="tag">${u} <span class="remove" onclick="removePrivate('${u}')">✕</span></span>`)
    .join('');
}

function markAdaptersDirty() {
  const hint = $('adaptersDirtyHint');
  const btn = $('adaptersSaveBtn');
  if (hint) hint.style.display = '';
  if (btn) {
    btn.style.borderColor = 'var(--warn)';
    btn.style.color = 'var(--warn)';
  }
}
function clearAdaptersDirty() {
  const hint = $('adaptersDirtyHint');
  const btn = $('adaptersSaveBtn');
  if (hint) hint.style.display = 'none';
  if (btn) {
    btn.style.borderColor = '';
    btn.style.color = '';
  }
}

function addGroup() {
  const v = $('newGroupId').value.trim();
  if (v) {
    adapterGroupIds = adapterGroupIds || [];
    if (!adapterGroupIds.includes(v)) adapterGroupIds.push(v);
    $('newGroupId').value = '';
    renderGroups(adapterGroupIds);
    markAdaptersDirty();
  }
}
function removeGroup(g) {
  adapterGroupIds = (adapterGroupIds || []).filter((x) => x !== g);
  renderGroups(adapterGroupIds);
  markAdaptersDirty();
}
function addPrivate() {
  const v = $('newPrivateId').value.trim();
  if (v) {
    adapterPrivateIds = adapterPrivateIds || [];
    if (!adapterPrivateIds.includes(v)) adapterPrivateIds.push(v);
    $('newPrivateId').value = '';
    renderPrivates(adapterPrivateIds);
    markAdaptersDirty();
  }
}
function removePrivate(u) {
  adapterPrivateIds = (adapterPrivateIds || []).filter((x) => x !== u);
  renderPrivates(adapterPrivateIds);
  markAdaptersDirty();
}

// ── Engine ────────────────────────────────────────────
async function toggleEngine() {
  if (!currentPersona) { toast('请先选择人格', 'error'); return; }
  const res = await post(pApi('/engine/toggle'), { enabled: !personaState.enabled });
  toast(res.success ? (res.enabled ? 'AI 已开启' : 'AI 已关闭') : res.error || '失败', res.success ? 'success' : 'error');
  loadPersonaStatus();
}

async function reloadEngine() {
  if (!currentPersona) { toast('请先选择人格', 'error'); return; }
  const res = await post(pApi('/engine/reload'), {});
  toast(res.success ? '引擎已重建' : res.error || '失败', res.success ? 'success' : 'error');
  loadPersonaStatus();
}

// ── NapCat ────────────────────────────────────────────
async function ncLoadStatus() {
  try {
    const res = await get('/napcat/status');
    const elInstalled = $('ncInstalled');
    const elRunning = $('ncRunning');
    const elQQ = $('ncQQ');
    if (!res.enabled) {
      if (elInstalled) elInstalled.textContent = '管理未启用';
      if (elRunning) elRunning.textContent = '管理未启用';
      if (elQQ) elQQ.textContent = '管理未启用';
      return;
    }
    const installed = res.installed ? '✅ 已安装' : '❌ 未安装';
    const running = res.running ? '✅ 运行中' : '⏹ 已停止';
    const qq = res.qq_installed ? '✅ 已安装' : '❌ 未检测到';
    if (elInstalled) elInstalled.textContent = installed;
    if (elRunning) elRunning.textContent = running;
    if (elQQ) elQQ.textContent = qq + (res.qq_path ? ` (${res.qq_path})` : '');
    const installBtn = $('ncInstallBtn');
    const startBtn = $('ncStartBtn');
    const stopBtn = $('ncStopBtn');
    if (installBtn) installBtn.style.display = res.installed ? 'none' : 'inline-flex';
    if (startBtn) startBtn.style.display = res.installed ? 'inline-flex' : 'none';
    if (stopBtn) stopBtn.style.display = res.running ? 'inline-flex' : 'none';
  } catch (e) {
    console.error('ncLoadStatus', e);
  }
}

async function ncInstall() {
  const btn = $('ncInstallBtn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> 安装中...';
  const res = await post('/napcat/install', {});
  btn.disabled = false;
  btn.innerHTML = '⬇️ 安装 NapCat';
  toast(res.success ? res.message : res.message || '安装失败', res.success ? 'success' : 'error');
  ncLoadStatus();
}
async function ncStart() {
  const btn = $('ncStartBtn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> 启动中...';
  const res = await post('/napcat/start', {});
  btn.disabled = false;
  btn.innerHTML = '▶ 启动 NapCat';
  toast(res.success ? res.message : res.message || '启动失败', res.success ? 'success' : 'error');
  ncLoadStatus();
}
async function ncStop() {
  const res = await post('/napcat/stop', {});
  toast(res.success ? res.message : res.message || '停止失败', res.success ? 'success' : 'error');
  ncLoadStatus();
}
async function ncLoadLogs() {
  try {
    const res = await get('/napcat/logs?lines=50');
    if (res.enabled) {
      $('ncLogs').textContent = res.logs.length ? res.logs.join('\n') : '暂无日志';
    }
  } catch (e) {}
}

// ── Global Settings ───────────────────────────────────
async function loadGlobalSettings() {
  try {
    const res = await get('/global-config');
    $('gsHost').value = res.webui_host || '0.0.0.0';
    $('gsPort').value = res.webui_port || 8080;
    $('gsLogLevel').value = res.log_level || 'INFO';
    $('gsNapcatDir').value = res.napcat_install_dir || '';
    $('gsNapcatPort').value = res.napcat_base_port || 3001;
    // NapCat 自动管理默认启用，无需配置
  } catch (e) {}
}

async function saveGlobalSettings() {
  const res = await post('/global-config', {
    webui_host: $('gsHost').value,
    webui_port: parseInt($('gsPort').value, 10),
    log_level: $('gsLogLevel').value,
    napcat_install_dir: $('gsNapcatDir').value,
    napcat_base_port: parseInt($('gsNapcatPort').value, 10),
  });
  toast(res.success ? '全局设置已保存' : res.error || '保存失败', res.success ? 'success' : 'error');
  if (res.success) flashSuccess(document.activeElement);
}

// ── Experience ────────────────────────────────────────
async function loadExperience() {
  if (!currentPersona) return;
  try {
    const res = await get(pApi('/experience'));
    const e = res.experience || {};
    $('expReplyMode').value = e.reply_mode || 'auto';
    $('expSensitivity').value = e.engagement_sensitivity ?? 0.5;
    $('expExpressiveness').value = e.expressiveness ?? 0.5;
    updateExpressivenessLabel();
    $('expHeatWindow').value = e.heat_window_seconds ?? 60;
    $('expProactive').value = String(e.proactive_enabled !== false);
    $('expProactiveInterval').value = e.proactive_interval_seconds ?? 300;
    $('expActiveStart').value = e.proactive_active_start_hour ?? 8;
    $('expActiveEnd').value = e.proactive_active_end_hour ?? 23;
    $('expDelayReply').value = String(e.delay_reply_enabled !== false);
    $('expPendingThreshold').value = e.pending_message_threshold ?? 4;
    $('expMinReplyInterval').value = e.min_reply_interval_seconds ?? 0;
    $('expReplyFreqWindow').value = e.reply_frequency_window_seconds ?? 60;
    $('expReplyFreqMax').value = e.reply_frequency_max_replies ?? 8;
    $('expExemptMention').value = String(e.reply_frequency_exempt_on_mention !== false);
    $('expMaxConcurrent').value = e.max_concurrent_llm_calls ?? 1;
    $('expEnableSkills').value = String(e.enable_skills !== false);
    $('expMaxSkillRounds').value = e.max_skill_rounds ?? 3;
    $('expSkillTimeout').value = e.skill_execution_timeout ?? 30;
    $('expAutoInstallDeps').value = String(e.auto_install_skill_deps !== false);
    $('expBasicMemoryHardLimit').value = e.basic_memory_hard_limit ?? 30;
    $('expBasicMemoryContextWindow').value = e.basic_memory_context_window ?? 5;
    $('expDiaryTopK').value = e.diary_top_k ?? 5;
    $('expDiaryTokenBudget').value = e.diary_token_budget ?? 800;
    $('expMemoryDepth').value = e.memory_depth || 'deep';
    $('expOtherAINames').value = (e.other_ai_names || []).join(', ');
  } catch (e) {}
  loadVectorStoreStatus();
}

function updateExpressivenessLabel() {
  const s = Math.max(0, Math.min(1, parseFloat($('expSensitivity').value) || 0));
  const e = Math.max(0, Math.min(1, parseFloat($('expExpressiveness').value) || 0));
  const el = $('expStylePreview');
  if (el) {
    let label = '';
    let desc = '';
    if (s >= 0.7 && e >= 0.7) {
      label = '积极主导型';
      desc = '决策活跃度高，行为边界宽松。倾向于主动参与对话，抢话门槛较低。';
    } else if (s >= 0.7 && e <= 0.3) {
      label = '被动回应型';
      desc = '决策活跃度高，但行为边界严格。内心倾向于参与，仅在被明确指向时回复，冷却时间较长。';
    } else if (s <= 0.3 && e >= 0.7) {
      label = '选择性参与型';
      desc = '决策活跃度低，但行为边界宽松。多数消息不触发回复，一旦决定参与则表现积极。';
    } else if (s <= 0.3 && e <= 0.3) {
      label = '深度观察型';
      desc = '决策活跃度低，行为边界严格。极少参与对话，仅在必要时回应。';
    } else {
      label = '均衡互动型';
      desc = '决策活跃度与行为边界均处于中等水平，根据对话上下文自然参与。';
    }
    el.innerHTML = `<strong style="color:var(--text)">${label}</strong>：${desc}`;
  }
  // Update quadrant dot position
  const dot = $('expQuadrantDot');
  if (dot) {
    // Map 0~1 to padding area inside the box (10% ~ 90%)
    dot.style.left = (10 + s * 80) + '%';
    dot.style.top = (90 - e * 80) + '%';
  }
}

function onQuadrantClick(event) {
  const rect = event.currentTarget.getBoundingClientRect();
  const x = event.clientX - rect.left;
  const y = event.clientY - rect.top;
  const width = rect.width;
  const height = rect.height;
  // Map click position to 0~1 (with 10% padding on each side)
  const s = Math.max(0, Math.min(1, (x / width - 0.1) / 0.8));
  const e = Math.max(0, Math.min(1, (0.9 - y / height) / 0.8));
  $('expSensitivity').value = s.toFixed(1);
  $('expExpressiveness').value = e.toFixed(1);
  updateExpressivenessLabel();
}

async function loadVectorStoreStatus() {
  if (!currentPersona) return;
  const dot = $('vsStatusDot');
  const text = $('vsStatusText');
  const statsRow = $('vsStatsRow');
  const groupList = $('vsGroupList');
  try {
    const res = await get(pApi('/vector-store-status'));
    if (res.available) {
      dot.style.background = '#2ecc71';
      text.textContent = 'Chroma 向量存储正常运行';
      statsRow.style.display = '';
      $('vsTotalEntries').textContent = String(res.total_entries ?? 0);
      $('vsModelName').textContent = res.model || '—';
      const groups = res.groups || [];
      $('vsGroupCount').textContent = String(groups.length);
      if (groups.length > 0) {
        groupList.style.display = '';
        groupList.innerHTML = groups.map(g =>
          `<span class="tag">${g.group_id}: ${g.count} 条</span>`
        ).join('');
      } else {
        groupList.style.display = 'none';
      }
    } else {
      dot.style.background = '#e74c3c';
      text.textContent = 'Chroma 向量存储不可用（未安装 chromadb）';
      statsRow.style.display = 'none';
      groupList.style.display = 'none';
    }
  } catch (e) {
    dot.style.background = '#e74c3c';
    text.textContent = '无法获取向量存储状态';
    statsRow.style.display = 'none';
    groupList.style.display = 'none';
  }
}

async function saveExperience() {
  if (!currentPersona) { toast('请先选择人格', 'error'); return; }
  const res = await post(pApi('/experience'), {
    experience: {
      reply_mode: $('expReplyMode').value,
      engagement_sensitivity: parseFloat($('expSensitivity').value),
      expressiveness: parseFloat($('expExpressiveness').value),
      heat_window_seconds: parseFloat($('expHeatWindow').value),
      proactive_enabled: $('expProactive').value === 'true',
      proactive_interval_seconds: parseFloat($('expProactiveInterval').value),
      proactive_active_start_hour: parseInt($('expActiveStart').value, 10),
      proactive_active_end_hour: parseInt($('expActiveEnd').value, 10),
      delay_reply_enabled: $('expDelayReply').value === 'true',
      pending_message_threshold: parseFloat($('expPendingThreshold').value),
      min_reply_interval_seconds: parseFloat($('expMinReplyInterval').value),
      reply_frequency_window_seconds: parseFloat($('expReplyFreqWindow').value),
      reply_frequency_max_replies: parseInt($('expReplyFreqMax').value, 10),
      reply_frequency_exempt_on_mention: $('expExemptMention').value === 'true',
      max_concurrent_llm_calls: parseInt($('expMaxConcurrent').value, 10),
      enable_skills: $('expEnableSkills').value === 'true',
      max_skill_rounds: parseInt($('expMaxSkillRounds').value, 10),
      skill_execution_timeout: parseFloat($('expSkillTimeout').value),
      auto_install_skill_deps: $('expAutoInstallDeps').value === 'true',
      memory_depth: $('expMemoryDepth').value,
      basic_memory_hard_limit: parseInt($('expBasicMemoryHardLimit').value, 10),
      basic_memory_context_window: parseInt($('expBasicMemoryContextWindow').value, 10),
      diary_top_k: parseInt($('expDiaryTopK').value, 10),
      diary_token_budget: parseInt($('expDiaryTokenBudget').value, 10),
      other_ai_names: $('expOtherAINames').value.split(',').map(s => s.trim()).filter(Boolean),
    }
  });
  toast(res.success ? '体验参数已保存' : res.error || '保存失败', res.success ? 'success' : 'error');
  if (res.success) flashSuccess(document.activeElement);
}

// ── Skills ────────────────────────────────────────────
let _skillsCache = null;
let _currentSkillConfig = null;
let _githubMonitorRepos = [];

// ── Skill 自定义配置注册表 ─────────────────────────────
// 任何 Skill 可通过在此添加条目获得专用可视化配置 UI，
// 替代默认的“参数表单 + JSON 文本域”。
//
// 条目格式:
//   { render, collect, modalWidth? }
//     render(config, meta) → html_string  渲染配置表单
//     collect(config, meta) → object      收集表单数据为 config 对象
//     modalWidth?: string                  模态框最大宽度（默认 "560px"）
//
// 新增自定义 Skill 时在此注册即可，openSkillConfig / saveSkillConfig
// 会自动分发到对应 render/collect。
const _skillConfigRenderers = {

  github_monitor: {
    render(config, meta) {
      return _renderGithubMonitorConfig(config);
    },
    collect(config, meta) {
      return _collectGithubMonitorConfig();
    },
    modalWidth: '740px',
  },

  // registry end
};

async function loadSkills() {
  if (!currentPersona) return;
  try {
    const res = await get(pApi('/skills'));
    const skills = res.skills || [];
    _skillsCache = skills;
    renderSkillsList(skills);
    _populateSkillHistoryFilter(skills);
  } catch (e) {
    console.error('loadSkills', e);
    $('skillsList').innerHTML = '<div style="color:var(--text-2);padding:12px">加载 Skill 列表失败</div>';
  }
  loadTelemetry();
  loadSkillHistory();
}

function _populateSkillHistoryFilter(skills) {
  const sel = $('skillHistoryFilter');
  if (!sel) return;
  const prev = sel.value;
  sel.innerHTML = '<option value="">全部 Skill</option>';
  for (const s of skills) {
    sel.innerHTML += `<option value="${s.name}">${s.name}</option>`;
  }
  sel.value = prev;
}

async function loadSkillHistory() {
  const el = $('skillHistoryList');
  if (!el || !currentPersona) return;
  const filter = $('skillHistoryFilter');
  const skillName = filter ? filter.value : '';
  const qs = skillName ? `?skill_name=${encodeURIComponent(skillName)}` : '';
  try {
    const res = await get(pApi(`/skill-history${qs}`));
    const history = res.history || [];
    if (!history.length) {
      el.innerHTML = '<div style="color:var(--text-2);padding:12px">暂无执行记录</div>';
      return;
    }
    el.innerHTML = history.map((h) => {
      const ts = h.timestamp ? new Date(h.timestamp * 1000).toLocaleString('zh-CN', {
        month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit',
      }) : '-';
      const statusColor = h.success ? 'var(--success)' : 'var(--danger)';
      const statusText = h.success ? '成功' : '失败';
      const duration = h.duration_ms >= 1000 ? `${(h.duration_ms / 1000).toFixed(1)}s` : `${Math.round(h.duration_ms)}ms`;
      const paramsStr = h.params ? JSON.stringify(h.params, null, 2) : '';
      const resultStr = h.result_summary || (h.error || '');
      const caller = h.caller_user_id ? `<span style="color:var(--text-3)">${h.caller_user_id}</span>` : '';

      return `
        <div style="background:var(--bg-2);border:1px solid var(--border);border-radius:8px;padding:10px 12px;margin-bottom:6px;font-size:13px">
          <div style="display:flex;justify-content:space-between;align-items:center;gap:8px;flex-wrap:wrap">
            <div style="display:flex;align-items:center;gap:8px;min-width:0">
              <span style="background:var(--surface);border:1px solid var(--border);border-radius:4px;padding:1px 8px;font-weight:600;font-size:12px;white-space:nowrap">${h.skill_name}</span>
              <span style="color:${statusColor};font-size:12px;font-weight:600">${statusText}</span>
              ${caller}
            </div>
            <div style="display:flex;gap:10px;font-size:11px;color:var(--text-3);flex-shrink:0">
              <span>${duration}</span>
              <span>${ts}</span>
            </div>
          </div>
          ${paramsStr ? `<details style="margin-top:6px"><summary style="font-size:11px;color:var(--text-3);cursor:pointer">参数</summary><pre style="margin:4px 0 0;padding:6px 8px;background:var(--surface);border:1px solid var(--border);border-radius:4px;font-size:11px;white-space:pre-wrap;word-break:break-all;max-height:120px;overflow:auto">${_escapeHtml(paramsStr)}</pre></details>` : ''}
          ${resultStr ? `<details style="margin-top:2px"><summary style="font-size:11px;color:var(--text-3);cursor:pointer">${h.success ? '结果' : '错误'}</summary><pre style="margin:4px 0 0;padding:6px 8px;background:var(--surface);border:1px solid var(--border);border-radius:4px;font-size:11px;white-space:pre-wrap;word-break:break-all;max-height:150px;overflow:auto;color:${h.success ? 'var(--text)' : 'var(--danger)'}">${_escapeHtml(resultStr)}</pre></details>` : ''}
        </div>
      `;
    }).join('');
  } catch (e) {
    console.error('loadSkillHistory', e);
    el.innerHTML = '<div style="color:var(--text-2);padding:12px">加载执行历史失败</div>';
  }
}

function _escapeHtml(str) {
  return str.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;').replace(/"/g, '&quot;');
}

function renderSkillsList(skills) {
  const el = $('skillsList');
  if (!skills.length) {
    el.innerHTML = '<div style="color:var(--text-2);padding:12px">暂无可用 Skill</div>';
    return;
  }
  el.innerHTML = skills.map((s) => {
    const enabled = s.enabled !== false;
    const statusClass = enabled ? 'on' : 'off';
    const statusText = enabled ? '已启用' : '已禁用';
    const paramCount = (s.parameters || []).length;
    const hasConfig = Object.keys(s.config || {}).length > 0;
    return `
    <div class="skill-row" data-name="${s.name}">
      <div class="skill-header">
        <div class="skill-header-left">
          <span class="skill-status ${statusClass}" onclick="toggleSkill('${s.name}', ${!enabled})">${statusText}</span>
          <span class="skill-name">${s.name}</span>
          ${s.version ? `<span class="skill-version">v${s.version}</span>` : ''}
          ${s.developer_only ? '<span class="skill-badge dev">开发者</span>' : ''}
          ${s.silent ? '<span class="skill-badge silent">静默</span>' : ''}
        </div>
        <div class="skill-actions">
          <button class="btn small" onclick="openSkillConfig('${s.name}')">⚙️ 配置</button>
        </div>
      </div>
      <div class="skill-desc">${s.description || '暂无描述'}</div>
      <div class="skill-meta">
        ${paramCount ? `<span class="skill-meta-item">📋 ${paramCount} 个参数</span>` : ''}
        ${hasConfig ? '<span class="skill-meta-item" style="color:var(--accent)">⚙️ 已配置</span>' : ''}
        ${(s.tags || []).map(t => `<span class="skill-tag">${t}</span>`).join('')}
      </div>
    </div>`;
  }).join('');
}

async function toggleSkill(name, enabled) {
  if (!currentPersona) { toast('请先选择人格', 'error'); return; }
  try {
    const res = await post(pApi(`/skills/${name}/toggle`), { enabled });
    if (res.success) {
      toast(`${name} ${enabled ? '已启用' : '已禁用'}`, 'success');
      // 更新缓存并重新渲染
      const skill = (_skillsCache || []).find(s => s.name === name);
      if (skill) skill.enabled = enabled;
      renderSkillsList(_skillsCache || []);
    } else {
      toast(res.error || '操作失败', 'error');
    }
  } catch (e) {
    toast('操作失败', 'error');
  }
}

async function openSkillConfig(name) {
  if (!currentPersona) return;
  try {
    const res = await get(pApi(`/skills/${name}/config`));
    const meta = res.meta || {};
    const config = res.config || {};
    const enabled = res.enabled !== false;
    _currentSkillConfig = { name, meta, config, enabled };

    const modal = $('skillConfigModal');
    if (modal.parentElement !== document.body) {
      document.body.appendChild(modal);
    }

    // 查找是否注册了自定义配置 UI
    const renderer = _skillConfigRenderers[name];

    const modalInner = modal.querySelector('div');
    if (modalInner) {
      modalInner.style.maxWidth = (renderer && renderer.modalWidth) || '560px';
    }

    $('skillConfigTitle').textContent = `${name} 配置`;
    const body = $('skillConfigBody');

    // 启停开关
    let html = `
      <div class="form-group" style="margin-bottom:16px">
        <label style="display:flex;align-items:center;gap:8px;cursor:pointer">
          <input type="checkbox" id="skillCfgEnabled" ${enabled ? 'checked' : ''}>
          <span>启用此 Skill</span>
        </label>
      </div>
    `;

    if (renderer) {
      html += renderer.render(config, meta);
    } else {
      // 通用：参数配置表单
      const params = meta.parameters || [];
      if (params.length) {
        html += '<div style="border-top:1px solid var(--border);padding-top:16px;margin-top:16px"><h4 style="margin:0 0 12px;font-size:14px">运行时参数</h4>';
        params.forEach((p) => {
          const raw = config[p.name] !== undefined ? config[p.name] : p.default;
          const val = raw !== undefined && raw !== null ? raw : '';
          const required = p.required ? ' *' : '';
          html += `<div class="form-group" style="margin-bottom:12px">`;
          html += `<label>${p.name}${required} <span style="color:var(--text-2);font-size:12px">(${p.type})</span></label>`;
          html += `<input type="text" id="skillParam_${p.name}" value="${val}" placeholder="${p.description || ''}">`;
          if (p.description) {
            html += `<div style="font-size:12px;color:var(--text-2);margin-top:4px">${p.description}</div>`;
          }
          html += `</div>`;
        });
        html += '</div>';
      }

      // JSON 文本域：仅在无参数（唯一配置途径）或已有额外数据时显示
      const paramNames = new Set((meta.parameters || []).map(p => p.name));
      const extraKeys = Object.keys(config).filter(k => !paramNames.has(k));
      if (paramNames.size === 0 || extraKeys.length > 0) {
        html += `
          <div style="border-top:1px solid var(--border);padding-top:16px;margin-top:16px">
            <h4 style="margin:0 0 12px;font-size:14px">额外配置（JSON）</h4>
            <div style="font-size:12px;color:var(--text-2);margin-bottom:8px">用于配置 API Key 等 Skill 专属参数，格式为 JSON 对象</div>
            <textarea id="skillCfgExtra" rows="4" style="font-family:monospace;font-size:13px" placeholder='{"api_key": "xxx", "base_url": "https://..."}'>${JSON.stringify(config, null, 2)}</textarea>
          </div>
        `;
      }
    }

    body.innerHTML = html;
    $('skillConfigModal').style.display = 'flex';
  } catch (e) {
    toast('加载配置失败', 'error');
  }
}

function closeSkillConfig() {
  $('skillConfigModal').style.display = 'none';
  _currentSkillConfig = null;
  _githubMonitorRepos = [];
  // 恢复默认模态框宽度
  const modal = $('skillConfigModal');
  if (modal) {
    const modalInner = modal.querySelector('div');
    if (modalInner) modalInner.style.maxWidth = '560px';
  }
}

async function saveSkillConfig() {
  if (!_currentSkillConfig || !currentPersona) return;
  const { name, meta, config: oldConfig } = _currentSkillConfig;
  const enabled = $('skillCfgEnabled').checked;

  // 查找是否注册了自定义配置 UI
  const renderer = _skillConfigRenderers[name];

  let config;
  if (renderer && renderer.collect) {
    config = renderer.collect(oldConfig, meta);
  } else {
    // 通用：收集参数值
    config = {};
    const params = meta.parameters || [];
    params.forEach((p) => {
      const el = $(`skillParam_${p.name}`);
      if (el) {
        const v = el.value.trim();
        if (v !== '') {
          if (p.type === 'int' || p.type === 'integer') {
            const n = parseInt(v, 10);
            if (!isNaN(n)) config[p.name] = n;
          } else if (p.type === 'float' || p.type === 'number') {
            const n = parseFloat(v);
            if (!isNaN(n)) config[p.name] = n;
          } else if (p.type === 'bool' || p.type === 'boolean') {
            config[p.name] = v.toLowerCase() === 'true' || v === '1';
          } else {
            config[p.name] = v;
          }
        }
      }
    });

    // 合并额外 JSON 配置（文本域可能未渲染）
    const extraEl = $('skillCfgExtra');
    if (extraEl) {
      try {
        const extraText = extraEl.value.trim();
        if (extraText) {
          const extra = JSON.parse(extraText);
          Object.assign(config, extra);
        }
      } catch (e) {
        toast('额外配置 JSON 格式错误', 'error');
        return;
      }
    }
  }

  try {
    const res = await post(pApi(`/skills/${name}/config`), { enabled, config });
    if (res.success) {
      toast('配置已保存', 'success');
      closeSkillConfig();
      loadSkills();
    } else {
      toast(res.error || '保存失败', 'error');
    }
  } catch (e) {
    toast('保存失败', 'error');
  }
}

// ── GitHub Monitor 自定义配置（由 _skillConfigRenderers 注册） ──

const GITHUB_MONITOR_EVENT_TYPES = [
  { key: 'issues', label: 'Issues', desc: '新建 / 关闭 / 重开' },
  { key: 'pulls', label: 'Pull Requests', desc: '开启 / 合并 / 关闭' },
  { key: 'releases', label: 'Releases', desc: '新版本发布' },
  { key: 'comments', label: '评论', desc: 'Issue / PR / Commit 评论' },
  { key: 'pushes', label: '推送', desc: '代码推送' },
];

function _renderGithubMonitorConfig(config) {
  const repos = (config.repos || []).map((r, i) => ({ ...r, _idx: i }));
  _githubMonitorRepos = JSON.parse(JSON.stringify(repos));
  const pollSeconds = config.poll_seconds !== undefined ? config.poll_seconds : 120;
  const apiBaseUrl = config.api_base_url || 'https://api.github.com';

  let html = `
    <!-- 全局轮询间隔 -->
    <div style="border-top:1px solid var(--border);padding-top:16px;margin-top:16px;margin-bottom:16px">
      <h4 style="margin:0 0 8px;font-size:14px">⏱️ 轮询间隔</h4>
      <div style="display:flex;align-items:center;gap:8px">
        <input type="number" id="ghPollSeconds" value="${pollSeconds}" min="30" max="3600" step="10"
          style="background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px 8px;font-size:13px;width:100px"
          oninput="_githubUpdatePollSeconds(this.value)">
        <span style="font-size:13px;color:var(--text)">秒</span>
        <span style="font-size:12px;color:var(--text-2)">（30-3600，默认 120）</span>
      </div>
    </div>

    <!-- API 地址 -->
    <div style="margin-bottom:16px">
      <h4 style="margin:0 0 8px;font-size:14px">🌐 GitHub API 地址</h4>
      <div style="display:flex;align-items:center;gap:8px">
        <input type="text" id="ghApiBaseUrl" value="${apiBaseUrl}"
          placeholder="https://api.github.com"
          style="flex:1;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px 8px;font-size:13px;max-width:400px">
      </div>
      <div style="font-size:11px;color:var(--text-2);margin-top:4px">
        国内可改用镜像，如 <code>https://ghproxy.com/https://api.github.com</code>
      </div>
    </div>

    <!-- 监控仓库列表 -->
    <div>
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:12px">
        <h4 style="margin:0;font-size:14px">📦 监控仓库</h4>
        <button class="btn small success" onclick="_githubAddRepo()" style="padding:4px 12px;font-size:12px">➕ 添加仓库</button>
      </div>
      <div style="font-size:12px;color:var(--text-2);margin-bottom:12px">
        配置要监控的 GitHub 仓库，检测到新事件时将自动截图并播报。可在下方为每个仓库独立配置事件类型和通知目标群。
      </div>
      <div id="githubMonitorRepoList">
        ${_renderGithubRepoList()}
      </div>
    </div>
  `;

  return html;
}

function _renderGithubRepoList() {
  if (!_githubMonitorRepos.length) {
    return '<div style="color:var(--text-2);padding:20px;text-align:center;border:1px dashed var(--border);border-radius:8px">暂无监控仓库，点击「添加仓库」开始配置</div>';
  }
  return _githubMonitorRepos.map((repo) => _renderGithubRepoCard(repo)).join('');
}

function _renderGithubRepoCard(repo) {
  const idx = repo._idx;
  const events = repo.events || [];
  return `
    <div class="github-repo-card" style="border:1px solid var(--border);border-radius:8px;padding:16px;margin-bottom:12px;background:var(--bg-2)">
      <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:12px">
        <div style="display:flex;gap:8px;align-items:center;flex:1">
          <input type="text" id="ghRepoOwner_${idx}" value="${repo.owner || ''}"
            placeholder="owner" style="flex:1;min-width:80px;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px 8px;font-size:13px"
            oninput="_githubUpdateRepo(${idx},'owner',this.value)">
          <span style="color:var(--text-2);font-size:18px">/</span>
          <input type="text" id="ghRepoName_${idx}" value="${repo.repo || ''}"
            placeholder="repo" style="flex:1;min-width:80px;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px 8px;font-size:13px"
            oninput="_githubUpdateRepo(${idx},'repo',this.value)">
        </div>
        <button class="btn small danger" onclick="_githubRemoveRepo(${idx})" style="margin-left:8px;flex-shrink:0">✕ 删除</button>
      </div>

      <!-- 事件类型勾选 -->
      <div style="margin-bottom:12px">
        <label style="font-size:12px;color:var(--text-2);display:block;margin-bottom:6px">监控事件</label>
        <div style="display:flex;flex-wrap:wrap;gap:8px">
          ${GITHUB_MONITOR_EVENT_TYPES.map(et => {
            const checked = events.includes(et.key) ? ' checked' : '';
            return `
              <label style="display:flex;align-items:center;gap:4px;cursor:pointer;font-size:13px;color:var(--text);background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:4px 10px;white-space:nowrap">
                <input type="checkbox"${checked} onchange="_githubToggleEvent(${idx},'${et.key}',this.checked)">
                <span>${et.label}</span>
                <span style="font-size:11px;color:var(--text-2)">${et.desc}</span>
              </label>
            `;
          }).join('')}
        </div>
      </div>

      <!-- 目标群 -->
      <div style="margin-bottom:12px">
        <label style="font-size:12px;color:var(--text-2);display:block;margin-bottom:4px">通知目标群（群号，多个用逗号分隔）</label>
        <input type="text" id="ghRepoGroups_${idx}" value="${(repo.groups || []).join(', ')}"
          placeholder="例如: 123456789, 987654321"
          style="width:100%;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px 8px;font-size:13px"
          oninput="_githubUpdateRepo(${idx},'groups_str',this.value)">
      </div>

      <!-- GitHub Token -->
      <div>
        <label style="font-size:12px;color:var(--text-2);display:block;margin-bottom:4px">GitHub Token <span style="color:var(--text-2)">（可选，避免 API 频率限制）</span></label>
        <input type="password" id="ghRepoToken_${idx}" value="${repo.github_token || ''}"
          placeholder="ghp_xxxxxxxxxxxxxxxxxxxx"
          style="width:100%;background:var(--bg);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px 8px;font-size:13px"
          oninput="_githubUpdateRepo(${idx},'github_token',this.value)">
        <div style="font-size:11px;color:var(--text-2);margin-top:4px">提供 Token 后 API 速率限制从 60/小时 → 5000/小时</div>
      </div>
    </div>
  `;
}

function _githubAddRepo() {
  const idx = _githubMonitorRepos.length;
  _githubMonitorRepos.push({
    _idx: idx,
    owner: '',
    repo: '',
    events: ['issues', 'pulls', 'releases'],
    groups: [],
    github_token: '',
    groups_str: '',
  });
  _githubRefreshRepoList();
}

function _githubRemoveRepo(idx) {
  _githubMonitorRepos = _githubMonitorRepos.filter(r => r._idx !== idx);
  // 重新分配 _idx
  _githubMonitorRepos.forEach((r, i) => { r._idx = i; });
  _githubRefreshRepoList();
}

function _githubUpdateRepo(idx, field, value) {
  const repo = _githubMonitorRepos.find(r => r._idx === idx);
  if (!repo) return;
  if (field === 'groups_str') {
    repo.groups_str = value;
    repo.groups = value.split(',').map(s => s.trim()).filter(Boolean);
  } else {
    repo[field] = value;
  }
}

function _githubToggleEvent(idx, eventKey, checked) {
  const repo = _githubMonitorRepos.find(r => r._idx === idx);
  if (!repo) return;
  if (!repo.events) repo.events = [];
  if (checked) {
    if (!repo.events.includes(eventKey)) repo.events.push(eventKey);
  } else {
    repo.events = repo.events.filter(e => e !== eventKey);
  }
}

function _githubUpdatePollSeconds(value) {
  // 值由 DOM 直接读取，此 handler 仅用于 oninput 绑定
}

function _githubRefreshRepoList() {
  const el = $('githubMonitorRepoList');
  if (el) {
    el.innerHTML = _renderGithubRepoList();
  }
}

function _collectGithubMonitorConfig() {
  const repos = _githubMonitorRepos.map(r => ({
    owner: (r.owner || '').trim(),
    repo: (r.repo || '').trim(),
    events: r.events || [],
    groups: r.groups || [],
    github_token: (r.github_token || '').trim(),
  })).filter(r => r.owner && r.repo);
  const pollEl = $('ghPollSeconds');
  const pollSeconds = pollEl ? Math.max(30, Math.min(3600, parseInt(pollEl.value, 10) || 120)) : 120;
  const apiEl = $('ghApiBaseUrl');
  const apiBaseUrl = apiEl ? (apiEl.value || '').trim() || 'https://api.github.com' : 'https://api.github.com';
  return { repos, poll_seconds: pollSeconds, api_base_url: apiBaseUrl };
}

// ── Plugins ───────────────────────────────────────────
let _pluginsCache = null;
let _currentPluginName = null;

registerPageLoader('plugins', { init: loadPlugins, refresh: loadPlugins });

async function loadPlugins() {
  try {
    const res = await get('/plugins');
    const plugins = res.plugins || [];
    _pluginsCache = plugins;
    renderPluginsList(plugins);
  } catch (e) {
    console.error('loadPlugins', e);
    $('pluginsList').innerHTML = '<div style="color:var(--text-2);padding:12px">加载 Plugin 列表失败</div>';
  }
}

function renderPluginsList(plugins) {
  const el = $('pluginsList');
  if (!plugins.length) {
    el.innerHTML = '<div style="color:var(--text-2);padding:12px">暂未发现 Plugin。请将插件放入 <code>plugins/</code> 目录。</div>';
    return;
  }
  el.innerHTML = plugins.map((p) => {
    const enabled = p.enabled !== false;
    const statusClass = enabled ? 'on' : 'off';
    const statusText = enabled ? '已启用' : '已禁用';
    const cmdCount = (p.commands || []).length;
    const paramCount = (p.parameters || []).length;
    const nlCount = (p.nl_examples || []).length;
    return `
    <div class="skill-row" data-name="${p.name}">
      <div class="skill-header">
        <div class="skill-header-left">
          <span class="skill-status ${statusClass}" onclick="togglePlugin('${p.name}', ${!enabled})">${statusText}</span>
          <span class="skill-name">${p.display_name || p.name}</span>
          ${p.version ? `<span class="skill-version">v${p.version}</span>` : ''}
          ${p.author ? `<span class="skill-badge" style="background:var(--surface-2);color:var(--text-2)">${p.author}</span>` : ''}
        </div>
        <div class="skill-actions">
          <button class="btn small" onclick="openPluginDetail('${p.name}')">📝 详情/编辑</button>
        </div>
      </div>
      <div class="skill-desc">${p.description || '暂无描述'}</div>
      <div class="skill-meta">
        ${cmdCount ? `<span class="skill-meta-item">⚡ ${cmdCount} 个指令</span>` : ''}
        ${paramCount ? `<span class="skill-meta-item">📋 ${paramCount} 个参数</span>` : ''}
        ${nlCount ? `<span class="skill-meta-item">💬 ${nlCount} 个NL示例</span>` : ''}
        ${p.has_source ? '<span class="skill-meta-item" style="color:var(--accent)">📝 可编辑</span>' : ''}
        ${(p.commands || []).slice(0, 3).map(c =>
          c.patterns.map(pt => `<span class="skill-tag">${pt}</span>`).join('')
        ).join('')}
      </div>
    </div>`;
  }).join('');
}

async function togglePlugin(name, enabled) {
  try {
    const res = await post(`/plugins/${name}/toggle`, { enabled });
    if (res.success) {
      toast(`${name} ${enabled ? '已启用' : '已禁用'}`, 'success');
      const p = (_pluginsCache || []).find(p => p.name === name);
      if (p) p.enabled = enabled;
      renderPluginsList(_pluginsCache || []);
    } else {
      toast(res.error || '操作失败', 'error');
    }
  } catch (e) {
    toast('操作失败', 'error');
  }
}

async function reloadPlugins() {
  try {
    const res = await post('/plugins/reload', {});
    toast(`刷新完成: ${res.total} 个插件 (启用 ${res.enabled})`, 'success');
    await loadPlugins();
  } catch (e) {
    toast('刷新失败', 'error');
  }
}

async function openPluginDetail(name) {
  _currentPluginName = name;
  try {
    const res = await get(`/plugins/${name}`);

    $('pluginDetailTitle').textContent = `${res.display_name || res.name} - 详情`;
    const meta = $('pluginDetailMeta');
    meta.innerHTML = `
      <div class="stat-grid" style="grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:8px">
        <div style="background:var(--bg-2);padding:8px 12px;border-radius:6px"><span style="color:var(--text-2);font-size:11px">版本</span><br>${res.version || '-'}</div>
        <div style="background:var(--bg-2);padding:8px 12px;border-radius:6px"><span style="color:var(--text-2);font-size:11px">作者</span><br>${res.author || '-'}</div>
        <div style="background:var(--bg-2);padding:8px 12px;border-radius:6px"><span style="color:var(--text-2);font-size:11px">指令数</span><br>${(res.commands || []).length}</div>
        <div style="background:var(--bg-2);padding:8px 12px;border-radius:6px"><span style="color:var(--text-2);font-size:11px">状态</span><br>
          <span style="color:${res.enabled !== false ? 'var(--success)' : 'var(--danger)'}">${res.enabled !== false ? '已启用' : '已禁用'}</span>
        </div>
      </div>
    `;

    const cmds = res.commands || [];
    let cmdHtml = cmds.length ? '<div style="margin-top:12px"><h4 style="margin:0 0 8px;font-size:14px">指令</h4>' : '';
    cmds.forEach(c => {
      cmdHtml += `
        <div style="background:var(--bg-2);border:1px solid var(--border);border-radius:6px;padding:8px 12px;margin-bottom:6px;font-size:13px">
          <span style="font-weight:600">${c.name}</span>
          <span style="color:var(--text-3);margin-left:8px">${c.patterns.join(', ')}</span>
          <span style="color:var(--text-3);margin-left:8px;font-size:11px">(${c.pattern_type})</span>
          ${c.description ? `<div style="color:var(--text-2);font-size:12px;margin-top:4px">${c.description}</div>` : ''}
        </div>
      `;
    });
    if (cmds.length) cmdHtml += '</div>';
    $('pluginDetailCommands').innerHTML = cmdHtml;

    const params = res.parameters || [];
    let paramHtml = params.length ? '<div style="margin-top:12px"><h4 style="margin:0 0 8px;font-size:14px">参数</h4>' : '';
    params.forEach(p => {
      const req = p.required ? '<span style="color:var(--danger);font-size:11px"> 必填</span>' : '<span style="color:var(--text-3);font-size:11px"> 可选</span>';
      paramHtml += `
        <div style="background:var(--bg-2);border:1px solid var(--border);border-radius:6px;padding:8px 12px;margin-bottom:6px;font-size:13px">
          <span style="font-weight:600">${p.name}</span>
          <span style="color:var(--text-3);margin-left:8px;font-size:12px">(${p.type})</span>${req}
          ${p.description ? `<div style="color:var(--text-2);font-size:12px;margin-top:4px">${p.description}</div>` : ''}
        </div>
      `;
    });
    if (params.length) paramHtml += '</div>';
    $('pluginDetailParams').innerHTML = paramHtml;

    const nls = res.nl_examples || [];
    if (nls.length) {
      $('pluginDetailNL').innerHTML = `
        <div style="margin-top:12px">
          <h4 style="margin:0 0 8px;font-size:14px">自然语言示例</h4>
          <div style="display:flex;flex-wrap:wrap;gap:6px">
            ${nls.map(e => `<code style="background:var(--bg-2);padding:4px 8px;border-radius:4px;font-size:12px">${e}</code>`).join('')}
          </div>
        </div>
      `;
    } else {
      $('pluginDetailNL').innerHTML = '';
    }

    const modal = $('pluginDetailModal');
    if (modal.parentElement !== document.body) {
      document.body.appendChild(modal);
    }
    modal.style.display = 'flex';

    // 加载权限配置和自定义配置
    console.log('openPluginDetail res:', res);
    console.log('res.settings:', res.settings);
    console.log('res.settings type:', typeof res.settings);
    console.log('res.settings keys:', res.settings ? Object.keys(res.settings) : 'null');
    await loadPluginConfigForm(name, res.settings, res.parameters);
  } catch (e) {
    toast('加载插件详情失败', 'error');
  }
}

async function loadPluginConfigForm(name, settings, parameters) {
  console.log('loadPluginConfigForm called with:', { name, settings, parameters });
  console.log('settings JSON:', JSON.stringify(settings));
  console.log('settings keys:', settings ? Object.keys(settings) : 'null');
  
  try {
    const cfg = await get(`/plugins/${name}/config`);
    $('pluginCfgDevOnly').checked = cfg.developer_only || false;
    $('pluginCfgGroupBlacklist').value = (cfg.group_blacklist || []).join('\n');
    $('pluginCfgRateLimit').value = cfg.rate_limit_calls_per_minute || 60;
  } catch (e) {
    console.warn('加载插件配置失败', e);
  }

  // 渲染自定义配置表单
  const settingsContainer = $('pluginDetailSettings');
  const settingsForm = $('pluginSettingsForm');
  
  console.log('settingsContainer:', settingsContainer, 'settingsForm:', settingsForm);
  
  // 初始化定时配置数据（自动识别所有 schedule 类型的配置）
  _pluginScheduleData = {};
  if (settings) {
    for (const [key, value] of Object.entries(settings)) {
      if (Array.isArray(value) && value.length > 0 && 
          typeof value[0] === 'object' && 'time' in value[0] && 'duration' in value[0]) {
        _pluginScheduleData[key] = value.map(s => ({ ...s }));
      }
    }
  }
  
  console.log('_pluginScheduleData:', _pluginScheduleData);
  console.log('settings check:', settings, 'keys:', settings ? Object.keys(settings) : 'null');
  
  if (Object.keys(settings).length > 0) {
    // 有已保存的配置
    settingsContainer.style.display = 'block';
    settingsForm.innerHTML = renderPluginSettingsForm(settings, parameters);
  } else if ((parameters || []).length > 0) {
    // 从未保存过配置，但插件声明了参数 → 用默认值渲染表单
    const defaultSettings = {};
    parameters.forEach(p => {
      if (p.default !== undefined && p.default !== null) {
        defaultSettings[p.name] = p.default;
      }
    });
    settingsContainer.style.display = 'block';
    settingsForm.innerHTML = renderPluginSettingsForm(defaultSettings, parameters);
  } else {
    settingsContainer.style.display = 'none';
    settingsForm.innerHTML = '';
  }
}

function renderPluginSettingsForm(settings, parameters) {
  // 根据参数定义和当前设置渲染表单
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
    
    if (type === 'boolean') {
      fields.push(`
        <div class="form-group" style="margin-bottom:12px">
          <label style="display:flex;align-items:center;gap:8px;cursor:pointer">
            <input type="checkbox" data-setting-key="${key}" ${value ? 'checked' : ''}>
            <span>${param.name}</span>
          </label>
          ${desc ? `<span style="color:var(--text-3);font-size:11px;margin-left:24px">${desc}</span>` : ''}
        </div>
      `);
    } else if (type === 'int' || type === 'number') {
      fields.push(`
        <div class="form-group" style="margin-bottom:12px">
          <label style="display:block;margin-bottom:4px">${param.name}</label>
          <input type="number" data-setting-key="${key}" value="${value ?? defaultVal ?? 0}"
            style="background:var(--bg-2);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px 8px;font-size:13px;width:150px">
          ${desc ? `<span style="color:var(--text-3);font-size:11px;margin-left:8px">${desc}</span>` : ''}
        </div>
      `);
    } else if (type === 'string' || type === 'str') {
      fields.push(`
        <div class="form-group" style="margin-bottom:12px">
          <label style="display:block;margin-bottom:4px">${param.name}</label>
          <input type="text" data-setting-key="${key}" value="${value ?? defaultVal ?? ''}"
            style="background:var(--bg-2);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px 8px;font-size:13px;width:100%">
          ${desc ? `<span style="color:var(--text-3);font-size:11px">${desc}</span>` : ''}
        </div>
      `);
    } else if (type === 'list' || type === 'array') {
      const listVal = Array.isArray(value) ? value : (defaultVal ? [defaultVal] : []);
      fields.push(`
        <div class="form-group" style="margin-bottom:12px">
          <label style="display:block;margin-bottom:4px">${param.name}</label>
          <div id="pluginList_${key}" style="margin-bottom:8px">
            ${listVal.map((v, i) => `
              <div style="display:flex;gap:4px;margin-bottom:4px">
                <input type="text" value="${v}" data-list-key="${key}" data-list-index="${i}"
                  style="background:var(--bg-2);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px 8px;font-size:13px;flex:1">
                <button class="btn small" onclick="removePluginListItem('${key}', ${i})" style="padding:2px 8px">✕</button>
              </div>
            `).join('')}
          </div>
          <button class="btn small" onclick="addPluginListItem('${key}')" style="padding:4px 12px;font-size:12px">+ 添加</button>
          ${desc ? `<span style="color:var(--text-3);font-size:11px;margin-left:8px">${desc}</span>` : ''}
        </div>
      `);
    }
  }
  
  // 第二步：处理 settings 中存在但 parameters 中没有定义的复杂类型
  for (const [key, value] of Object.entries(settings || {})) {
    if (renderedKeys.has(key)) continue;
    
    // 检测 schedule 类型（数组，每个元素包含 time 和 duration）
    if (Array.isArray(value) && value.length > 0 && 
        typeof value[0] === 'object' && 'time' in value[0] && 'duration' in value[0]) {
      renderedKeys.add(key);
      fields.push(renderScheduleConfig(key, value));
    }
  }
  
  return fields.join('') || '<div style="color:var(--text-2);font-size:13px">暂无可配置项</div>';
}

// 渲染定时配置（通用 schedule 类型）
function renderScheduleConfig(key, schedule) {
  const scheduleId = `pluginSchedule_${key}`;
  return `
    <div class="form-group" style="margin-bottom:12px">
      <label style="font-weight:600;margin-bottom:8px;display:block">${formatConfigKey(key)}</label>
      <div id="${scheduleId}" style="margin-bottom:8px">
        ${schedule.map((s, i) => `
          <div class="schedule-item" style="display:flex;gap:8px;align-items:center;margin-bottom:6px" data-index="${i}">
            <input type="time" value="${s.time || '22:00'}" 
              style="background:var(--bg-2);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px 8px;font-size:13px"
              onchange="updatePluginScheduleItem('${key}', ${i}, 'time', this.value)">
            <span style="color:var(--text-2);font-size:12px">分析时长</span>
            <input type="number" value="${s.duration || 1440}" min="1" max="10080"
              style="background:var(--bg-2);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px 8px;font-size:13px;width:80px"
              onchange="updatePluginScheduleItem('${key}', ${i}, 'duration', parseInt(this.value, 10))">
            <span style="color:var(--text-2);font-size:12px">分钟</span>
            <button class="btn small" style="padding:2px 8px;font-size:11px" onclick="removePluginScheduleItem('${key}', ${i})">✕</button>
          </div>
        `).join('')}
      </div>
      <button class="btn small" style="padding:4px 12px;font-size:12px" onclick="addPluginScheduleItem('${key}')">+ 添加定时</button>
    </div>
  `;
}

// 格式化配置 key 为可读名称
function formatConfigKey(key) {
  return key.replace(/_/g, ' ').replace(/^\w/, c => c.toUpperCase());
}

// 插件定时配置辅助函数（支持多个 schedule key）
let _pluginScheduleData = {};

function updatePluginScheduleItem(key, index, field, value) {
  if (!_pluginScheduleData[key]) _pluginScheduleData[key] = [];
  if (!_pluginScheduleData[key][index]) _pluginScheduleData[key][index] = {};
  _pluginScheduleData[key][index][field] = value;
}

function addPluginScheduleItem(key) {
  if (!_pluginScheduleData[key]) _pluginScheduleData[key] = [];
  _pluginScheduleData[key].push({ time: '22:00', duration: 1440 });
  renderPluginScheduleList(key);
}

function removePluginScheduleItem(key, index) {
  if (_pluginScheduleData[key]) {
    _pluginScheduleData[key].splice(index, 1);
    renderPluginScheduleList(key);
  }
}

function renderPluginScheduleList(key) {
  const container = $(`pluginSchedule_${key}`);
  if (!container || !_pluginScheduleData[key]) return;
  
  container.innerHTML = _pluginScheduleData[key].map((s, i) => `
    <div class="schedule-item" style="display:flex;gap:8px;align-items:center;margin-bottom:6px" data-index="${i}">
      <input type="time" value="${s.time || '22:00'}" 
        style="background:var(--bg-2);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px 8px;font-size:13px"
        onchange="updatePluginScheduleItem('${key}', ${i}, 'time', this.value)">
      <span style="color:var(--text-2);font-size:12px">分析时长</span>
      <input type="number" value="${s.duration || 1440}" min="1" max="10080"
        style="background:var(--bg-2);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px 8px;font-size:13px;width:80px"
        onchange="updatePluginScheduleItem('${key}', ${i}, 'duration', parseInt(this.value, 10))">
      <span style="color:var(--text-2);font-size:12px">分钟</span>
      <button class="btn small" style="padding:2px 8px;font-size:11px" onclick="removePluginScheduleItem('${key}', ${i})">✕</button>
    </div>
  `).join('');
}

function addPluginListItem(key) {
  const container = document.getElementById('pluginList_' + key);
  if (!container) return;
  const idx = container.querySelectorAll('[data-list-key]').length;
  const div = document.createElement('div');
  div.style.cssText = 'display:flex;gap:4px;margin-bottom:4px';
  div.innerHTML = `
    <input type="text" data-list-key="${key}" data-list-index="${idx}"
      style="background:var(--bg-2);color:var(--text);border:1px solid var(--border);border-radius:6px;padding:6px 8px;font-size:13px;flex:1">
    <button class="btn small" onclick="removePluginListItem('${key}', ${idx})" style="padding:2px 8px">✕</button>
  `;
  container.appendChild(div);
}

function removePluginListItem(key, index) {
  const container = document.getElementById('pluginList_' + key);
  if (!container) return;
  const inputs = container.querySelectorAll('[data-list-key]');
  let found = null;
  inputs.forEach(inp => {
    if (parseInt(inp.getAttribute('data-list-index')) === index) {
      found = inp.parentElement;
    }
  });
  if (found) found.remove();
  // Re-index
  container.querySelectorAll('[data-list-key]').forEach((inp, i) => {
    inp.setAttribute('data-list-index', i);
  });
  // Update remove buttons onclick
  const buttons = container.querySelectorAll('button');
  buttons.forEach((btn, i) => {
    btn.setAttribute('onclick', `removePluginListItem('${key}', ${i})`);
  });
}

async function savePluginConfig() {
  if (!_currentPluginName) return;
  
  // 收集权限配置
  const permBody = {
    developer_only: $('pluginCfgDevOnly').checked,
    group_blacklist: $('pluginCfgGroupBlacklist').value.split('\n').map(s => s.trim()).filter(Boolean),
    rate_limit_calls_per_minute: parseInt($('pluginCfgRateLimit').value, 10) || 60,
  };
  
  // 收集自定义配置
  const settingsBody = {};
  
  // 处理 schedule 配置（支持多个 key）
  for (const [key, schedule] of Object.entries(_pluginScheduleData)) {
    if (Array.isArray(schedule) && schedule.length > 0) {
      settingsBody[key] = schedule;
    }
  }
  
  // 处理其他配置项
  const settingInputs = document.querySelectorAll('[data-setting-key]');
  settingInputs.forEach(input => {
    const key = input.getAttribute('data-setting-key');
    if (input.type === 'checkbox') {
      settingsBody[key] = input.checked;
    } else if (input.type === 'number') {
      settingsBody[key] = parseFloat(input.value) || 0;
    } else {
      settingsBody[key] = input.value;
    }
  });

  // 收集 list 类型配置（data-list-key 输入框）
  const listKeys = new Set();
  document.querySelectorAll('[data-list-key]').forEach(inp => {
    listKeys.add(inp.getAttribute('data-list-key'));
  });
  listKeys.forEach(key => {
    const values = [];
    document.querySelectorAll(`[data-list-key="${key}"]`).forEach(inp => {
      if (inp.value.trim()) values.push(inp.value.trim());
    });
    settingsBody[key] = values;
  });
  
  try {
    // 保存权限配置
    const permRes = await fetch(API + `/plugins/${_currentPluginName}/config`, {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(permBody),
    });
    const permData = await permRes.json();
    
    // 保存自定义配置（如果有）
    let settingsData = { success: true };
    if (Object.keys(settingsBody).length > 0) {
      const settingsRes = await post(`/plugins/${_currentPluginName}/settings`, { settings: settingsBody });
      settingsData = settingsRes;
    }
    
    if (permData.success && settingsData.success) {
      toast('✅ 配置已保存（需重启人格生效）', 'success');
      closePluginDetail();
    } else {
      toast(permData.error || settingsData.error || '保存失败', 'error');
    }
  } catch (e) {
    toast('保存失败: ' + e.message, 'error');
  }
}

function closePluginDetail() {
  $('pluginDetailModal').style.display = 'none';
  _currentPluginName = null;
}

// ── Stickers ──────────────────────────────────────────
let _currentStickerId = null;

async function loadStickers() {
  if (!currentPersona) { toast('请先选择人格', 'error'); return; }
  try {
    const data = await get(pApi('/stickers'));
    if (data.error) { toast(data.error, 'error'); return; }

    // 统计卡片
    const stats = data.stats || {};
    $('ssTotal').textContent = stats.total || 0;
    $('ssGroups').textContent = stats.groups || 0;
    $('ssUsage').textContent = stats.total_usage || 0;
    const vs = data.vector_store || {};
    $('ssVector').textContent = vs.available ? `${vs.total_entries} 条` : '未启用';
    $('ssGeneralized').textContent = stats.generalized_count || 0;
    $('ssLearning').textContent = stats.learning_count || 0;

    // 偏好信息
    const pref = data.preference || {};
    const prefCard = $('stickerPreferenceCard');
    const prefBody = $('stickerPreferenceBody');
    if (pref && Object.keys(pref).length > 0) {
      prefCard.style.display = 'block';
      const preferredTags = pref.preferred_tags || [];
      const avoidedTags = pref.avoided_tags || [];
      const preferred = preferredTags.length > 0
        ? preferredTags.map(t => `<span class="tag" style="border-color:var(--accent)">${t}</span>`).join(' ')
        : '<span style="color:var(--text-2)">无</span>';
      const avoided = avoidedTags.length > 0
        ? avoidedTags.map(t => `<span class="tag" style="border-color:var(--danger)">${t}</span>`).join(' ')
        : '<span style="color:var(--text-2)">无</span>';
      const novelty = ((pref.novelty_preference || 0.5) * 100).toFixed(0);
      const emotionMap = pref.emotion_tag_map || {};
      const emotionHtml = Object.keys(emotionMap).length > 0
        ? Object.entries(emotionMap).map(([k, v]) => `<span class="tag">${k} → ${(v||[]).join(',')}</span>`).join(' ')
        : '<span style="color:var(--text-2)">尚未学习</span>';
      const tagSuccess = pref.tag_success_rate || {};
      const topSuccess = Object.entries(tagSuccess).sort((a,b) => b[1]-a[1]).slice(0, 8);
      const successHtml = topSuccess.length > 0
        ? topSuccess.map(([tag, rate]) => `<span class="tag">${tag} ${(rate*100).toFixed(0)}%</span>`).join(' ')
        : '<span style="color:var(--text-2)">尚未学习</span>';
      const groupFeedback = pref.group_tag_feedback || {};
      const topFeedback = Object.entries(groupFeedback).sort((a,b) => b[1]-a[1]).slice(0, 8);
      const feedbackHtml = topFeedback.length > 0
        ? topFeedback.map(([tag, rate]) => {
            const color = rate >= 0.6 ? 'var(--accent)' : rate <= 0.4 ? 'var(--danger)' : 'var(--text-2)';
            return `<span class="tag" style="border-color:${color}">${tag} ${(rate*100).toFixed(0)}%</span>`;
          }).join(' ')
        : '<span style="color:var(--text-2)">尚未学习</span>';
      const styleWeights = pref.style_weights || {};
      const styleHtml = Object.keys(styleWeights).length > 0
        ? Object.entries(styleWeights).map(([k, v]) => `<span class="tag">${k}: ${(v).toFixed(2)}</span>`).join(' ')
        : '<span style="color:var(--text-2)">尚未生成</span>';
      prefBody.innerHTML = `
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;font-size:13px">
          <div><strong>偏好标签：</strong><div style="margin-top:4px;display:flex;flex-wrap:wrap;gap:4px">${preferred}</div></div>
          <div><strong>回避标签：</strong><div style="margin-top:4px;display:flex;flex-wrap:wrap;gap:4px">${avoided}</div></div>
          <div><strong>喜新程度：</strong><span>${novelty}%</span></div>
          <div><strong>风格权重：</strong><span>${styleHtml}</span></div>
        </div>
        <div style="margin-top:10px;font-size:13px">
          <strong>情绪→标签映射：</strong><div style="margin-top:4px;display:flex;flex-wrap:wrap;gap:4px">${emotionHtml}</div>
        </div>
        <div style="margin-top:10px;font-size:13px">
          <strong>标签成功率（群友反馈）：</strong><div style="margin-top:4px;display:flex;flex-wrap:wrap;gap:4px">${successHtml}</div>
        </div>
        <div style="margin-top:10px;font-size:13px">
          <strong>群聊标签反馈：</strong><div style="margin-top:4px;display:flex;flex-wrap:wrap;gap:4px">${feedbackHtml}</div>
        </div>
      `;
    } else {
      prefCard.style.display = 'none';
    }

    // 标签云
    const tagsEl = $('stickerTags');
    const topTags = stats.top_tags || [];
    if (topTags.length === 0) {
      tagsEl.innerHTML = '<span style="color:var(--text-2)">暂无标签数据</span>';
    } else {
      tagsEl.innerHTML = topTags.map(([tag, count]) =>
        `<span class="tag" style="font-size:${12 + Math.min(count * 2, 8)}px">${tag} (${count})</span>`
      ).join('');
    }

    // 表情包列表
    renderStickerList(data.records || []);
  } catch (e) {
    toast('加载表情包数据失败', 'error');
    console.error(e);
  }
}

function renderStickerList(records) {
  const el = $('stickerList');
  if (records.length === 0) {
    el.innerHTML = '<p style="color:var(--text-2)">暂无表情包数据</p>';
    return;
  }
  el.innerHTML = records.map(r => {
    const tags = (r.tags || []).map(t => `<span class="tag">${t}</span>`).join('');
    const usage = r.usage_count || 0;
    const contextPreview = (r.usage_context || '').substring(0, 60).replace(/\n/g, ' ');
    const sceneCount = r.scene_generalize_count || 0;
    const sceneBadge = sceneCount > 0
      ? `<span class="tag" style="background:var(--accent);color:#fff;border-color:var(--accent)">场景概括 ${sceneCount}/3</span>`
      : '';
    const isPending = r.learning_status === 'pending';
    const learningBadge = isPending
      ? '<span class="tag" style="background:var(--warning,#e6a700);color:#fff;border-color:var(--warning,#e6a700)">⏳ 学习中</span>'
      : '';
    return `
      <div class="sticker-item" style="display:flex;gap:12px;padding:12px;border:1px solid var(--border);border-radius:8px;margin-bottom:8px;cursor:pointer;align-items:flex-start;${isPending ? 'opacity:0.7' : ''}"
           onclick="openStickerDetail('${r.sticker_id}')"
           title="使用情境：${contextPreview}...">
        <div style="width:64px;height:64px;background:var(--surface-2);border-radius:6px;display:flex;align-items:center;justify-content:center;flex-shrink:0;font-size:28px"
             title="${r.caption || ''}">🖼️</div>
        <div style="flex:1;min-width:0">
          <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:4px">
            <strong style="font-size:14px">${r.caption || '未命名表情包'}</strong>
            <span style="font-size:12px;color:var(--text-2)">使用 ${usage} 次</span>
          </div>
          <div style="font-size:12px;color:var(--text-2);margin-bottom:6px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">
            情境：${contextPreview}...
          </div>
          <div style="display:flex;flex-wrap:wrap;gap:4px">${learningBadge}${sceneBadge}${tags}</div>
        </div>
      </div>
    `;
  }).join('');
}

async function openStickerDetail(stickerId) {
  if (!currentPersona) return;
  _currentStickerId = stickerId;
  try {
    const data = await get(pApi(`/stickers/${stickerId}`));
    if (data.error) { toast(data.error, 'error'); return; }
    const r = data.record || {};
    $('stickerDetailTitle').textContent = r.caption || '表情包详情';
    const tags = (r.tags || []).map(t => `<span class="tag">${t}</span>`).join('') || '无';
    const sceneCount = r.scene_generalize_count || 0;
    const sceneSummary = r.scene_summary || '';
    const sceneHtml = sceneSummary
      ? `<div><strong>场景概括：</strong><pre style="background:var(--surface-2);padding:8px;border-radius:6px;white-space:pre-wrap;margin:4px 0;border-left:3px solid var(--accent)">${sceneSummary}</pre></div>`
      : '<div><strong>场景概括：</strong><span style="color:var(--text-2)">尚未生成（需累积 8 次观察）</span></div>';
    const isPending = r.learning_status === 'pending';
    const pendingBanner = isPending
      ? '<div style="background:var(--warning,#e6a700);color:#fff;padding:8px 12px;border-radius:6px;margin-bottom:12px;font-size:13px">⏳ 正在学习中，标签和详细信息即将更新...</div>'
      : '';
    $('stickerDetailBody').innerHTML = `
      ${pendingBanner}
      <div style="display:grid;gap:10px;font-size:13px">
        <div><strong>ID：</strong><code>${r.sticker_id}</code></div>
        <div><strong>图片描述：</strong>${r.caption || '无'}</div>
        ${isPending ? '' : sceneHtml}
        ${isPending ? '' : `<div><strong>场景概括次数：</strong>${sceneCount} / 3</div>`}
        <div><strong>使用情境：</strong><pre style="background:var(--surface-2);padding:8px;border-radius:6px;white-space:pre-wrap;margin:4px 0">${r.usage_context || '无'}</pre></div>
        <div><strong>触发消息：</strong>${r.trigger_message || '无'}</div>
        <div><strong>触发情绪：</strong>${r.trigger_emotion || '无'}</div>
        <div><strong>来源用户：</strong>${r.source_user || '未知'}</div>
        <div><strong>来源群聊：</strong>${r.source_group || '未知'}</div>
        <div><strong>发现时间：</strong>${r.discovered_at || '未知'}</div>
        ${isPending ? '' : `<div><strong>使用次数：</strong>${r.usage_count || 0}</div>`}
        ${isPending ? '' : `<div><strong>新鲜度：</strong>${((r.novelty_score || 1) * 100).toFixed(0)}%</div>`}
        <div><strong>标签：</strong>${tags}</div>
      </div>
    `;
    $('stickerDetailModal').style.display = 'flex';
  } catch (e) {
    toast('加载详情失败', 'error');
  }
}

function closeStickerDetail() {
  $('stickerDetailModal').style.display = 'none';
  _currentStickerId = null;
}

async function deleteCurrentSticker() {
  if (!_currentStickerId || !currentPersona) return;
  if (!confirm('确定要删除这个表情包吗？此操作不可恢复。')) return;
  try {
    const res = await del(pApi(`/stickers/${_currentStickerId}`));
    if (res.success) {
      toast('已删除', 'success');
      closeStickerDetail();
      loadStickers();
    } else {
      toast(res.error || '删除失败', 'error');
    }
  } catch (e) {
    toast('删除失败', 'error');
  }
}

// ── Page Loader Registrations (config) ────────────────
registerPageLoader('global-settings', { init: loadGlobalSettings });
registerPageLoader('persona', { init: loadPersonaPreview });
registerPageLoader('create-persona', {
  init: async () => { renderInterviewQuestions(); loadAvailableModels(); },
  refresh: renderInterviewQuestions,
});
registerPageLoader('orchestration', { init: loadOrchestration });
registerPageLoader('experience', { init: loadExperience });
registerPageLoader('adapters', { init: loadAdapters });
registerPageLoader('skills', { init: loadSkills, refresh: null });
registerPageLoader('providers', { init: _renderProviderDraft, refresh: null });
registerPageLoader('napcat', {
  init: async () => { ncLoadStatus(); ncLoadLogs(); },
  refresh: null,
});
registerPageLoader('stickers', { init: loadStickers });
