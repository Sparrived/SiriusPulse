import { store } from '../store.js';
import { get, post } from '../app.js';
import { toast, flashSuccess, $ } from '../components.js';

function numberInput(name, min, max, step) {
  const id = `exp_${name}`;
  return `
    <div class="number-input-group">
      <button type="button" class="number-spin-btn" data-spin-target="${id}" data-spin-dir="-1">−</button>
      <input id="${id}" type="number" name="${name}" min="${min}" max="${max || ''}" step="${step || 1}">
      <button type="button" class="number-spin-btn" data-spin-target="${id}" data-spin-dir="1">+</button>
    </div>
  `;
}

function getStyleInfo(s, e) {
  if (s >= 0.7 && e >= 0.7) return { label: '积极主导型', desc: '决策活跃度高，行为边界宽松。倾向于主动参与对话。', color: 'var(--success)' };
  if (s >= 0.7 && e <= 0.3) return { label: '被动回应型', desc: '决策活跃度高，但行为边界严格。仅在被明确指向时回复。', color: 'var(--accent)' };
  if (s <= 0.3 && e >= 0.7) return { label: '选择性参与型', desc: '决策活跃度低，但行为边界宽松。一旦决定参与则表现积极。', color: 'var(--warn)' };
  if (s <= 0.3 && e <= 0.3) return { label: '深度观察型', desc: '决策活跃度低，行为边界严格。极少参与对话。', color: 'var(--text-3)' };
  return { label: '均衡互动型', desc: '决策活跃度与行为边界均处于中等水平。', color: 'var(--info)' };
}

function quadrantSelector() {
  return `
    <div class="quadrant-container">
      <div class="quadrant-labels">
        <span class="quadrant-label-y">表达力</span>
        <span class="quadrant-label-x">参与灵敏度</span>
      </div>
      <div class="quadrant-grid" id="quadrantGrid">
        <div class="quadrant-cell quadrant-tl" data-s="low" data-e="high">
          <span class="quadrant-cell-label">选择性参与型</span>
        </div>
        <div class="quadrant-cell quadrant-tr" data-s="high" data-e="high">
          <span class="quadrant-cell-label">积极主导型</span>
        </div>
        <div class="quadrant-cell quadrant-bl" data-s="low" data-e="low">
          <span class="quadrant-cell-label">深度观察型</span>
        </div>
        <div class="quadrant-cell quadrant-br" data-s="high" data-e="low">
          <span class="quadrant-cell-label">被动回应型</span>
        </div>
        <div class="quadrant-center" data-s="mid" data-e="mid">
          <span class="quadrant-cell-label">均衡互动型</span>
        </div>
        <div class="quadrant-dot" id="quadrantDot"></div>
      </div>
      <div class="quadrant-axes">
        <span>低</span>
        <span>高</span>
      </div>
      <div class="quadrant-axes-y">
        <span>高</span>
        <span>低</span>
      </div>
      <input type="hidden" name="engagement_sensitivity" id="exp_engagement_sensitivity">
      <input type="hidden" name="expressiveness" id="exp_expressiveness">
    </div>
  `;
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
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:24px;align-items:start">
          <div style="display:grid;gap:16px">
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
              <label>参与灵敏度 <span id="sensitivityLabel" style="float:right;color:var(--accent)">0.50</span></label>
              <input type="range" id="sensitivitySlider" min="0" max="1" step="0.01" value="0.5">
            </div>
            <div class="form-group">
              <label>表达力 <span id="expressivenessLabel" style="float:right;color:var(--accent)">0.50</span></label>
              <input type="range" id="expressivenessSlider" min="0" max="1" step="0.01" value="0.5">
            </div>
            <div class="form-group">
              <label>热度窗口（秒）</label>
              ${numberInput('heat_window_seconds', 0)}
            </div>
            <div id="stylePreview" style="padding:12px 16px;background:var(--surface-1,var(--bg-2));border:1px solid var(--border);border-radius:var(--radius-md)">
              <div style="display:flex;align-items:center;gap:10px">
                <span id="styleDot" style="width:8px;height:8px;border-radius:50%;background:var(--accent)"></span>
                <span id="styleLabel" style="font-size:14px;font-weight:600;color:var(--text-1)">均衡互动型</span>
              </div>
              <div id="styleDesc" style="font-size:12px;color:var(--text-2);margin-top:6px;margin-left:18px">决策活跃度与行为边界均处于中等水平。</div>
            </div>
          </div>
          <div>
            <label style="font-size:14px;font-weight:500;margin-bottom:12px;display:block">行为类型</label>
            ${quadrantSelector()}
          </div>
        </div>

        <div style="font-size:15px;font-weight:600;color:var(--text-1);border-bottom:1px solid var(--border);padding-bottom:8px">回复频率</div>
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:16px">
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
            ${numberInput('pending_message_threshold', 0)}
          </div>
          <div class="form-group">
            <label>最小回复间隔（秒）</label>
            ${numberInput('min_reply_interval_seconds', 0)}
          </div>
          <div class="form-group">
            <label>Main model reply cooldown (seconds)</label>
            ${numberInput('main_model_reply_cooldown_seconds', 0)}
          </div>
          <div class="form-group">
            <label>频率窗口（秒）</label>
            ${numberInput('reply_frequency_window_seconds', 0)}
          </div>
          <div class="form-group">
            <label>窗口内最大回复数</label>
            ${numberInput('reply_frequency_max_replies', 0)}
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
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:16px">
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
            ${numberInput('max_skill_rounds', 0)}
          </div>
          <div class="form-group">
            <label>技能执行超时（秒）</label>
            ${numberInput('skill_execution_timeout', 0)}
          </div>
          <div class="form-group">
            <label>计划模式</label>
            <div class="select-wrap">
              <select name="plan_mode_enabled">
                <option value="false">false</option>
                <option value="true">true</option>
              </select>
            </div>
          </div>
          <div class="form-group">
            <label>普通聊天限制工具</label>
            <div class="select-wrap">
              <select name="plan_mode_limit_normal_tools">
                <option value="false">false</option>
                <option value="true">true</option>
              </select>
            </div>
          </div>
          <div class="form-group">
            <label>计划中允许轻量闲聊</label>
            <div class="select-wrap">
              <select name="plan_mode_allow_light_chat">
                <option value="true">true</option>
                <option value="false">false</option>
              </select>
            </div>
          </div>
          <div class="form-group">
            <label>Plan public status in chat prompt</label>
            <div class="select-wrap">
              <select name="plan_mode_chat_awareness_enabled">
                <option value="false">false</option>
                <option value="true">true</option>
              </select>
            </div>
          </div>
          <div class="form-group">
            <label>计划状态消息</label>
            <div class="select-wrap">
              <select name="plan_mode_presence_enabled">
                <option value="false">false</option>
                <option value="true">true</option>
              </select>
            </div>
          </div>
          <div class="form-group">
            <label>状态消息间隔（秒）</label>
            ${numberInput('plan_mode_presence_min_interval_seconds', 0)}
          </div>
          <div class="form-group">
            <label>进入计划状态文案</label>
            <input type="text" name="plan_mode_presence_enter_message">
          </div>
          <div class="form-group">
            <label>计划更新状态文案</label>
            <input type="text" name="plan_mode_presence_update_message">
          </div>
        </div>

        <div style="font-size:15px;font-weight:600;color:var(--text-1);border-bottom:1px solid var(--border);padding-bottom:8px">记忆系统</div>
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:16px">
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
            <label>日记 Top-K</label>
            ${numberInput('diary_top_k', 0)}
          </div>
          <div class="form-group">
            <label>日记 Token 预算</label>
            ${numberInput('diary_token_budget', 0)}
          </div>
        </div>
      </form>
    </div>
  `;

  await loadExperience(name);
}

function setupQuadrant() {
  const grid = $('quadrantGrid');
  const dot = $('quadrantDot');
  if (!grid || !dot) return;

  const sensitivityInput = $('exp_engagement_sensitivity');
  const expressivenessInput = $('exp_expressiveness');
  const sensitivitySlider = $('sensitivitySlider');
  const expressivenessSlider = $('expressivenessSlider');

  function updateDotPosition(s, e) {
    dot.style.left = `${s * 100}%`;
    dot.style.bottom = `${e * 100}%`;
  }

  function updateLabels(s, e) {
    const sensitivityEl = $('sensitivityLabel');
    const expressivenessEl = $('expressivenessLabel');
    if (sensitivityEl) sensitivityEl.textContent = s.toFixed(2);
    if (expressivenessEl) expressivenessEl.textContent = e.toFixed(2);
  }

  function updatePreview(s, e) {
    const style = getStyleInfo(s, e);
    const labelEl = $('styleLabel');
    const descEl = $('styleDesc');
    const dotEl = $('styleDot');
    if (labelEl) labelEl.textContent = style.label;
    if (descEl) descEl.textContent = style.desc;
    if (dotEl) dotEl.style.background = style.color;
  }

  function syncAll(s, e) {
    s = Math.max(0, Math.min(1, s));
    e = Math.max(0, Math.min(1, e));
    sensitivityInput.value = s.toFixed(2);
    expressivenessInput.value = e.toFixed(2);
    if (sensitivitySlider) sensitivitySlider.value = s;
    if (expressivenessSlider) expressivenessSlider.value = e;
    updateDotPosition(s, e);
    updateLabels(s, e);
    updatePreview(s, e);
  }

  // 四象限点击
  grid.addEventListener('click', (e) => {
    const rect = grid.getBoundingClientRect();
    const x = e.clientX - rect.left;
    const y = e.clientY - rect.top;
    const s = x / rect.width;
    const eVal = 1 - y / rect.height;
    syncAll(s, eVal);
  });

  // 滑块事件
  if (sensitivitySlider) {
    sensitivitySlider.addEventListener('input', () => {
      const s = parseFloat(sensitivitySlider.value);
      const e = parseFloat(expressivenessInput.value);
      syncAll(s, e);
    });
  }

  if (expressivenessSlider) {
    expressivenessSlider.addEventListener('input', () => {
      const s = parseFloat(sensitivityInput.value);
      const e = parseFloat(expressivenessSlider.value);
      syncAll(s, e);
    });
  }

  // 初始化
  const s = parseFloat(sensitivityInput.value) || 0.5;
  const e = parseFloat(expressivenessInput.value) || 0.5;
  syncAll(s, e);
}

async function loadExperience(name) {
  try {
    const data = await get(`/persona/experience`);
    const form = $('expForm');
    if (!form) return;

    form.reply_mode.value = data.reply_mode || 'auto';
    form.heat_window_seconds.value = data.heat_window_seconds ?? 300;

    // 设置四象限隐藏输入和滑块
    const sensitivityInput = $('exp_engagement_sensitivity');
    const expressivenessInput = $('exp_expressiveness');
    const sensitivitySlider = $('sensitivitySlider');
    const expressivenessSlider = $('expressivenessSlider');
    const s = data.engagement_sensitivity ?? 0.5;
    const e = data.expressiveness ?? 0.5;
    if (sensitivityInput) sensitivityInput.value = s.toFixed(2);
    if (expressivenessInput) expressivenessInput.value = e.toFixed(2);
    if (sensitivitySlider) sensitivitySlider.value = s;
    if (expressivenessSlider) expressivenessSlider.value = e;

    form.delay_reply_enabled.value = String(data.delay_reply_enabled ?? true);
    form.pending_message_threshold.value = data.pending_message_threshold ?? 3;
    form.min_reply_interval_seconds.value = data.min_reply_interval_seconds ?? 2;
    form.main_model_reply_cooldown_seconds.value = data.main_model_reply_cooldown_seconds ?? 0;
    form.reply_frequency_window_seconds.value = data.reply_frequency_window_seconds ?? 60;
    form.reply_frequency_max_replies.value = data.reply_frequency_max_replies ?? 5;
    form.reply_frequency_exempt_on_mention.value = String(data.reply_frequency_exempt_on_mention ?? true);

    form.enable_skills.value = String(data.enable_skills ?? true);
    form.max_skill_rounds.value = data.max_skill_rounds ?? 3;
    form.skill_execution_timeout.value = data.skill_execution_timeout ?? 30;
    form.plan_mode_enabled.value = String(data.plan_mode_enabled ?? false);
    form.plan_mode_limit_normal_tools.value = String(data.plan_mode_limit_normal_tools ?? false);
    form.plan_mode_allow_light_chat.value = String(data.plan_mode_allow_light_chat ?? true);
    form.plan_mode_chat_awareness_enabled.value = String(data.plan_mode_chat_awareness_enabled ?? false);
    form.plan_mode_presence_enabled.value = String(data.plan_mode_presence_enabled ?? false);
    form.plan_mode_presence_min_interval_seconds.value = data.plan_mode_presence_min_interval_seconds ?? 45;
    form.plan_mode_presence_enter_message.value = data.plan_mode_presence_enter_message || '我看到了，这个得稍微捋一下。';
    form.plan_mode_presence_update_message.value = data.plan_mode_presence_update_message || '补充我看到了，我会按新的前提来。';

    form.memory_depth.value = data.memory_depth || 'medium';
    form.diary_top_k.value = data.diary_top_k ?? 5;
    form.diary_token_budget.value = data.diary_token_budget ?? 2000;

    setupQuadrant();

    $('expSave').addEventListener('click', () => saveExperience(name));

    // 数字调节按钮事件
    document.querySelectorAll('[data-spin-target]').forEach(btn => {
      btn.addEventListener('click', () => {
        const target = document.getElementById(btn.dataset.spinTarget);
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
    delay_reply_enabled: form.delay_reply_enabled.value === 'true',
    pending_message_threshold: parseInt(form.pending_message_threshold.value, 10),
    min_reply_interval_seconds: parseInt(form.min_reply_interval_seconds.value, 10),
    main_model_reply_cooldown_seconds: parseFloat(form.main_model_reply_cooldown_seconds.value),
    reply_frequency_window_seconds: parseInt(form.reply_frequency_window_seconds.value, 10),
    reply_frequency_max_replies: parseInt(form.reply_frequency_max_replies.value, 10),
    reply_frequency_exempt_on_mention: form.reply_frequency_exempt_on_mention.value === 'true',
    enable_skills: form.enable_skills.value === 'true',
    max_skill_rounds: parseInt(form.max_skill_rounds.value, 10),
    skill_execution_timeout: parseInt(form.skill_execution_timeout.value, 10),
    plan_mode_enabled: form.plan_mode_enabled.value === 'true',
    plan_mode_limit_normal_tools: form.plan_mode_limit_normal_tools.value === 'true',
    plan_mode_allow_light_chat: form.plan_mode_allow_light_chat.value === 'true',
    plan_mode_chat_awareness_enabled: form.plan_mode_chat_awareness_enabled.value === 'true',
    plan_mode_presence_enabled: form.plan_mode_presence_enabled.value === 'true',
    plan_mode_presence_min_interval_seconds: parseInt(
      form.plan_mode_presence_min_interval_seconds.value,
      10
    ),
    plan_mode_presence_enter_message: form.plan_mode_presence_enter_message.value,
    plan_mode_presence_update_message: form.plan_mode_presence_update_message.value,
    memory_depth: form.memory_depth.value,
    diary_top_k: parseInt(form.diary_top_k.value, 10),
    diary_token_budget: parseInt(form.diary_token_budget.value, 10),
  };

  try {
    await post(`/persona/experience`, { experience });
    flashSuccess($('expSave'));
    toast('体验参数已保存', 'success');
  } catch (e) {
    toast('保存失败: ' + e.message, 'error');
  }
}
