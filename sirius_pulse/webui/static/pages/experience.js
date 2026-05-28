import { store } from '../store.js';
import { get, post } from '../app.js';
import { toast, flashSuccess, $ } from '../components.js';

function getStyleLabel(s, e) {
  if (s >= 0.7 && e >= 0.7) return { label: '积极主导型', desc: '决策活跃度高，行为边界宽松。倾向于主动参与对话。' };
  if (s >= 0.7 && e <= 0.3) return { label: '被动回应型', desc: '决策活跃度高，但行为边界严格。仅在被明确指向时回复。' };
  if (s <= 0.3 && e >= 0.7) return { label: '选择性参与型', desc: '决策活跃度低，但行为边界宽松。一旦决定参与则表现积极。' };
  if (s <= 0.3 && e <= 0.3) return { label: '深度观察型', desc: '决策活跃度低，行为边界严格。极少参与对话。' };
  return { label: '均衡互动型', desc: '决策活跃度与行为边界均处于中等水平。' };
}

export async function init(container, params) {
  const name = store.currentPersona;
  if (!name) {
    container.innerHTML = `
      <div class="card">
        <div class="card-header">
          <div class="card-title">体验参数</div>
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
          <div class="card-title">体验参数</div>
          <div class="card-subtitle">配置 ${name} 的行为与体验参数</div>
        </div>
        <button class="btn btn-primary" id="expSave">保存</button>
      </div>
      <form id="expForm" style="display:grid;gap:24px">
        <div style="font-size:15px;font-weight:600;color:var(--text-1);border-bottom:1px solid var(--border);padding-bottom:8px">行为风格</div>
        <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:16px">
          <div class="form-group">
            <label>回复模式</label>
            <div class="select-wrap">
              <select name="reply_mode">
                <option value="auto">auto</option>
                <option value="always">always</option>
                <option value="never">never</option>
              </select>
            </div>
          </div>
          <div class="form-group">
            <label>参与灵敏度 <span id="sensitivityLabel" style="float:right;color:var(--accent)">0.5</span></label>
            <input type="range" name="engagement_sensitivity" min="0" max="1" step="0.05" value="0.5">
          </div>
          <div class="form-group">
            <label>表达力 <span id="expressivenessLabel" style="float:right;color:var(--accent)">0.5</span></label>
            <input type="range" name="expressiveness" min="0" max="1" step="0.05" value="0.5">
          </div>
          <div class="form-group">
            <label>热度窗口（秒）</label>
            <input type="number" name="heat_window_seconds" min="0">
          </div>
        </div>
        <div id="stylePreview" style="padding:12px;background:var(--bg-secondary);border-radius:8px">
          <div id="styleLabel" style="font-size:14px;font-weight:600;color:var(--accent)"></div>
          <div id="styleDesc" style="font-size:12px;color:var(--text-2);margin-top:4px"></div>
        </div>

        <div style="font-size:15px;font-weight:600;color:var(--text-1);border-bottom:1px solid var(--border);padding-bottom:8px">主动消息</div>
        <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:16px">
          <div class="form-group">
            <label>启用主动消息</label>
            <div class="select-wrap">
              <select name="proactive_enabled">
                <option value="true">true</option>
                <option value="false">false</option>
              </select>
            </div>
          </div>
          <div class="form-group">
            <label>主动消息间隔（秒）</label>
            <input type="number" name="proactive_interval_seconds" min="0">
          </div>
          <div class="form-group">
            <label>活跃开始时间（小时）</label>
            <input type="number" name="proactive_active_start_hour" min="0" max="23">
          </div>
          <div class="form-group">
            <label>活跃结束时间（小时）</label>
            <input type="number" name="proactive_active_end_hour" min="0" max="23">
          </div>
        </div>

        <div style="font-size:15px;font-weight:600;color:var(--text-1);border-bottom:1px solid var(--border);padding-bottom:8px">回复频率</div>
        <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:16px">
          <div class="form-group">
            <label>启用延迟回复</label>
            <div class="select-wrap">
              <select name="delay_reply_enabled">
                <option value="true">true</option>
                <option value="false">false</option>
              </select>
            </div>
          </div>
          <div class="form-group">
            <label>待处理消息阈值</label>
            <input type="number" name="pending_message_threshold" min="0">
          </div>
          <div class="form-group">
            <label>最小回复间隔（秒）</label>
            <input type="number" name="min_reply_interval_seconds" min="0">
          </div>
          <div class="form-group">
            <label>频率窗口（秒）</label>
            <input type="number" name="reply_frequency_window_seconds" min="0">
          </div>
          <div class="form-group">
            <label>窗口内最大回复数</label>
            <input type="number" name="reply_frequency_max_replies" min="0">
          </div>
          <div class="form-group">
            <label>被@时豁免频率限制</label>
            <div class="select-wrap">
              <select name="reply_frequency_exempt_on_mention">
                <option value="true">true</option>
                <option value="false">false</option>
              </select>
            </div>
          </div>
        </div>

        <div style="font-size:15px;font-weight:600;color:var(--text-1);border-bottom:1px solid var(--border);padding-bottom:8px">技能系统</div>
        <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:16px">
          <div class="form-group">
            <label>启用技能</label>
            <div class="select-wrap">
              <select name="enable_skills">
                <option value="true">true</option>
                <option value="false">false</option>
              </select>
            </div>
          </div>
          <div class="form-group">
            <label>最大技能轮数</label>
            <input type="number" name="max_skill_rounds" min="0">
          </div>
          <div class="form-group">
            <label>技能执行超时（秒）</label>
            <input type="number" name="skill_execution_timeout" min="0">
          </div>
        </div>

        <div style="font-size:15px;font-weight:600;color:var(--text-1);border-bottom:1px solid var(--border);padding-bottom:8px">记忆系统</div>
        <div style="display:grid;grid-template-columns:repeat(auto-fill,minmax(240px,1fr));gap:16px">
          <div class="form-group">
            <label>记忆深度</label>
            <div class="select-wrap">
              <select name="memory_depth">
                <option value="shallow">shallow</option>
                <option value="medium">medium</option>
                <option value="deep">deep</option>
              </select>
            </div>
          </div>
          <div class="form-group">
            <label>基础记忆硬限制</label>
            <input type="number" name="basic_memory_hard_limit" min="0">
          </div>
          <div class="form-group">
            <label>基础上下文窗口</label>
            <input type="number" name="basic_memory_context_window" min="0">
          </div>
          <div class="form-group">
            <label>日记 Top-K</label>
            <input type="number" name="diary_top_k" min="0">
          </div>
          <div class="form-group">
            <label>日记 Token 预算</label>
            <input type="number" name="diary_token_budget" min="0">
          </div>
        </div>
      </form>
    </div>
  `;

  await loadExperience(name);
  setupSliders();
}

function setupSliders() {
  const sensitivity = document.querySelector('[name="engagement_sensitivity"]');
  const expressiveness = document.querySelector('[name="expressiveness"]');

  function updatePreview() {
    const s = parseFloat(sensitivity.value);
    const e = parseFloat(expressiveness.value);
    $('sensitivityLabel').textContent = s.toFixed(2);
    $('expressivenessLabel').textContent = e.toFixed(2);
    const style = getStyleLabel(s, e);
    $('styleLabel').textContent = style.label;
    $('styleDesc').textContent = style.desc;
  }

  if (sensitivity) sensitivity.addEventListener('input', updatePreview);
  if (expressiveness) expressiveness.addEventListener('input', updatePreview);
}

async function loadExperience(name) {
  try {
    const data = await get(`/personas/${name}/experience`);
    const form = $('expForm');
    if (!form) return;

    form.reply_mode.value = data.reply_mode || 'auto';
    form.engagement_sensitivity.value = data.engagement_sensitivity ?? 0.5;
    form.expressiveness.value = data.expressiveness ?? 0.5;
    form.heat_window_seconds.value = data.heat_window_seconds ?? 300;

    form.proactive_enabled.value = String(data.proactive_enabled ?? true);
    form.proactive_interval_seconds.value = data.proactive_interval_seconds ?? 3600;
    form.proactive_active_start_hour.value = data.proactive_active_start_hour ?? 8;
    form.proactive_active_end_hour.value = data.proactive_active_end_hour ?? 23;

    form.delay_reply_enabled.value = String(data.delay_reply_enabled ?? true);
    form.pending_message_threshold.value = data.pending_message_threshold ?? 3;
    form.min_reply_interval_seconds.value = data.min_reply_interval_seconds ?? 2;
    form.reply_frequency_window_seconds.value = data.reply_frequency_window_seconds ?? 60;
    form.reply_frequency_max_replies.value = data.reply_frequency_max_replies ?? 5;
    form.reply_frequency_exempt_on_mention.value = String(data.reply_frequency_exempt_on_mention ?? true);

    form.enable_skills.value = String(data.enable_skills ?? true);
    form.max_skill_rounds.value = data.max_skill_rounds ?? 3;
    form.skill_execution_timeout.value = data.skill_execution_timeout ?? 30;

    form.memory_depth.value = data.memory_depth || 'medium';
    form.basic_memory_hard_limit.value = data.basic_memory_hard_limit ?? 50;
    form.basic_memory_context_window.value = data.basic_memory_context_window ?? 20;
    form.diary_top_k.value = data.diary_top_k ?? 5;
    form.diary_token_budget.value = data.diary_token_budget ?? 2000;

    const s = parseFloat(form.engagement_sensitivity.value);
    const e = parseFloat(form.expressiveness.value);
    $('sensitivityLabel').textContent = s.toFixed(2);
    $('expressivenessLabel').textContent = e.toFixed(2);
    const style = getStyleLabel(s, e);
    $('styleLabel').textContent = style.label;
    $('styleDesc').textContent = style.desc;

    $('expSave').addEventListener('click', () => saveExperience(name));
  } catch (e) {
    toast('加载体验参数失败: ' + e.message, 'error');
  }
}

async function saveExperience(name) {
  const form = $('expForm');
  if (!form) return;

  const experience = {
    reply_mode: form.reply_mode.value,
    engagement_sensitivity: parseFloat(form.engagement_sensitivity.value),
    expressiveness: parseFloat(form.expressiveness.value),
    heat_window_seconds: parseInt(form.heat_window_seconds.value, 10),
    proactive_enabled: form.proactive_enabled.value === 'true',
    proactive_interval_seconds: parseInt(form.proactive_interval_seconds.value, 10),
    proactive_active_start_hour: parseInt(form.proactive_active_start_hour.value, 10),
    proactive_active_end_hour: parseInt(form.proactive_active_end_hour.value, 10),
    delay_reply_enabled: form.delay_reply_enabled.value === 'true',
    pending_message_threshold: parseInt(form.pending_message_threshold.value, 10),
    min_reply_interval_seconds: parseInt(form.min_reply_interval_seconds.value, 10),
    reply_frequency_window_seconds: parseInt(form.reply_frequency_window_seconds.value, 10),
    reply_frequency_max_replies: parseInt(form.reply_frequency_max_replies.value, 10),
    reply_frequency_exempt_on_mention: form.reply_frequency_exempt_on_mention.value === 'true',
    enable_skills: form.enable_skills.value === 'true',
    max_skill_rounds: parseInt(form.max_skill_rounds.value, 10),
    skill_execution_timeout: parseInt(form.skill_execution_timeout.value, 10),
    memory_depth: form.memory_depth.value,
    basic_memory_hard_limit: parseInt(form.basic_memory_hard_limit.value, 10),
    basic_memory_context_window: parseInt(form.basic_memory_context_window.value, 10),
    diary_top_k: parseInt(form.diary_top_k.value, 10),
    diary_token_budget: parseInt(form.diary_token_budget.value, 10),
  };

  try {
    await post(`/personas/${name}/experience`, { experience });
    flashSuccess($('expSave'));
    toast('体验参数已保存', 'success');
  } catch (e) {
    toast('保存失败: ' + e.message, 'error');
  }
}
