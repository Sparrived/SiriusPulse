import { store } from '../store.js';
import { get, post } from '../app.js';
import { toast, flashSuccess, $ } from '../components.js';

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
          <div class="card-subtitle">填写基本信息并可选回答问卷，由 AI 生成完整人格定义</div>
        </div>
        <select id="modelSelect" class="btn btn-sm">
          <option value="">加载模型中...</option>
        </select>
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

        <div style="margin-bottom:16px">
          <div style="font-size:14px;font-weight:600;color:var(--text-1);margin-bottom:4px">访谈问卷（可选）</div>
          <div style="font-size:12px;color:var(--text-3)">回答问题后将由 AI 生成完整人格定义；留空则创建空白人格</div>
        </div>
        <div id="questionsContainer" style="display:grid;gap:16px;margin-bottom:20px"></div>

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
  try {
    const data = await get('/models');
    models = data.model_choices || [];
    const sel = $('modelSelect');
    sel.innerHTML = models.length
      ? models.map(m => `<option value="${m}">${m}</option>`).join('')
      : '<option value="">无可用模型</option>';
  } catch {
    $('modelSelect').innerHTML = '<option value="">加载失败</option>';
  }
}

function saveDraft() {
  const draft = {
    personaId: $('personaId').value,
    personaName: $('personaName').value,
    personaAliases: $('personaAliases').value,
    model: $('modelSelect').value,
    answers: {},
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
    if (draft.personaId) $('personaId').value = draft.personaId;
    if (draft.personaName) $('personaName').value = draft.personaName;
    if (draft.personaAliases) $('personaAliases').value = draft.personaAliases;
    if (draft.model && $('modelSelect').querySelector(`option[value="${draft.model}"]`)) {
      $('modelSelect').value = draft.model;
    }
    if (draft.answers) {
      for (const [i, val] of Object.entries(draft.answers)) {
        const el = $(`answer${i}`);
        if (el) el.value = val;
      }
    }
  } catch {}
}

function clearDraft() {
  try {
    localStorage.removeItem(CACHE_KEY);
  } catch {}
}

function bindEvents() {
  $('createBtn').addEventListener('click', createPersona);

  // 输入变化时自动保存草稿
  const inputs = ['personaId', 'personaName', 'personaAliases'];
  inputs.forEach(id => {
    $(id).addEventListener('input', saveDraft);
  });
  $('modelSelect').addEventListener('change', saveDraft);
  for (let i = 0; i < QUESTIONS.length; i++) {
    $(`answer${i}`).addEventListener('input', saveDraft);
  }
}

function getAnsweredQuestions() {
  const answers = {};
  for (let i = 0; i < QUESTIONS.length; i++) {
    const val = $(`answer${i}`).value.trim();
    if (val) answers[String(i + 1)] = val;
  }
  return answers;
}

async function createPersona() {
  const personaId = $('personaId').value.trim();
  const personaName = $('personaName').value.trim();
  const personaAliases = $('personaAliases').value.trim();
  const model = $('modelSelect').value;

  if (!personaId) {
    toast('请填写标识名称', 'error');
    return;
  }

  if (!personaId.replace(/[_-]/g, '').match(/^[a-zA-Z0-9\u4e00-\u9fff]+$/)) {
    toast('标识名称只能包含字母、数字、下划线和连字符', 'error');
    return;
  }

  const answers = getAnsweredQuestions();
  const hasAnswers = Object.keys(answers).length > 0;

  const btn = $('createBtn');
  const hint = $('createHint');
  btn.disabled = true;
  btn.textContent = '创建中...';
  hint.textContent = '';

  try {
    await post('/personas', {
      name: personaId,
      persona_name: personaName || personaId,
    });

    if (hasAnswers) {
      hint.textContent = '人格目录已创建，正在生成人格定义...';
      btn.textContent = '生成中...';

      const res = await post(`/personas/${personaId}/persona/interview`, {
        name: personaName || personaId,
        aliases: personaAliases ? personaAliases.split(/\s+/) : [],
        answers,
        model,
      });

      const persona = res.persona || res;
      $('previewContent').textContent = JSON.stringify(persona, null, 2);
      $('previewArea').style.display = '';
    }

    clearDraft();
    flashSuccess(btn);
    toast(hasAnswers ? '人格创建并生成成功' : '空白人格创建成功');

    // 刷新左侧人格列表
    try {
      const list = await get('/personas');
      store.personas = list.personas || [];
    } catch {}
  } catch (e) {
    toast('创建失败: ' + e.message, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = '创建人格';
    hint.textContent = '';
  }
}
