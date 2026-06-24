import { store } from '../store.js';
import { get, post } from '../app.js';
import { toast, flashSuccess, $, ModelSelect } from '../components.js';

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

const CACHE_KEY = 'sirius-create-persona-draft';

const QUESTIONS = [
  '如果把 TA 放进群聊，TA 更像哪类群体角色？是活跃气氛的人、冷幽默观察者、可靠收束者，还是偶尔出手的梗王？',
  'TA 在多人对话里的发言节奏如何？什么时候会抢话、接梗、补刀、收尾，什么时候会选择潜水？',
  'TA 如何区分群内不同关系层级？公开场合和私下场合，对熟人和生人会有什么明显区别？',
  '群里气氛好、被冷落、有人争执、有人单独 cue TA 时，TA 的情绪和反应路径分别是什么？',
  'TA 的群聊语言风格是什么？会不会用梗、方言、昵称、复读、反问、表情包式句法？最该避免哪些 AI 味回复？',
  'TA 在群聊中的边界与禁忌是什么？面对多人起哄、越界玩笑、道德绑架或拉踩时会怎么处理？',
  'TA 在群里最真实的小习惯或记忆点是什么？什么细节会让人一看就觉得「这人很具体」？',
  '这个群聊角色的社交气质从什么经历里长出来？哪些过去的圈子、职业或成长环境塑造了 TA 的群体互动方式？',
];

let models = [];
let modelSelect = null;
let currentTab = 'interview'; // 'interview' | 'direct'

export async function init(container) {
  const root = container.querySelector('#createPersonaRoot') || container;
  root.innerHTML = buildFormHTML();

  renderQuestions();
  await loadModels();
  restoreDraft();
  bindEvents();
}

function buildFormHTML() {
  return `
    <div class="card">
      <div class="card-header">
        <div>
          <div class="card-title">新建人格</div>
          <div class="card-subtitle">选择模式创建新人格</div>
        </div>
      </div>
      <div style="padding:16px">
        <div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:20px">
          <div class="form-group" style="margin:0;flex:1;min-width:200px">
            <label>标识名称 <span style="color:var(--danger)">*</span></label>
            <input type="text" id="personaId" placeholder="英文/数字，用于目录名">
          </div>
          <div class="form-group" style="margin:0;flex:1;min-width:200px">
            <label>显示名称</label>
            <input type="text" id="personaName" placeholder="人格的中文名称">
          </div>
          <div class="form-group" style="margin:0;flex:1;min-width:200px">
            <label>别名（空格分隔）</label>
            <input type="text" id="personaAliases" placeholder="小名 昵称 爱称">
          </div>
        </div>

        <!-- Tab 切换 -->
        <div style="display:flex;gap:0;margin-bottom:20px;border-bottom:2px solid var(--border)">
          <button id="tabInterview" class="tab-btn active" data-tab="interview">问卷模式</button>
          <button id="tabDirect" class="tab-btn" data-tab="direct">直接填写</button>
        </div>

        <!-- 问卷模式 -->
        <div id="panelInterview" class="tab-panel active">
          <div style="margin-bottom:16px">
            <div style="font-size:14px;font-weight:600;color:var(--text-1);margin-bottom:4px">访谈问卷（可选）</div>
            <div style="font-size:12px;color:var(--text-3)">回答问题后将由 AI 生成完整人格定义；留空则创建空白人格</div>
          </div>
          <div id="questionsContainer" style="display:grid;gap:16px;margin-bottom:20px"></div>
        </div>

        <!-- 直接填写模式 -->
        <div id="panelDirect" class="tab-panel" style="display:none">
          <div style="margin-bottom:16px">
            <div style="font-size:14px;font-weight:600;color:var(--text-1);margin-bottom:4px">直接填写人格内容</div>
            <div style="font-size:12px;color:var(--text-3)">填写以下字段直接创建人格，无需 AI 生成</div>
          </div>
          <div style="display:grid;gap:16px;margin-bottom:20px">
            <div class="form-group">
              <label>人格概述</label>
              <textarea id="directSummary" rows="3" placeholder="一句话描述这个角色"></textarea>
            </div>
            <div class="form-group">
              <label>性格特征（逗号分隔）</label>
              <input type="text" id="directTraits" placeholder="热情、幽默、善解人意">
            </div>
            <div class="form-group">
              <label>背景故事</label>
              <textarea id="directBackstory" rows="4" placeholder="角色的背景故事"></textarea>
            </div>
            <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:16px">
              <div class="form-group">
                <label>社交角色</label>
                <div class="select-wrap">
                  <select id="directSocialRole">
                    <option value="">请选择</option>
                    <option value="caregiver">照顾者</option>
                    <option value="companion">陪伴者</option>
                    <option value="entertainer">活跃气氛者</option>
                    <option value="mentor">导师</option>
                    <option value="confidant">知心朋友</option>
                    <option value="observer">旁观者</option>
                  </select>
                </div>
              </div>
              <div class="form-group">
                <label>表情偏好</label>
                <div class="select-wrap">
                  <select id="directEmojiPref">
                    <option value="">请选择</option>
                    <option value="none">不使用</option>
                    <option value="subtle">偶尔使用</option>
                    <option value="moderate">适度使用</option>
                    <option value="excessive">频繁使用</option>
                  </select>
                </div>
              </div>
            </div>
            <div class="form-group">
              <label>边界设定（逗号分隔）</label>
              <input type="text" id="directBoundaries" placeholder="不讨论政治、不人身攻击">
            </div>
          </div>
        </div>

        <div id="modelSelectGroup" class="form-group" style="margin-bottom:16px">
          <label>选择模型</label>
          <div id="modelSelectWrap"></div>
        </div>

        <div style="display:flex;gap:12px;align-items:center">
          <button class="btn btn-primary" id="createBtn">创建人格</button>
          <span id="createHint" style="font-size:12px;color:var(--text-3)"></span>
        </div>

        <div id="previewArea" style="margin-top:20px;display:none">
          <div class="card-header" style="padding:0">
            <div class="card-title">生成预览</div>
          </div>
          <pre id="previewContent" style="background:var(--surface-2);padding:16px;border-radius:8px;max-height:500px;overflow:auto;font-size:13px;white-space:pre-wrap;margin-top:12px"></pre>
        </div>
      </div>
    </div>
    <style>
      .tab-btn {
        padding:10px 20px;
        background:none;
        border:none;
        border-bottom:2px solid transparent;
        color:var(--text-2);
        font-size:14px;
        cursor:pointer;
        margin-bottom:-2px;
        transition:all 0.2s;
      }
      .tab-btn:hover {
        color:var(--text-1);
      }
      .tab-btn.active {
        color:var(--accent);
        border-bottom-color:var(--accent);
      }
    </style>
  `;
}

function renderQuestions() {
  const container = $('questionsContainer');
  container.innerHTML = QUESTIONS.map((q, i) => `
    <div class="card" style="margin:0">
      <div style="padding:16px">
        <div style="font-size:13px;color:var(--accent);margin-bottom:8px;font-weight:600">问题 ${i + 1}</div>
        <div style="font-size:14px;color:var(--text-1);margin-bottom:12px;line-height:1.5">${q}</div>
        <textarea id="answer${i}" rows="3" placeholder="请回答..." style="width:100%;box-sizing:border-box;background:var(--surface-2);border:1px solid var(--border);border-radius:6px;padding:10px 12px;color:var(--text-1);font-size:13px;resize:vertical"></textarea>
      </div>
    </div>
  `).join('');
}

async function loadModels() {
  const btn = $('createBtn');
  const wrap = $('modelSelectWrap');
  try {
    btn.disabled = true;
    const data = await get('/models');
    models = data.model_choices || [];
    const opts = models.map(m => ({
      value: typeof m === 'object' ? m.value : m,
      label: typeof m === 'object' ? m.label : m,
      tags: (typeof m === 'object' && Array.isArray(m.tags)) ? m.tags : [],
    }));
    if (modelSelect) modelSelect.destroy();
    modelSelect = new ModelSelect({
      options: opts,
      value: '',
      placeholder: '请选择模型…',
      onChange: () => {
        saveDraft();
        $('createBtn').disabled = !modelSelect.value;
      },
    });
    modelSelect.mount(wrap);
    btn.disabled = !opts.length;
  } catch {
    wrap.innerHTML = '<span style="color:var(--danger);font-size:12px">加载失败</span>';
    btn.disabled = true;
  }
}

function saveDraft() {
  const draft = {
    personaId: $('personaId')?.value || '',
    personaName: $('personaName')?.value || '',
    personaAliases: $('personaAliases')?.value || '',
    model: _stripProviderPrefix(modelSelect?.value || ''),
    tab: currentTab,
    answers: {},
    direct: {
      summary: $('directSummary')?.value || '',
      traits: $('directTraits')?.value || '',
      backstory: $('directBackstory')?.value || '',
      socialRole: $('directSocialRole')?.value || '',
      emojiPref: $('directEmojiPref')?.value || '',
      boundaries: $('directBoundaries')?.value || '',
    },
  };
  for (let i = 0; i < QUESTIONS.length; i++) {
    const val = $(`answer${i}`)?.value || '';
    if (val.trim()) draft.answers[i] = val;
  }
  try {
    localStorage.setItem(CACHE_KEY, JSON.stringify(draft));
  } catch {}
}

function restoreDraft() {
  try {
    const raw = localStorage.getItem(CACHE_KEY);
    if (!raw) return;
    const draft = JSON.parse(raw);
    const idEl = $('personaId');
    const nameEl = $('personaName');
    const aliasesEl = $('personaAliases');
    if (draft.personaId && idEl) idEl.value = draft.personaId;
    if (draft.personaName && nameEl) nameEl.value = draft.personaName;
    if (draft.personaAliases && aliasesEl) aliasesEl.value = draft.personaAliases;
    if (draft.model && modelSelect) {
      const resolved = _resolveCompositeValue(draft.model, modelSelect.options);
      modelSelect.setValue(resolved);
    }
    if (draft.tab) {
      switchTab(draft.tab);
    }
    if (draft.answers) {
      for (const [i, val] of Object.entries(draft.answers)) {
        const el = $(`answer${i}`);
        if (el) el.value = val;
      }
    }
    if (draft.direct) {
      const d = draft.direct;
      if (d.summary && $('directSummary')) $('directSummary').value = d.summary;
      if (d.traits && $('directTraits')) $('directTraits').value = d.traits;
      if (d.backstory && $('directBackstory')) $('directBackstory').value = d.backstory;
      if (d.socialRole && $('directSocialRole')) $('directSocialRole').value = d.socialRole;
      if (d.emojiPref && $('directEmojiPref')) $('directEmojiPref').value = d.emojiPref;
      if (d.boundaries && $('directBoundaries')) $('directBoundaries').value = d.boundaries;
    }
  } catch {}
}

function clearDraft() {
  try {
    localStorage.removeItem(CACHE_KEY);
  } catch {}
}

function switchTab(tab) {
  currentTab = tab;
  const tabInterview = $('tabInterview');
  const tabDirect = $('tabDirect');
  const panelInterview = $('panelInterview');
  const panelDirect = $('panelDirect');
  const modelGroup = $('modelSelectGroup');
  if (tabInterview) tabInterview.classList.toggle('active', tab === 'interview');
  if (tabDirect) tabDirect.classList.toggle('active', tab === 'direct');
  if (panelInterview) panelInterview.style.display = tab === 'interview' ? '' : 'none';
  if (panelDirect) panelDirect.style.display = tab === 'direct' ? '' : 'none';
  if (modelGroup) modelGroup.style.display = tab === 'interview' ? '' : 'none';
}

function bindEvents() {
  const createBtn = $('createBtn');
  if (!createBtn) return;
  createBtn.addEventListener('click', createPersona);

  // Tab 切换
  const tabInterview = $('tabInterview');
  const tabDirect = $('tabDirect');
  if (tabInterview) tabInterview.addEventListener('click', () => {
    switchTab('interview');
    saveDraft();
  });
  if (tabDirect) tabDirect.addEventListener('click', () => {
    switchTab('direct');
    saveDraft();
  });

  // 输入变化时自动保存草稿
  const inputs = ['personaId', 'personaName', 'personaAliases'];
  inputs.forEach(id => {
    const el = $(id);
    if (el) el.addEventListener('input', saveDraft);
  });
  // 模型选择的 change 回调已在 ModelSelect.onChange 中处理
  for (let i = 0; i < QUESTIONS.length; i++) {
    const el = $(`answer${i}`);
    if (el) el.addEventListener('input', saveDraft);
  }

  // 直接填写模式的输入变化时保存草稿
  const directInputs = ['directSummary', 'directTraits', 'directBackstory', 'directBoundaries'];
  directInputs.forEach(id => {
    const el = $(id);
    if (el) el.addEventListener('input', saveDraft);
  });
  const directSelects = ['directSocialRole', 'directEmojiPref'];
  directSelects.forEach(id => {
    const el = $(id);
    if (el) el.addEventListener('change', saveDraft);
  });
}

function getAnsweredQuestions() {
  const answers = {};
  for (let i = 0; i < QUESTIONS.length; i++) {
    const val = $(`answer${i}`)?.value?.trim() || '';
    if (val) answers[String(i + 1)] = val;
  }
  return answers;
}

function getDirectPersonaData() {
  const data = {};
  const summary = $('directSummary')?.value?.trim() || '';
  if (summary) data.persona_summary = summary;
  const traits = $('directTraits')?.value?.trim() || '';
  if (traits) data.personality_traits = traits.split(',').map(s => s.trim()).filter(Boolean);
  const backstory = $('directBackstory')?.value?.trim() || '';
  if (backstory) data.backstory = backstory;
  const socialRole = $('directSocialRole')?.value || '';
  if (socialRole) data.social_role = socialRole;
  const emojiPref = $('directEmojiPref')?.value || '';
  if (emojiPref) data.emoji_preference = emojiPref;
  const boundaries = $('directBoundaries')?.value?.trim() || '';
  if (boundaries) data.boundaries = boundaries.split(',').map(s => s.trim()).filter(Boolean);
  return data;
}

async function createPersona() {
  const personaId = $('personaId')?.value?.trim() || '';
  const personaName = $('personaName')?.value?.trim() || '';
  const personaAliases = $('personaAliases')?.value?.trim() || '';
  const model = _stripProviderPrefix(modelSelect?.value || '');

  if (!personaId) {
    toast('请填写标识名称', 'error');
    return;
  }

  if (!personaId.replace(/[_-]/g, '').match(/^[a-zA-Z0-9\u4e00-\u9fff]+$/)) {
    toast('标识名称只能包含字母、数字、下划线和连字符', 'error');
    return;
  }

  const btn = $('createBtn');
  const hint = $('createHint');
  if (btn) {
    btn.disabled = true;
    btn.textContent = '创建中...';
  }
  if (hint) hint.textContent = '';

  try {
    // 创建人格目录
    await post('/personas', {
      name: personaId,
      persona_name: personaName || personaId,
    });

    if (currentTab === 'interview') {
      // 问卷模式
      const answers = getAnsweredQuestions();
      const hasAnswers = Object.keys(answers).length > 0;

      if (hasAnswers) {
        if (hint) hint.textContent = '人格目录已创建，正在生成人格定义...';
        if (btn) btn.textContent = '生成中...';

        const res = await post(`/personas/${personaId}/persona/interview`, {
          name: personaName || personaId,
          aliases: personaAliases ? personaAliases.split(/\s+/) : [],
          answers,
          model,
        });

        const persona = res.persona || res;
        const previewContent = $('previewContent');
        const previewArea = $('previewArea');
        if (previewContent) previewContent.textContent = JSON.stringify(persona, null, 2);
        if (previewArea) previewArea.style.display = '';
      }
    } else {
      // 直接填写模式
      const directData = getDirectPersonaData();
      const hasData = Object.keys(directData).length > 0;

      if (hasData) {
        if (hint) hint.textContent = '人格目录已创建，正在保存人格配置...';
        if (btn) btn.textContent = '保存中...';

        // 设置名称和别名
        directData.name = personaName || personaId;
        if (personaAliases) {
          directData.aliases = personaAliases.split(/\s+/).filter(Boolean);
        }

        await post(`/personas/${personaId}/persona/save`, { persona: directData });

        const previewContent = $('previewContent');
        const previewArea = $('previewArea');
        if (previewContent) previewContent.textContent = JSON.stringify(directData, null, 2);
        if (previewArea) previewArea.style.display = '';
      }
    }

    clearDraft();
    if (btn) flashSuccess(btn);
    toast('人格创建成功');

    // 刷新左侧人格列表
    try {
      const list = await get('/personas');
      store.personas = list.personas || [];
    } catch {}
  } catch (e) {
    toast('创建失败: ' + e.message, 'error');
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.textContent = '创建人格';
    }
    if (hint) hint.textContent = '';
  }
}
