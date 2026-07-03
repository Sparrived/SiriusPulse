import { store } from '../store.js';
import { get, post } from '../app.js';
import { confirmDanger, toast } from '../components.js';
import { createScopedPage } from '../page-context.js';
import { createAutoSave } from '../autosave.js';

const scopedPage = createScopedPage();

export function dispose() {
  scopedPage.use(null, null);
}
const $ = scopedPage.$;

const BOOLEAN_FIELDS = [
  'enable_skills',
  'plan_mode_enabled',
  'plan_mode_limit_normal_tools',
  'plan_mode_allow_light_chat',
  'plan_mode_chat_awareness_enabled',
  'plan_mode_presence_enabled',
];

let replyTimeCurvePoints = [];
const booleanState = {};

function numberInput(name, min, max, step) {
  const id = `exp_${name}`;
  return `
    <div class="number-input-group">
      <button type="button" class="number-spin-btn" data-spin-target="${id}" data-spin-dir="-1">−</button>
      <input id="${id}" type="number" name="${name}" min="${min}" max="${max ?? ''}" step="${step || 1}">
      <button type="button" class="number-spin-btn" data-spin-target="${id}" data-spin-dir="1">+</button>
    </div>
  `;
}

function toggleInput(name, title, desc, size = '') {
  return `
    <button type="button" class="exp-toggle ${size}" data-boolean-field="${name}" aria-pressed="false">
      <span class="exp-toggle-body">
        <strong>${title}</strong>
        <small>${desc}</small>
      </span>
      <span class="exp-toggle-state">关闭</span>
    </button>
  `;
}

function fieldCard(title, desc, control, size = '') {
  return `
    <div class="exp-field-card ${size}">
      <label>${title}</label>
      <p>${desc}</p>
      ${control}
    </div>
  `;
}

function replyTimeCurveEditor() {
  return `
    <div class="exp-curve-card">
      <div class="exp-curve-head">
        <div>
          <strong>24 小时回复系数曲线</strong>
          <small>最终参与分数 = 原始 score × 当前时间系数，再与回复阈值比较。</small>
        </div>
        <span class="exp-always-on">始终启用</span>
      </div>
      <div class="exp-curve-toolbar">
        <span id="replyCurveNow">当前系数：1.00</span>
        <button class="btn btn-sm" type="button" id="replyCurveReset">重置为全天 1.0</button>
      </div>
      <svg id="replyTimeCurve" class="reply-time-curve" viewBox="0 0 720 180" role="img" aria-label="24小时回复系数曲线"></svg>
      <div class="exp-curve-axis"><span>00:00</span><span>06:00</span><span>12:00</span><span>18:00</span><span>24:00</span></div>
      <div id="replyCurvePoints" class="exp-curve-points"></div>
    </div>
  `;
}

function parseCurveTime(value) {
  const match = String(value || '').match(/^(\d{1,2}):(\d{2})$/);
  if (!match) return null;
  const hour = parseInt(match[1], 10);
  const minute = parseInt(match[2], 10);
  if (hour === 24 && minute === 0) return 1440;
  if (hour < 0 || hour > 23 || minute < 0 || minute > 59) return null;
  return hour * 60 + minute;
}

function formatCurveTime(minutes) {
  const clamped = Math.max(0, Math.min(1440, Math.round(minutes)));
  if (clamped === 1440) return '24:00';
  const hour = Math.floor(clamped / 60);
  const minute = clamped % 60;
  return `${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`;
}

function normalizeCurvePoints(points) {
  const map = new Map();
  (Array.isArray(points) ? points : []).forEach(point => {
    const minutes = parseCurveTime(point?.time);
    if (minutes === null) return;
    const coefficient = Math.max(0, Math.min(2, Number(point.coefficient ?? 1)));
    map.set(minutes, Number(coefficient.toFixed(2)));
  });
  return [...map.entries()]
    .sort((a, b) => a[0] - b[0])
    .map(([minutes, coefficient]) => ({ time: formatCurveTime(minutes), coefficient }));
}

function coefficientAt(points, date = new Date()) {
  const normalized = normalizeCurvePoints(points);
  if (!normalized.length) return 1;
  if (normalized.length === 1) return normalized[0].coefficient;
  const anchors = normalized.map(point => [parseCurveTime(point.time), point.coefficient]);
  let current = date.getHours() * 60 + date.getMinutes() + date.getSeconds() / 60;
  for (let i = 0; i < anchors.length - 1; i += 1) {
    const [leftMinute, leftValue] = anchors[i];
    const [rightMinute, rightValue] = anchors[i + 1];
    if (current >= leftMinute && current <= rightMinute) {
      return interpolate(leftMinute, leftValue, rightMinute, rightValue, current);
    }
  }
  const [leftMinute, leftValue] = anchors[anchors.length - 1];
  const [rightMinute, rightValue] = anchors[0];
  if (current < rightMinute) current += 1440;
  return interpolate(leftMinute, leftValue, rightMinute + 1440, rightValue, current);
}

function interpolate(leftMinute, leftValue, rightMinute, rightValue, currentMinute) {
  if (rightMinute <= leftMinute) return leftValue;
  const ratio = (currentMinute - leftMinute) / (rightMinute - leftMinute);
  return Math.max(0, Math.min(2, leftValue + (rightValue - leftValue) * ratio));
}

function section(title, subtitle, body) {
  return `
    <section class="exp-section">
      <div class="exp-section-head">
        <div>
          <h3>${title}</h3>
          <p>${subtitle}</p>
        </div>
      </div>
      ${body}
    </section>
  `;
}

function getStyleInfo(s, e) {
  if (s >= 0.7 && e >= 0.7) return { label: '积极主导型', desc: '灵敏且外放，适合活跃群与陪伴感更强的人格。', color: 'var(--success)' };
  if (s >= 0.7 && e <= 0.3) return { label: '被动回应型', desc: '容易识别该回的消息，但表达克制，适合工具型人格。', color: 'var(--accent)' };
  if (s <= 0.3 && e >= 0.7) return { label: '选择性参与型', desc: '不轻易加入，但一旦加入会更主动、更有存在感。', color: 'var(--warn)' };
  if (s <= 0.3 && e <= 0.3) return { label: '深度观察型', desc: '低打扰、低存在感，适合安静群或只在明确需要时回应。', color: 'var(--text-3)' };
  return { label: '均衡互动型', desc: '参与判断和表达边界都适中，适合作为默认体验。', color: 'var(--info)' };
}

function quadrantSelector() {
  return `
    <div class="quadrant-container exp-quadrant">
      <div class="quadrant-labels">
        <span class="quadrant-label-y">表达力</span>
        <span class="quadrant-label-x">参与灵敏度</span>
      </div>
      <div class="quadrant-grid" id="quadrantGrid">
        <div class="quadrant-cell quadrant-tl" data-s="low" data-e="high"><span class="quadrant-cell-label">选择性参与</span></div>
        <div class="quadrant-cell quadrant-tr" data-s="high" data-e="high"><span class="quadrant-cell-label">积极主导</span></div>
        <div class="quadrant-cell quadrant-bl" data-s="low" data-e="low"><span class="quadrant-cell-label">深度观察</span></div>
        <div class="quadrant-cell quadrant-br" data-s="high" data-e="low"><span class="quadrant-cell-label">被动回应</span></div>
        <div class="quadrant-center" data-s="mid" data-e="mid"><span class="quadrant-cell-label">均衡互动</span></div>
        <div class="quadrant-dot" id="quadrantDot"></div>
      </div>
      <div class="quadrant-axes"><span>低</span><span>高</span></div>
      <div class="quadrant-axes-y"><span>高</span><span>低</span></div>
      <input type="hidden" name="engagement_sensitivity" id="exp_engagement_sensitivity">
      <input type="hidden" name="expressiveness" id="exp_expressiveness">
    </div>
  `;
}

function pageStyles() {
  return `
    <style>
      .experience-page { display:grid; gap:18px; }
      .exp-hero { display:flex; justify-content:space-between; gap:18px; align-items:flex-start; padding:20px; border:1px solid var(--border); border-radius:var(--radius-lg); background:linear-gradient(135deg,var(--surface-1,var(--bg-2)),transparent); }
      .exp-hero h2 { margin:0 0 6px; font-size:22px; color:var(--text-1); }
      .exp-hero p { margin:0; color:var(--text-2); font-size:13px; line-height:1.6; }
      .exp-pill-row { display:flex; gap:8px; flex-wrap:wrap; margin-top:14px; }
      .exp-pill { padding:4px 10px; border-radius:999px; border:1px solid var(--border); color:var(--text-2); font-size:12px; background:var(--bg-1); }
      .exp-section { border:1px solid var(--border); border-radius:var(--radius-lg); background:var(--bg-1); padding:18px; display:grid; gap:16px; }
      .exp-section-head { display:flex; justify-content:space-between; gap:12px; align-items:flex-start; }
      .exp-section h3 { margin:0; font-size:16px; color:var(--text-1); }
      .exp-section p { margin:4px 0 0; color:var(--text-2); font-size:12px; line-height:1.55; }
      .exp-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(190px,1fr)); grid-auto-flow:dense; gap:14px; align-items:stretch; }
      .exp-card-wide { grid-column:span 2; }
      .exp-card-tall { grid-row:span 2; }
      .exp-style-grid { display:grid; grid-template-columns:minmax(260px,1fr) minmax(280px,1fr); gap:18px; align-items:stretch; }
      .exp-field-card { display:grid; gap:9px; padding:14px; border:1px solid var(--border); border-radius:var(--radius-md); background:var(--surface-1,var(--bg-2)); }
      .exp-field-card label { font-size:13px; font-weight:700; color:var(--text-1); }
      .exp-field-card p { margin:0; min-height:34px; }
      .exp-slider-card { display:grid; gap:16px; }
      .exp-slider-row label { display:flex; justify-content:space-between; font-size:13px; font-weight:700; color:var(--text-1); margin-bottom:8px; }
      .exp-slider-row span { color:var(--accent); font-variant-numeric:tabular-nums; }
      .exp-style-preview { padding:14px 16px; border:1px solid var(--border); border-radius:var(--radius-md); background:var(--surface-1,var(--bg-2)); }
      .exp-style-preview strong { color:var(--text-1); font-size:15px; }
      .exp-style-preview p { margin-top:6px; }
      .exp-style-dot { width:10px; height:10px; border-radius:50%; display:inline-block; margin-right:8px; background:var(--accent); }
      .exp-toggle { display:flex; justify-content:space-between; align-items:flex-start; gap:14px; min-height:104px; text-align:left; padding:14px; border:1px solid var(--border); border-radius:var(--radius-md); background:var(--surface-1,var(--bg-2)); color:inherit; cursor:pointer; transition:border-color .15s, background .15s, box-shadow .15s; }
      .exp-toggle:hover { border-color:var(--accent); }
      .exp-toggle.is-enabled { border-color:color-mix(in srgb, var(--success) 70%, var(--border)); background:linear-gradient(135deg, color-mix(in srgb, var(--success) 18%, transparent), var(--surface-1,var(--bg-2))); box-shadow:inset 0 0 0 1px color-mix(in srgb, var(--success) 35%, transparent); }
      .exp-toggle.is-disabled { opacity:.78; }
      .exp-toggle-body { min-width:0; }
      .exp-toggle strong { display:block; color:var(--text-1); font-size:13px; margin-bottom:4px; }
      .exp-toggle small { display:block; color:var(--text-2); font-size:12px; line-height:1.45; }
      .exp-toggle-state { flex:0 0 auto; border-radius:999px; padding:3px 8px; font-size:11px; font-weight:700; color:var(--text-3); background:var(--bg-2); border:1px solid var(--border); }
      .exp-toggle.is-enabled .exp-toggle-state { color:var(--success); border-color:color-mix(in srgb, var(--success) 55%, var(--border)); background:color-mix(in srgb, var(--success) 14%, var(--bg-2)); }
      .exp-curve-card { margin-top:14px; padding:14px; border:1px solid var(--border); border-radius:var(--radius-md); background:var(--surface-1,var(--bg-2)); user-select:none; }
      .exp-curve-head { display:flex; align-items:flex-start; justify-content:space-between; gap:12px; }
      .exp-curve-head strong { display:block; color:var(--text-1); font-size:13px; margin-bottom:4px; }
      .exp-curve-head small { display:block; color:var(--text-2); font-size:12px; line-height:1.45; }
      .exp-always-on { flex:0 0 auto; border-radius:999px; padding:3px 8px; font-size:11px; font-weight:700; color:var(--success); background:color-mix(in srgb, var(--success) 14%, var(--bg-2)); border:1px solid color-mix(in srgb, var(--success) 55%, var(--border)); }
      .exp-curve-toolbar { display:flex; justify-content:space-between; align-items:center; gap:12px; margin:14px 0 8px; color:var(--text-2); font-size:12px; }
      .reply-time-curve { width:100%; height:180px; display:block; border:1px solid var(--border); border-radius:var(--radius-md); background:linear-gradient(to bottom, rgba(255,255,255,.04), transparent); touch-action:none; user-select:none; }
      .reply-time-curve path { fill:none; stroke:var(--accent); stroke-width:3; }
      .reply-time-curve circle { fill:var(--accent); stroke:var(--bg-1); stroke-width:3; cursor:grab; }
      .reply-time-curve.is-dragging, .reply-time-curve.is-dragging circle, .reply-time-curve circle:active { cursor:grabbing; }
      .reply-time-curve text { fill:var(--text-3); font-size:10px; }
      .reply-time-curve line { stroke:var(--border); stroke-width:1; opacity:.65; }
      .exp-curve-axis { display:flex; justify-content:space-between; color:var(--text-3); font-size:11px; margin-top:6px; }
      .exp-curve-points { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:8px; margin-top:12px; }
      .exp-curve-point { display:flex; gap:6px; align-items:center; }
      .exp-curve-point input { width:100%; background:var(--surface-2); color:var(--text); border:1px solid var(--border); border-radius:6px; padding:6px 8px; font-size:12px; }
      .exp-quadrant { min-height:260px; }
      @media (max-width: 900px) { .exp-style-grid { grid-template-columns:1fr; } .exp-hero { flex-direction:column; } }
      @media (max-width: 620px) { .exp-grid { grid-template-columns:1fr; } .exp-card-wide, .exp-card-tall { grid-column:span 1; grid-row:span 1; } .exp-toggle { min-height:auto; } }
    </style>
  `;
}

export async function init(container, params = {}) {
  scopedPage.use(params?.ctx, container);
  const name = store.currentPersona;
  if (!name) {
    container.innerHTML = `
      <div class="card">
        <div class="card-header"><div class="card-title">体验参数</div></div>
        <div style="padding:40px;text-align:center;color:var(--text-3)">
          <div style="font-size:48px;margin-bottom:16px">✦</div>
          <div style="font-size:16px;margin-bottom:8px">请先选择人格</div>
          <div style="font-size:13px">在侧边栏中选择要配置的人格</div>
        </div>
      </div>
    `;
    return;
  }

  for (const field of BOOLEAN_FIELDS) delete booleanState[field];

  container.innerHTML = `
    ${pageStyles()}
    <div class="experience-page">
      <div class="exp-hero">
        <div>
          <h2>体验参数</h2>
          <p>调整 ${name} 在群聊中的参与节奏、回复长度、工具能力和记忆检索。常用项放在前面，高级开关集中在下方。</p>
          <div class="exp-pill-row">
            <span class="exp-pill">行为风格</span>
            <span class="exp-pill">回复控制</span>
            <span class="exp-pill">工具与计划</span>
            <span class="exp-pill">记忆检索</span>
          </div>
        </div>
        <span id="expAutoSaveStatus" style="color:var(--text-3);font-size:12px"></span>
      </div>

      <form id="expForm" style="display:grid;gap:18px">
        ${section('行为画像', '用两个主旋钮决定它多容易参与、参与后多外放。点击象限也可以快速设值。', `
          <div class="exp-style-grid">
            <div class="exp-slider-card">
              <div class="exp-slider-row">
                <label>参与灵敏度 <span id="sensitivityLabel">0.50</span></label>
                <input type="range" id="sensitivitySlider" min="0" max="1" step="0.01" value="0.5">
              </div>
              <div class="exp-slider-row">
                <label>表达力 <span id="expressivenessLabel">0.50</span></label>
                <input type="range" id="expressivenessSlider" min="0" max="1" step="0.01" value="0.5">
              </div>
              <div class="exp-style-preview">
                <div><span class="exp-style-dot" id="styleDot"></span><strong id="styleLabel">均衡互动型</strong></div>
                <p id="styleDesc">参与判断和表达边界都适中，适合作为默认体验。</p>
              </div>
            </div>
            ${quadrantSelector()}
          </div>
        `)}

        ${section('回复控制', '控制实际发言节奏和外显长度，避免刷屏或连续抢答。', `
          <div class="exp-grid">
            ${fieldCard('最小回复间隔（秒）', '两次实际发言之间至少等待多久。0 表示不限制。', numberInput('min_reply_interval_seconds', 0), 'exp-card-wide')}
            ${fieldCard('主模型调用冷却（秒）', '主回复模型之间的冷却时间，用于降低连续 LLM 调用。', numberInput('main_model_reply_cooldown_seconds', 0, null, 0.5))}
            ${fieldCard('单句最大长度（字）', '限制拆分后每句话的长度，越小越像短句聊天。', numberInput('max_sentence_chars', 5, 50))}
          </div>
          ${replyTimeCurveEditor()}
        `)}

        ${section('工具与计划', '集中管理工具能力、计划流程和计划状态表现。', `
          <div class="exp-grid">
            ${toggleInput('enable_skills', '启用技能', '允许模型在需要时调用已启用技能。', 'exp-card-wide')}
            ${fieldCard('最大技能轮数', '限制单次回复中工具调用和模型续写的循环次数。', numberInput('max_skill_rounds', 0))}
            ${toggleInput('plan_mode_enabled', '启用计划模式', '允许模型进入多步骤计划流程。', 'exp-card-tall')}
            ${toggleInput('plan_mode_limit_normal_tools', '普通聊天限制工具', '计划模式开启时，普通聊天不主动使用常规工具。')}
            ${toggleInput('plan_mode_allow_light_chat', '计划中允许轻量闲聊', '计划执行期间允许少量自然聊天，不完全静默。', 'exp-card-wide')}
            ${toggleInput('plan_mode_chat_awareness_enabled', '在聊天中暴露计划状态', '把公开计划状态注入聊天提示词，便于上下文衔接。')}
            ${toggleInput('plan_mode_presence_enabled', '发送计划状态消息', '计划处理较久时，向群里发送“正在处理”的存在感消息。')}
            ${fieldCard('状态消息间隔（秒）', '计划状态消息的最小发送间隔。', numberInput('plan_mode_presence_min_interval_seconds', 0))}
          </div>
        `)}

        ${section('记忆检索', '控制本轮回复最多注入多少日记上下文。数值越高越有记忆感，也越耗 token。', `
          <div class="exp-grid">
            ${fieldCard('日记 Top-K', '从日记索引中最多检索多少条相关记录。', numberInput('diary_top_k', 0))}
            ${fieldCard('日记 Token 预算', '日记上下文可占用的最大 token 预算。', numberInput('diary_token_budget', 0))}
          </div>
        `)}
      </form>
    </div>
  `;

  const autoSave = createAutoSave({
    root: $('expForm'),
    statusEl: $('expAutoSaveStatus'),
    save: () => saveExperience(name),
    onError: (error) => toast('保存失败: ' + error.message, 'error'),
  });
  setupBooleanCards(container, autoSave);
  await loadExperience(name);
  autoSave.markReady();
}

function setupQuadrant(autoSave) {
  const grid = $('quadrantGrid');
  const dot = $('quadrantDot');
  if (!grid || !dot) return;

  const sensitivityInput = $('exp_engagement_sensitivity');
  const expressivenessInput = $('exp_expressiveness');
  const sensitivitySlider = $('sensitivitySlider');
  const expressivenessSlider = $('expressivenessSlider');

  function syncAll(s, e) {
    s = Math.max(0, Math.min(1, s));
    e = Math.max(0, Math.min(1, e));
    sensitivityInput.value = s.toFixed(2);
    expressivenessInput.value = e.toFixed(2);
    if (sensitivitySlider) sensitivitySlider.value = s;
    if (expressivenessSlider) expressivenessSlider.value = e;
    dot.style.left = `${s * 100}%`;
    dot.style.bottom = `${e * 100}%`;

    const sensitivityEl = $('sensitivityLabel');
    const expressivenessEl = $('expressivenessLabel');
    if (sensitivityEl) sensitivityEl.textContent = s.toFixed(2);
    if (expressivenessEl) expressivenessEl.textContent = e.toFixed(2);

    const info = getStyleInfo(s, e);
    const styleDot = $('styleDot');
    const styleLabel = $('styleLabel');
    const styleDesc = $('styleDesc');
    if (styleDot) styleDot.style.background = info.color;
    if (styleLabel) styleLabel.textContent = info.label;
    if (styleDesc) styleDesc.textContent = info.desc;
  }

  grid.querySelectorAll('[data-s]').forEach(cell => {
    cell.addEventListener('click', () => {
      const map = { low: 0.2, mid: 0.5, high: 0.8 };
      syncAll(map[cell.dataset.s] ?? 0.5, map[cell.dataset.e] ?? 0.5);
      autoSave?.schedule();
    });
  });

  grid.addEventListener('click', e => {
    if (e.target.closest('[data-s]')) return;
    const rect = grid.getBoundingClientRect();
    syncAll((e.clientX - rect.left) / rect.width, 1 - (e.clientY - rect.top) / rect.height);
    autoSave?.schedule();
  });

  sensitivitySlider?.addEventListener('input', () => {
    syncAll(parseFloat(sensitivitySlider.value), parseFloat(expressivenessInput.value));
    autoSave?.schedule();
  });
  expressivenessSlider?.addEventListener('input', () => {
    syncAll(parseFloat(sensitivityInput.value), parseFloat(expressivenessSlider.value));
    autoSave?.schedule();
  });

  syncAll(parseFloat(sensitivityInput.value) || 0.5, parseFloat(expressivenessInput.value) || 0.5);
}

function setupReplyTimeCurve(autoSave) {
  const svg = $('replyTimeCurve');
  const list = $('replyCurvePoints');
  const reset = $('replyCurveReset');
  if (!svg || !list) return;

  const width = 720;
  const height = 180;
  const padding = 14;
  const plotHeight = height - padding * 2;
  let dragIndex = null;
  let previousBodyUserSelect = '';

  function pointToSvg(point) {
    const minutes = parseCurveTime(point.time) ?? 0;
    return {
      x: (minutes / 1440) * width,
      y: padding + ((2 - point.coefficient) / 2) * plotHeight,
    };
  }

  function eventToPoint(event, index = null) {
    const rect = svg.getBoundingClientRect();
    const x = Math.max(0, Math.min(width, ((event.clientX - rect.left) / rect.width) * width));
    const y = Math.max(padding, Math.min(height - padding, ((event.clientY - rect.top) / rect.height) * height));
    let minutes = Math.round((x / width) * 1440);
    if (index !== null) {
      const previous = index > 0 ? parseCurveTime(replyTimeCurvePoints[index - 1]?.time) : null;
      const next = index < replyTimeCurvePoints.length - 1 ? parseCurveTime(replyTimeCurvePoints[index + 1]?.time) : null;
      const min = previous === null ? 0 : previous + 1;
      const max = next === null ? 1440 : next - 1;
      minutes = Math.max(min, Math.min(max, minutes));
    }
    return {
      time: formatCurveTime(minutes),
      coefficient: Number((2 - ((y - padding) / plotHeight) * 2).toFixed(2)),
    };
  }

  function startDrag(event, index) {
    event.preventDefault();
    dragIndex = index;
    previousBodyUserSelect = document.body.style.userSelect;
    document.body.style.userSelect = 'none';
    svg.classList.add('is-dragging');
    svg.setPointerCapture?.(event.pointerId);
  }

  function endDrag(event) {
    if (dragIndex === null) return;
    dragIndex = null;
    document.body.style.userSelect = previousBodyUserSelect;
    svg.classList.remove('is-dragging');
    if (event?.pointerId !== undefined && svg.hasPointerCapture?.(event.pointerId)) {
      svg.releasePointerCapture?.(event.pointerId);
    }
  }

  function render() {
    replyTimeCurvePoints = normalizeCurvePoints(replyTimeCurvePoints);
    if (!replyTimeCurvePoints.length) {
      replyTimeCurvePoints = [{ time: '00:00', coefficient: 1 }, { time: '24:00', coefficient: 1 }];
    }

    const points = replyTimeCurvePoints.map(pointToSvg);
    const path = points.map((point, index) => `${index ? 'L' : 'M'} ${point.x.toFixed(1)} ${point.y.toFixed(1)}`).join(' ');
    const grid = [0, 0.5, 1, 1.5, 2].map(value => {
      const y = padding + ((2 - value) / 2) * plotHeight;
      return `<line x1="0" y1="${y}" x2="${width}" y2="${y}"></line><text x="4" y="${y - 4}">${value.toFixed(1)}</text>`;
    }).join('');
    const circles = points.map((point, index) => `
      <circle data-curve-index="${index}" cx="${point.x.toFixed(1)}" cy="${point.y.toFixed(1)}" r="7"></circle>
    `).join('');
    svg.innerHTML = `${grid}<path d="${path}"></path>${circles}`;

    const current = coefficientAt(replyTimeCurvePoints);
    const nowLabel = $('replyCurveNow');
    if (nowLabel) nowLabel.textContent = `当前系数：${current.toFixed(2)}（最终 score × ${current.toFixed(2)}）`;

    list.innerHTML = replyTimeCurvePoints.map((point, index) => `
      <div class="exp-curve-point">
        <input type="time" data-curve-time="${index}" value="${point.time === '24:00' ? '23:59' : point.time}">
        <input type="number" data-curve-coefficient="${index}" min="0" max="2" step="0.05" value="${point.coefficient}">
        <button class="btn btn-sm" type="button" data-curve-remove="${index}" ${replyTimeCurvePoints.length <= 1 ? 'disabled' : ''}>删除</button>
      </div>
    `).join('');
    bindPointList();
  }

  function bindPointList() {
    list.querySelectorAll('[data-curve-time]').forEach(input => {
      input.addEventListener('change', () => {
        replyTimeCurvePoints[parseInt(input.dataset.curveTime, 10)].time = input.value;
        render();
        autoSave?.schedule();
      });
    });
    list.querySelectorAll('[data-curve-coefficient]').forEach(input => {
      input.addEventListener('input', () => {
        replyTimeCurvePoints[parseInt(input.dataset.curveCoefficient, 10)].coefficient = Number(input.value);
        render();
        autoSave?.schedule();
      });
    });
    list.querySelectorAll('[data-curve-remove]').forEach(button => {
      button.addEventListener('click', () => {
        if (!confirmDanger()) return;
        replyTimeCurvePoints.splice(parseInt(button.dataset.curveRemove, 10), 1);
        render();
        autoSave?.schedule();
      });
    });
  }

  svg.addEventListener('pointerdown', event => {
    const circle = event.target.closest?.('[data-curve-index]');
    if (circle) {
      startDrag(event, parseInt(circle.dataset.curveIndex, 10));
      return;
    }
    event.preventDefault();
    replyTimeCurvePoints.push(eventToPoint(event));
    render();
    autoSave?.schedule();
  });

  svg.addEventListener('pointermove', event => {
    if (dragIndex === null) return;
    event.preventDefault();
    replyTimeCurvePoints[dragIndex] = eventToPoint(event, dragIndex);
    render();
    autoSave?.schedule();
  });

  svg.addEventListener('pointerup', endDrag);
  svg.addEventListener('pointercancel', endDrag);
  svg.addEventListener('lostpointercapture', endDrag);

  reset?.addEventListener('click', () => {
    replyTimeCurvePoints = [{ time: '00:00', coefficient: 1 }, { time: '24:00', coefficient: 1 }];
    render();
    autoSave?.schedule();
  });

  render();
}

function setBooleanField(name, value) {
  booleanState[name] = Boolean(value);
  const card = scopedPage.query(`[data-boolean-field="${name}"]`);
  if (!card) return;
  card.classList.toggle('is-enabled', booleanState[name]);
  card.classList.toggle('is-disabled', !booleanState[name]);
  card.setAttribute('aria-pressed', String(booleanState[name]));
  const state = card.querySelector('.exp-toggle-state');
  if (state) state.textContent = booleanState[name] ? '启用' : '关闭';
}

function setupBooleanCards(root, autoSave) {
  scopedPage.on(root, 'click', (event) => {
    const card = event.target.closest?.('[data-boolean-field]');
    if (!card || !root.contains(card)) return;
    event.preventDefault();
    const name = card.dataset.booleanField;
    setBooleanField(name, !booleanState[name]);
    autoSave?.schedule();
  });
}

async function loadExperience(name) {
  try {
    const data = await get(`/persona/experience`);
    const form = $('expForm');
    if (!form) return;

    const s = data.engagement_sensitivity ?? 0.5;
    const e = data.expressiveness ?? 0.5;
    $('exp_engagement_sensitivity').value = Number(s).toFixed(2);
    $('exp_expressiveness').value = Number(e).toFixed(2);
    $('sensitivitySlider').value = s;
    $('expressivenessSlider').value = e;

    form.min_reply_interval_seconds.value = data.min_reply_interval_seconds ?? 2;
    form.main_model_reply_cooldown_seconds.value = data.main_model_reply_cooldown_seconds ?? 0;
    replyTimeCurvePoints = normalizeCurvePoints(data.reply_time_curve_points || []);
    if (!replyTimeCurvePoints.length) {
      replyTimeCurvePoints = [{ time: '00:00', coefficient: 1 }, { time: '24:00', coefficient: 1 }];
    }
    form.max_sentence_chars.value = data.max_sentence_chars ?? 20;
    form.max_skill_rounds.value = data.max_skill_rounds ?? 3;
    form.plan_mode_presence_min_interval_seconds.value = data.plan_mode_presence_min_interval_seconds ?? 45;
    form.diary_top_k.value = data.diary_top_k ?? 5;
    form.diary_token_budget.value = data.diary_token_budget ?? 2000;

    setBooleanField('enable_skills', data.enable_skills ?? true);
    setBooleanField('plan_mode_enabled', data.plan_mode_enabled ?? false);
    setBooleanField('plan_mode_limit_normal_tools', data.plan_mode_limit_normal_tools ?? false);
    setBooleanField('plan_mode_allow_light_chat', data.plan_mode_allow_light_chat ?? true);
    setBooleanField('plan_mode_chat_awareness_enabled', data.plan_mode_chat_awareness_enabled ?? false);
    setBooleanField('plan_mode_presence_enabled', data.plan_mode_presence_enabled ?? false);

    setupQuadrant(autoSave);
    setupReplyTimeCurve(autoSave);

    scopedPage.$$('[data-spin-target]').forEach(btn => {
      btn.addEventListener('click', () => {
        const target = $(btn.dataset.spinTarget);
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
    if (e?.name === 'AbortError') return;
    toast('加载体验参数失败: ' + e.message, 'error');
  }
}

async function saveExperience(name) {
  const form = $('expForm');
  if (!form) return;

  const experience = {
    engagement_sensitivity: parseFloat(form.engagement_sensitivity.value),
    expressiveness: parseFloat(form.expressiveness.value),
    min_reply_interval_seconds: parseInt(form.min_reply_interval_seconds.value, 10),
    main_model_reply_cooldown_seconds: parseFloat(form.main_model_reply_cooldown_seconds.value),
    reply_time_curve_points: normalizeCurvePoints(replyTimeCurvePoints),
    max_sentence_chars: parseInt(form.max_sentence_chars.value, 10),
    max_skill_rounds: parseInt(form.max_skill_rounds.value, 10),
    plan_mode_presence_min_interval_seconds: parseInt(
      form.plan_mode_presence_min_interval_seconds.value,
      10
    ),
    diary_top_k: parseInt(form.diary_top_k.value, 10),
    diary_token_budget: parseInt(form.diary_token_budget.value, 10),
  };

  BOOLEAN_FIELDS.forEach(name => {
    experience[name] = Boolean(booleanState[name]);
  });

  try {
    await post(`/persona/experience`, { experience });
  } catch (e) {
    if (e?.name === 'AbortError') return;
    throw e;
  }
}
