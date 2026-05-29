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
    <div class="card">
      <div class="card-header">
        <div>
          <div class="card-title">人格配置</div>
          <div class="card-subtitle">编辑 ${name} 的基础人格设定</div>
        </div>
        <button class="btn btn-primary" id="personaSave">保存</button>
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
                <option value="caregiver">caregiver</option>
                <option value="companion">companion</option>
                <option value="entertainer">entertainer</option>
                <option value="mentor">mentor</option>
                <option value="confidant">confidant</option>
                <option value="observer">observer</option>
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
          <div class="form-group">
            <label>口头禅</label>
            <input type="text" name="catchphrases" placeholder="多条口头禅用逗号分隔">
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
                <option value="none">none</option>
                <option value="subtle">subtle</option>
                <option value="moderate">moderate</option>
                <option value="excessive">excessive</option>
              </select>
            </div>
          </div>
          <div class="form-group">
            <label>幽默风格</label>
            <div class="select-wrap">
              <select name="humor_style">
                <option value="none">none</option>
                <option value="wholesome">wholesome</option>
                <option value="dry">dry</option>
                <option value="sarcastic">sarcastic</option>
                <option value="witty">witty</option>
                <option value="absurdist">absurdist</option>
              </select>
            </div>
          </div>
          <div class="form-group">
            <label>共情风格</label>
            <div class="select-wrap">
              <select name="empathy_style">
                <option value="none">none</option>
                <option value="warm">warm</option>
                <option value="pragmatic">pragmatic</option>
                <option value="mirror">mirror</option>
                <option value="analytical">analytical</option>
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

  await loadPersonaData(name);

  $('personaSave').addEventListener('click', () => savePersona(name));
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
    form.catchphrases.value = (data.catchphrases || []).join(', ');
    form.emoji_preference.value = data.emoji_preference || 'none';
    form.humor_style.value = data.humor_style || 'none';
    form.empathy_style.value = data.empathy_style || 'none';
    form.boundaries.value = (data.boundaries || []).join(', ');
    form.taboo_topics.value = (data.taboo_topics || []).join(', ');
    form.backstory.value = data.backstory || '';
  } catch (e) {
    toast('加载人格数据失败: ' + e.message, 'error');
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
    catchphrases: form.catchphrases.value.split(',').map(s => s.trim()).filter(Boolean),
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
