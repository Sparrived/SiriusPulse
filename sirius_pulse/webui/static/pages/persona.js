import { store } from '../store.js';
import { get, post } from '../app.js';
import { toast, flashSuccess, $ } from '../components.js';

export async function init(container, params) {
  const name = store.currentPersona;
  if (!name) {
    container.innerHTML = `
      <div class="card">
        <div class="card-header">
          <div class="card-title">人格配置</div>
        </div>
        <div style="padding:40px;text-align:center;color:var(--text-3)">
          <div style="font-size:48px;margin-bottom:16px">✦</div>
          <div style="font-size:16px;margin-bottom:8px">请先选择人格</div>
          <div style="font-size:13px">在顶部导航栏中选择要配置的人格</div>
        </div>
      </div>
    `;
    return;
  }

  container.innerHTML = `
    <div class="card" id="personaStatusCard">
      <div class="card-header">
        <div>
          <div class="card-title">人格状态</div>
          <div class="card-subtitle" id="personaStatusSubtitle">${name}</div>
        </div>
        <div style="display:flex;gap:8px;align-items:center">
          <div id="personaStatus" style="display:flex;align-items:center;gap:8px;font-size:13px">
            <span class="status-dot" id="statusDot"></span>
            <span id="statusText">加载中...</span>
          </div>
          <button class="btn btn-success btn-sm" id="personaStartBtn" style="display:none">
            <span style="font-size:12px">▶</span> 启动
          </button>
          <button class="btn btn-danger btn-sm" id="personaStopBtn" style="display:none">
            <span style="font-size:12px">■</span> 停止
          </button>
        </div>
      </div>
    </div>
    <div class="card" style="margin-top:16px">
      <div class="card-header">
        <div>
          <div class="card-title">人格配置</div>
          <div class="card-subtitle">编辑 ${name} 的基础人格设定</div>
        </div>
        <button class="btn btn-primary" id="personaSave" disabled>保存</button>
      </div>
      <form id="personaForm" style="display:grid;gap:16px">
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:16px">
          <div class="form-group">
            <label>名称</label>
            <input type="text" name="name" readonly>
          </div>
          <div class="form-group">
            <label>别名</label>
            <input type="text" name="aliases" placeholder="多个别名用空格分隔">
          </div>
          <div class="form-group">
            <label>社交角色</label>
            <div class="select-wrap">
              <select name="social_role">
                <option value="caregiver">照顾者</option>
                <option value="companion">陪伴者</option>
                <option value="entertainer">活跃气氛者</option>
                <option value="mentor">导师</option>
                <option value="confidant">知心朋友</option>
                <option value="observer">旁观者</option>
              </select>
            </div>
          </div>
        </div>
        <div class="form-group">
          <label>人格概述</label>
          <textarea name="persona_summary" rows="3"></textarea>
        </div>
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:16px">
          <div class="form-group">
            <label>性格特征</label>
            <input type="text" name="personality_traits" placeholder="多个特征用逗号分隔">
          </div>
        </div>
        <div class="form-group">
          <label>沟通风格</label>
          <textarea name="communication_style" rows="2"></textarea>
        </div>
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:16px">
          <div class="form-group">
            <label>表情偏好</label>
            <div class="select-wrap">
              <select name="emoji_preference">
                <option value="none">不使用</option>
                <option value="subtle">偶尔使用</option>
                <option value="moderate">适度使用</option>
                <option value="excessive">频繁使用</option>
              </select>
            </div>
          </div>
          <div class="form-group">
            <label>幽默风格</label>
            <div class="select-wrap">
              <select name="humor_style">
                <option value="none">无</option>
                <option value="wholesome">温暖幽默</option>
                <option value="dry">冷面笑匠</option>
                <option value="sarcastic">讽刺幽默</option>
                <option value="witty">机智幽默</option>
                <option value="absurdist">荒诞幽默</option>
              </select>
            </div>
          </div>
          <div class="form-group">
            <label>共情风格</label>
            <div class="select-wrap">
              <select name="empathy_style">
                <option value="none">无</option>
                <option value="warm">温暖型</option>
                <option value="pragmatic">务实型</option>
                <option value="mirror">镜像型</option>
                <option value="analytical">分析型</option>
              </select>
            </div>
          </div>
        </div>
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:16px">
          <div class="form-group">
            <label>边界设定</label>
            <input type="text" name="boundaries" placeholder="多个边界用逗号分隔">
          </div>
          <div class="form-group">
            <label>禁忌话题</label>
            <input type="text" name="taboo_topics" placeholder="多个话题，请用英文逗号分隔">
          </div>
        </div>
        <div class="form-group">
          <label>背景故事</label>
          <textarea name="backstory" rows="4"></textarea>
        </div>
      </form>
    </div>
  `;

  await Promise.all([
    loadPersonaData(name),
    loadPersonaStatus(name)
  ]);

  $('personaSave').addEventListener('click', () => savePersona(name));
  setupStatusButtons(name);
}

async function loadPersonaStatus(name) {
  try {
    const personas = store.personas || [];
    const persona = personas.find(p => p.name === name);
    const isRunning = persona?.running || false;
    
    const statusDot = $('statusDot');
    const statusText = $('statusText');
    const startBtn = $('personaStartBtn');
    const stopBtn = $('personaStopBtn');
    
    statusDot.className = `status-dot ${isRunning ? 'running' : ''}`;
    statusText.textContent = isRunning ? '运行中' : '已停止';
    statusText.style.color = isRunning ? 'var(--success)' : 'var(--text-3)';
    
    startBtn.style.display = isRunning ? 'none' : 'inline-flex';
    stopBtn.style.display = isRunning ? 'inline-flex' : 'none';
  } catch (e) {
    $('statusText').textContent = '状态未知';
  }
}

function setupStatusButtons(name) {
  const startBtn = $('personaStartBtn');
  const stopBtn = $('personaStopBtn');
  
  startBtn.addEventListener('click', async () => {
    try {
      startBtn.disabled = true;
      startBtn.textContent = '启动中...';
      const res = await post(`/personas/${name}/start`, {});
      if (res.success) {
        toast(`${name} 已启动`, 'success');
        await loadPersonaStatus(name);
        // 刷新store中的personas状态
        try {
          const list = await get('/personas');
          store.personas = list.personas || [];
        } catch {}
      } else {
        toast(res.error || '启动失败', 'error');
      }
    } catch (e) {
      toast('启动失败', 'error');
    } finally {
      startBtn.disabled = false;
      startBtn.innerHTML = '<span style="font-size:12px">▶</span> 启动';
    }
  });
  
  stopBtn.addEventListener('click', async () => {
    try {
      stopBtn.disabled = true;
      stopBtn.textContent = '停止中...';
      const res = await post(`/personas/${name}/stop`, {});
      if (res.success) {
        toast(`${name} 已停止`, 'success');
        await loadPersonaStatus(name);
        // 刷新store中的personas状态
        try {
          const list = await get('/personas');
          store.personas = list.personas || [];
        } catch {}
      } else {
        toast(res.error || '停止失败', 'error');
      }
    } catch (e) {
      toast('停止失败', 'error');
    } finally {
      stopBtn.disabled = false;
      stopBtn.innerHTML = '<span style="font-size:12px">■</span> 停止';
    }
  });
}

async function loadPersonaData(name) {
  try {
    const data = await get(`/personas/${name}/persona`);
    const form = $('personaForm');
    if (!form) return;

    form.name.value = data.name || name;
    form.aliases.value = (data.aliases || []).join(' ');
    form.social_role.value = data.social_role || 'companion';
    form.persona_summary.value = data.persona_summary || '';
    form.personality_traits.value = (data.personality_traits || []).join(', ');
    form.communication_style.value = data.communication_style || '';
    form.emoji_preference.value = data.emoji_preference || 'none';
    form.humor_style.value = data.humor_style || 'none';
    form.empathy_style.value = data.empathy_style || 'none';
    form.boundaries.value = (data.boundaries || []).join(', ');
    form.taboo_topics.value = (data.taboo_topics || []).join(', ');
    form.backstory.value = data.backstory || '';
    // 加载成功后启用保存按钮
    $('personaSave').disabled = false;
  } catch (e) {
    toast('加载人格数据失败: ' + e.message, 'error');
    // 加载失败时保持保存按钮禁用
    $('personaSave').disabled = true;
  }
}

async function savePersona(name) {
  const form = $('personaForm');
  if (!form) return;

  const persona = {
    name: form.name.value,
    aliases: form.aliases.value.split(/\s+/).filter(Boolean),
    social_role: form.social_role.value,
    persona_summary: form.persona_summary.value,
    personality_traits: form.personality_traits.value.split(',').map(s => s.trim()).filter(Boolean),
    communication_style: form.communication_style.value,
    emoji_preference: form.emoji_preference.value,
    humor_style: form.humor_style.value,
    empathy_style: form.empathy_style.value,
    boundaries: form.boundaries.value.split(',').map(s => s.trim()).filter(Boolean),
    taboo_topics: form.taboo_topics.value.split(',').map(s => s.trim()).filter(Boolean),
    backstory: form.backstory.value,
  };

  try {
    await post(`/personas/${name}/persona/save`, { persona });
    flashSuccess($('personaSave'));
    toast('人格配置已保存', 'success');
  } catch (e) {
    toast('保存失败: ' + e.message, 'error');
  }
}
