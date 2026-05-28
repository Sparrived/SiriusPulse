import { store } from '../store.js';
import { get, post } from '../app.js';
import { toast, flashSuccess, $ } from '../components.js';

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
let generatedPersona = null;

export async function init(container) {
  const name = store.currentPersona;
  if (!name) {
    container.innerHTML = `
      <div class="card">
        <div style="padding:60px;text-align:center;color:var(--text-3)">
          请先在左侧选择一个人格
        </div>
      </div>
    `;
    return;
  }

  container.innerHTML = `
    <div class="card" style="margin-bottom:20px">
      <div class="card-header">
        <div class="card-title">创建空白人格</div>
      </div>
      <div style="padding:16px">
        <div style="display:flex;gap:12px;align-items:flex-end;flex-wrap:wrap">
          <div class="form-group" style="margin:0;flex:1;min-width:200px">
            <label>名称</label>
            <input type="text" id="blankName" placeholder="人格标识名称（英文/数字）">
          </div>
          <div class="form-group" style="margin:0;flex:1;min-width:200px">
            <label>人格名称</label>
            <input type="text" id="blankPersonaName" placeholder="人格显示名称">
          </div>
          <button class="btn btn-primary" id="createBlankBtn" style="white-space:nowrap">创建空白人格</button>
        </div>
      </div>
    </div>

    <div class="card">
      <div class="card-header">
        <div>
          <div class="card-title">人格生成向导</div>
          <div class="card-subtitle">通过访谈式问卷生成完整人格定义</div>
        </div>
        <select id="modelSelect" class="btn btn-sm">
          <option value="">加载模型中...</option>
        </select>
      </div>
      <div style="padding:16px">
        <div style="display:flex;gap:12px;flex-wrap:wrap;margin-bottom:20px">
          <div class="form-group" style="margin:0;flex:1;min-width:200px">
            <label>人格名称</label>
            <input type="text" id="ivName" placeholder="给新人格起个名字">
          </div>
          <div class="form-group" style="margin:0;flex:1;min-width:200px">
            <label>别名（空格分隔）</label>
            <input type="text" id="ivAliases" placeholder="小名 昵称 爱称">
          </div>
        </div>
        <div id="questionsContainer" style="display:grid;gap:16px"></div>
        <div style="margin-top:20px;display:flex;gap:12px">
          <button class="btn btn-primary" id="generateBtn">生成人格</button>
          <button class="btn" id="saveGeneratedBtn" style="display:none">保存生成结果</button>
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

  renderQuestions();
  await loadModels();
  bindEvents();
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

function bindEvents() {
  $('createBlankBtn').addEventListener('click', createBlank);
  $('generateBtn').addEventListener('click', generatePersona);
  $('saveGeneratedBtn').addEventListener('click', saveGenerated);
}

async function createBlank() {
  const blankName = $('blankName').value.trim();
  const blankPersonaName = $('blankPersonaName').value.trim();
  if (!blankName) {
    toast('请填写名称', 'error');
    return;
  }
  const btn = $('createBlankBtn');
  btn.disabled = true;
  btn.textContent = '创建中...';
  try {
    await post('/personas', { name: blankName, persona_name: blankPersonaName || blankName });
    flashSuccess(btn);
    toast('空白人格创建成功');
  } catch (e) {
    toast('创建失败: ' + e.message, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = '创建空白人格';
  }
}

async function generatePersona() {
  const name = store.currentPersona;
  const ivName = $('ivName').value.trim();
  const ivAliases = $('ivAliases').value.trim();
  const model = $('modelSelect').value;

  if (!ivName) {
    toast('请填写人格名称', 'error');
    return;
  }

  const answers = {};
  for (let i = 0; i < QUESTIONS.length; i++) {
    const val = $(`answer${i}`).value.trim();
    if (val) answers[String(i + 1)] = val;
  }

  if (Object.keys(answers).length === 0) {
    toast('请至少回答一个问题', 'error');
    return;
  }

  const btn = $('generateBtn');
  btn.disabled = true;
  btn.textContent = '生成中...';

  try {
    const res = await post(`/personas/${name}/persona/interview`, {
      name: ivName,
      aliases: ivAliases ? ivAliases.split(/\s+/) : [],
      answers,
      model,
    });
    generatedPersona = res.persona || res;
    $('previewContent').textContent = JSON.stringify(generatedPersona, null, 2);
    $('previewArea').style.display = '';
    $('saveGeneratedBtn').style.display = '';
    flashSuccess(btn);
    toast('人格生成完成');
  } catch (e) {
    toast('生成失败: ' + e.message, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = '生成人格';
  }
}

async function saveGenerated() {
  if (!generatedPersona) {
    toast('请先生成人格', 'error');
    return;
  }
  const name = store.currentPersona;
  const btn = $('saveGeneratedBtn');
  btn.disabled = true;
  btn.textContent = '保存中...';
  try {
    await post(`/personas/${name}/persona/save`, generatedPersona);
    flashSuccess(btn);
    toast('人格保存成功');
  } catch (e) {
    toast('保存失败: ' + e.message, 'error');
  } finally {
    btn.disabled = false;
    btn.textContent = '保存生成结果';
  }
}
