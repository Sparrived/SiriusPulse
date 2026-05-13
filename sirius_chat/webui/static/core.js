const API = '/api';
let personas = [];
let currentPersona = null;
let personaState = {};
let providerDraft = [];
let currentPage = 'dashboard';

const PAGE_LOADERS = {};

function registerPageLoader(page, { init, refresh } = {}) {
  PAGE_LOADERS[page] = {
    init: init || null,
    refresh: refresh === undefined ? (init || null) : refresh,
  };
}

// Adapter 白名单的独立状态（避免被 personaState 覆盖丢失）
let adapterGroupIds = [];
let adapterPrivateIds = [];

// 缓存上次统计数据，避免无变化时重复重建 DOM 触发跳动
let _lastTelemetryData = null;
let _lastTokenData = null;

function $(id) { return document.getElementById(id); }

/* ── Animation helpers ──────────────────────────────── */

function animateNumber(el, target, duration = 600) {
  if (!el) return;
  const start = parseInt(el.textContent.replace(/,/g, '') || '0', 10) || 0;
  if (start === target) return;
  const startTime = performance.now();
  function tick(now) {
    const elapsed = now - startTime;
    const progress = Math.min(elapsed / duration, 1);
    const eased = 1 - Math.pow(1 - progress, 3);
    const current = Math.round(start + (target - start) * eased);
    el.textContent = String(current);
    if (progress < 1) requestAnimationFrame(tick);
  }
  requestAnimationFrame(tick);
}

function flashSuccess(btn) {
  if (!btn) return;
  const prev = btn.textContent;
  btn.classList.add('btn-success-flash');
  btn.textContent = '✓ ' + prev;
  btn.disabled = true;
  setTimeout(() => {
    btn.classList.remove('btn-success-flash');
    btn.textContent = prev;
    btn.disabled = false;
  }, 1200);
}

function applyStagger(containerSelector, childSelector) {
  const container = typeof containerSelector === 'string'
    ? document.querySelector(containerSelector)
    : containerSelector;
  if (!container) return;
  container.classList.add('animate-stagger');
  const children = childSelector
    ? container.querySelectorAll(childSelector)
    : container.children;
  Array.from(children).forEach((child, i) => {
    child.style.setProperty('--i', String(i));
  });
}

function toast(msg, type = 'success') {
  const t = $('toast');
  t.textContent = msg;
  t.className = 'toast ' + type + ' show';
  setTimeout(() => t.classList.remove('show'), 3000);
}

async function get(path, signal) {
  const r = await fetch(API + path, signal ? { signal } : undefined);
  if (!r.ok) {
    const text = await r.text();
    throw new Error(`HTTP ${r.status} ${r.statusText}: ${text.slice(0, 200)}`);
  }
  return r.json();
}
async function post(path, body) {
  const r = await fetch(API + path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!r.ok) {
    const text = await r.text();
    throw new Error(`HTTP ${r.status} ${r.statusText}: ${text.slice(0, 200)}`);
  }
  return r.json();
}
async function del(path) {
  const r = await fetch(API + path, { method: 'DELETE' });
  if (!r.ok) {
    const text = await r.text();
    throw new Error(`HTTP ${r.status} ${r.statusText}: ${text.slice(0, 200)}`);
  }
  return r.json();
}

function pApi(path) {
  return `/personas/${currentPersona}${path}`;
}

// ── Navigation ────────────────────────────────────────
const pageTitles = {
  dashboard: ['概览', 'Dashboard'],
  'global-settings': ['全局设置', 'Configuration / Global'],
  providers: ['Provider 配置', 'Configuration / Providers'],
  persona: ['人格配置', 'Configuration / Persona'],
  'create-persona': ['新建人格', 'Configuration / Create Persona'],
  orchestration: ['模型编排', 'Configuration / Orchestration'],
  experience: ['体验参数', 'Configuration / Experience'],
  adapters: ['Adapter 配置', 'Configuration / Adapters'],
  skills: ['Skill 管理', 'Configuration / Skills'],
  'napcat': ['NapCat 管理', 'Platform / NapCat'],
  'plugins': ['Plugin 管理', 'Platform / Plugins'],
  'token-tracker': ['Token 追踪', 'Analytics / Token Tracker'],
  'cognition': ['认知分析', 'Analytics / Cognition'],
  'diary': ['日记', 'Analytics / Diary'],
  'users': ['用户画像', 'Analytics / Users'],
  'glossary': ['名词解释', 'Analytics / Glossary'],
  'stickers': ['表情包库', 'Analytics / Stickers'],
  'memory-viz': ['记忆可视化', 'Analytics / Memory Viz'],
};

async function navTo(page) {
  currentPage = page;
  document.querySelectorAll('.nav-item').forEach((el) => el.classList.remove('active'));
  document.querySelector(`.nav-item[data-page="${page}"]`)?.classList.add('active');
  const t = pageTitles[page];
  $('pageTitle').textContent = t?.[0] ?? '';
  $('pageBreadcrumb').textContent = t?.[1] ?? '';

  const container = $('mainContainer');
  try {
    const res = await fetch(`/static/pages/${page}.html`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    container.innerHTML = await res.text();
  } catch (e) {
    container.innerHTML = `<div class="card"><h2>加载失败</h2><p>无法加载页面：${page}</p><pre style="color:var(--text-2);font-size:12px">${e.message}</pre></div>`;
    console.error('navTo error:', e);
  }

  // 页面切换入场动画
  container.classList.remove('animate-fade-in');
  void container.offsetWidth; // force reflow
  container.classList.add('animate-fade-in');

  // 页面加载后重新填充人格下拉框
  renderPersonaSelect();

  // 如果尚未选中有数据的人格，默认选中第一个
  if (!currentPersona && personas.length > 0) {
    selectPersona(personas[0].name);
  }

  const loader = PAGE_LOADERS[page];
  if (loader?.init) await loader.init();

  // Replace native <select> with custom dropdowns after page loads
  setTimeout(() => mountCustomSelects(), 0);
}

// ── Custom Select ─────────────────────────────────────
function mountCustomSelect(sel) {
  if (sel._customMounted || sel.style.display === 'none') return;
  sel._customMounted = true;
  sel.style.display = 'none';

  const wrapper = document.createElement('div');
  wrapper.className = 'custom-select';
  const btn = document.createElement('button');
  btn.className = 'custom-select-btn';
  btn.type = 'button';
  const list = document.createElement('div');
  list.className = 'custom-select-list';
  wrapper.appendChild(btn);
  wrapper.appendChild(list);
  sel.parentNode.insertBefore(wrapper, sel.nextSibling);

  function sync() {
    const opts = Array.from(sel.options);
    const current = sel.value;
    const currentLabel = opts.find((o) => o.value === current)?.textContent || current;
    btn.textContent = currentLabel;
    list.innerHTML = opts.map((o) => {
      const active = o.value === current;
      return `<div class="custom-select-item${active ? ' active' : ''}" data-value="${o.value.replace(/"/g, '&quot;')}">${o.textContent}</div>`;
    }).join('');
  }

  btn.addEventListener('click', (e) => {
    e.stopPropagation();
    const open = wrapper.classList.contains('open');
    document.querySelectorAll('.custom-select').forEach((w) => w.classList.remove('open'));
    wrapper.classList.toggle('open', !open);
  });

  list.addEventListener('click', (e) => {
    const item = e.target.closest('.custom-select-item');
    if (!item) return;
    const val = item.dataset.value;
    sel.value = val;
    sel.dispatchEvent(new Event('change', { bubbles: true }));
    wrapper.classList.remove('open');
    sync();
  });

  // Intercept native value setter so .value = x updates UI automatically
  const proto = Object.getPrototypeOf(sel);
  const nativeDesc = Object.getOwnPropertyDescriptor(proto, 'value');
  if (nativeDesc && nativeDesc.set) {
    Object.defineProperty(sel, 'value', {
      get() { return nativeDesc.get.call(sel); },
      set(v) { nativeDesc.set.call(sel, v); sync(); },
      configurable: true,
    });
  }

  // Watch options changes (innerHTML / appendChild)
  const observer = new MutationObserver(sync);
  observer.observe(sel, { childList: true });

  sync();
}

function mountCustomSelects(root) {
  (root || document).querySelectorAll('select').forEach((sel) => mountCustomSelect(sel));
}

function syncCustomSelect(id) {
  const sel = $(id);
  if (sel && sel._customMounted) {
    // Trigger sync by touching the value
    const v = sel.value;
    sel.value = v;
  }
}

// ── Personas ──────────────────────────────────────────
async function loadPersonas() {
  try {
    const res = await get('/personas');
    personas = res.personas || [];
    renderPersonaSelect();
    renderPersonaCards(false);
    updateSidebar();
    if (!currentPersona && personas.length > 0) {
      selectPersona(personas[0].name);
    }
    // Load telemetry and token stats in parallel (don't block persona list)
    loadTelemetry();
    loadTokenStats();
  } catch (e) {
    console.error('loadPersonas', e);
    personas = [];
    renderPersonaSelect();
    updateSidebar();
  }
}

function renderPersonaSelect() {
  document.querySelectorAll('.persona-select-bar').forEach((el) => {
    if (!personas.length) {
      el.innerHTML = '<span style="color:var(--text-2);font-size:13px">暂无人格</span>';
      return;
    }
    el.innerHTML = personas.map((p) => {
      const selected = p.name === currentPersona;
      return `<div class="persona-chip ${p.running ? 'running' : ''} ${selected ? 'selected' : ''}" onclick="selectPersona('${p.name}')">`
        + `<div class="chip-status">${p.running ? '●' : '○'}</div>`
        + `<div class="chip-name">${p.persona_name || p.name}</div>`
        + `</div>`;
    }).join('');
  });
}

function selectPersona(name) {
  currentPersona = name;
  renderPersonaSelect();
  loadPersonaStatus();
}

async function loadPersonaStatus() {
  if (!currentPersona) return;
  try {
    personaState = await get(pApi('/status'));
    updateSidebar();
    const loader = PAGE_LOADERS[currentPage];
    if (loader?.refresh) await loader.refresh();
  } catch (e) {
    console.error('loadPersonaStatus', e);
  }
}

function updateSidebar() {
  const running = personas.filter((p) => p.running).length;
  $('sbCurrentPersona').textContent = currentPersona ? (personaState.persona_name || currentPersona) : '—';
  animateNumber($('sbPersonaCount'), personas.length, 400);
  animateNumber($('sbRunningCount'), running, 400);
  const dot = $('sbRunningDot');
  if (dot) {
    dot.classList.toggle('ok', running > 0);
    dot.classList.toggle('pulse', running > 0);
  }
}

function formatHeartbeat(ts) {
  if (!ts) return '—';
  const d = new Date(ts);
  const now = new Date();
  const diff = (now - d) / 1000;
  if (diff < 5) return '刚刚';
  if (diff < 60) return `${Math.floor(diff)}秒前`;
  if (diff < 3600) return `${Math.floor(diff / 60)}分钟前`;
  return d.toLocaleString();
}

function renderPersonaCards(animate = true) {
  const el = $('personaCards');
  if (!el) return;
  if (!personas.length) {
    el.innerHTML = '<div style="color:var(--text-2);padding:20px">暂无人格。使用 CLI <code>python main.py persona create &lt;name&gt;</code> 创建。</div>';
    return;
  }

  // 非动画刷新尝试增量更新，避免 DOM 重建导致的跳动
  if (!animate) {
    const cards = el.querySelectorAll('.persona-card');
    let needRebuild = cards.length !== personas.length;
    if (!needRebuild) {
      for (let i = 0; i < personas.length; i++) {
        if (cards[i].dataset.name !== personas[i].name) { needRebuild = true; break; }
      }
    }
    if (!needRebuild) {
      cards.forEach((card, i) => {
        const p = personas[i];
        const isSelected = p.name === currentPersona;
        card.className = `persona-card ${p.running ? 'running' : ''} ${isSelected ? 'selected' : ''}`;
        card.querySelector('.p-status').textContent = p.running ? '● 运行中' : (p.status === 'stale' ? '○ 心跳超时' : '○ 已停止');
        const hbEl = card.querySelector('.p-status').nextElementSibling;
        if (hbEl) hbEl.textContent = '心跳: ' + formatHeartbeat(p.heartbeat_at);
        const actions = card.querySelector('.p-actions');
        if (actions) {
          actions.innerHTML = (p.running
            ? `<button class="btn danger" onclick="event.stopPropagation(); stopPersona('${p.name}')">⏹ 停止</button>`
            : `<button class="btn success" onclick="event.stopPropagation(); startPersona('${p.name}')">▶ 启动</button>`)
            + `<button class="btn" onclick="event.stopPropagation(); selectPersona('${p.name}'); navTo('persona')">⚙️ 配置</button>`;
        }
      });
      $('dashPersonaCount').textContent = String(personas.length);
      $('dashRunningCount').textContent = String(personas.filter((p) => p.running).length);
      $('dashStoppedCount').textContent = String(personas.filter((p) => !p.running).length);
      return;
    }
    // 结构变化需要重建，先移除动画类避免新子元素触发 stagger
    el.classList.remove('animate-stagger');
  }

  el.innerHTML = personas.map((p) => {
    const port = p.adapters?.[0]?.ws_url?.split(':').pop() || '—';
    const hb = formatHeartbeat(p.heartbeat_at);
    const isSelected = p.name === currentPersona;
    return `
    <div class="persona-card ${p.running ? 'running' : ''} ${isSelected ? 'selected' : ''}" data-name="${p.name}" onclick="selectPersona('${p.name}')">
      <div class="p-port">端口 ${port}</div>
      <div class="p-name">${p.persona_name || p.name}</div>
      <div class="p-meta">${p.persona_summary || p.name}</div>
      <div class="p-status">${p.running ? '● 运行中' : (p.status === 'stale' ? '○ 心跳超时' : '○ 已停止')}</div>
      <div style="font-size:11px;color:var(--text-2);margin-bottom:8px">心跳: ${hb}</div>
      <div class="p-actions">
        ${p.running
          ? `<button class="btn danger" onclick="event.stopPropagation(); stopPersona('${p.name}')">⏹ 停止</button>`
          : `<button class="btn success" onclick="event.stopPropagation(); startPersona('${p.name}')">▶ 启动</button>`}
        <button class="btn" onclick="event.stopPropagation(); selectPersona('${p.name}'); navTo('persona')">⚙️ 配置</button>
        <button class="btn" onclick="event.stopPropagation(); selectPersona('${p.name}'); navTo('token-tracker')">📈 Token</button>
      </div>
    </div>
  `;
  }).join('');

  if (animate) applyStagger(el, '.persona-card');

  if (animate) {
    animateNumber($('dashPersonaCount'), personas.length, 500);
    animateNumber($('dashRunningCount'), personas.filter((p) => p.running).length, 500);
    animateNumber($('dashStoppedCount'), personas.filter((p) => !p.running).length, 500);
  } else {
    $('dashPersonaCount').textContent = String(personas.length);
    $('dashRunningCount').textContent = String(personas.filter((p) => p.running).length);
    $('dashStoppedCount').textContent = String(personas.filter((p) => !p.running).length);
  }

  // 更新选中人格详细信息
  const sp = personas.find((p) => p.name === currentPersona);
  const ds = $('dashSelectedInfo');
  if (sp) {
    ds.style.display = '';
    $('dsName').textContent = sp.persona_name || sp.name;
    $('dsSummary').textContent = sp.persona_summary || '暂无描述';
    $('dsTags').innerHTML = (sp.persona_summary || '').split(/[,，]/).filter(Boolean).slice(0,5).map((t) => `<span class="tag">${t.trim()}</span>`).join('');
    $('dsStatus').textContent = sp.running ? '运行中' : (sp.status === 'stale' ? '心跳超时' : '已停止');
    $('dsHeartbeat').textContent = formatHeartbeat(sp.heartbeat_at);
    $('dsPid').textContent = sp.pid || '—';
    $('dsAdapters').textContent = String(sp.adapters_count || 0);
    $('dsPort').textContent = sp.adapters?.[0]?.ws_url?.split(':').pop() || '—';
  } else {
    ds.style.display = 'none';
  }
}

function renderSectionBars(container, breakdown, breakdownByTask) {
  if (!container) return;
  const rawEntries = Object.entries(breakdown)
    .filter(([k]) => k !== 'total');
  let chart = echarts.getInstanceByDom(container);
  if (!rawEntries.length || typeof echarts === 'undefined') {
    if (chart) { chart.dispose(); window.removeEventListener('resize', container._sankeyResize); }
    container.innerHTML = '<div style="color:var(--text-2);padding:12px">暂无模块分布数据</div>';
    return;
  }

  const labels = {
    persona: '人格设定', identity: '身份识别', output_constraint: '输出约束',
    emotion: '情感上下文', empathy: '共情策略', relationship: '互动指导',
    memory: '记忆引用', interests: '用户兴趣', group_style: '群体风格',
    participants: '近期参与者', cross_group: '跨群认知', skills: '可用技能',
    glossary: '名词解释', output_format: '输出格式', diary: '日记记忆',
    history_xml: '对话历史', cross_group_xml: '跨群历史',
    system_prompt_total: '系统指令', user_message: '用户消息',
  };

  const groups = [
    { name: '人格与身份', keys: ['persona', 'identity'], color: '#58a6ff' },
    { name: '情感与关系', keys: ['emotion', 'empathy', 'relationship'], color: '#3fb950' },
    { name: '记忆与历史', keys: ['memory', 'diary', 'history_xml', 'cross_group_xml'], color: '#d29922' },
    { name: '环境与风格', keys: ['group_style', 'participants', 'cross_group', 'interests'], color: '#f85149' },
    { name: '功能与格式', keys: ['skills', 'glossary', 'output_format', 'output_constraint'], color: '#a371f7' },
    { name: '输入组成', keys: ['system_prompt_total', 'user_message'], color: '#e3b341' },
  ];

  const taskLabels = {
    response_generate: '主模型调用',
    cognition_analyze: '认知分析',
    diary_generate: '日记生成',
    diary_consolidate: '日记合并',
    proactive_generate: '主动生成',
    persona_generate: '人格生成',
    sticker_preference_generate: '表情包偏好生成',
    sticker_tag_extract: '表情包标签提取',
  };
  const taskColors = ['#58a6ff', '#3fb950', '#d29922', '#f85149', '#a371f7', '#e3b341'];

  const nodes = [{ name: '总输入', itemStyle: { color: '#ffffff' } }];
  const links = [];

  const hasTaskBreakdown = breakdownByTask && Object.keys(breakdownByTask).length > 1;

  if (hasTaskBreakdown) {
    // 4-level sankey: 总输入 → 任务 → 大类 → 子模块
    // 大类和子模块节点跨任务合并（不带后缀），任务层保留区分
    const taskNames = Object.keys(breakdownByTask);
    taskNames.forEach((taskName, ti) => {
      const taskLabel = taskLabels[taskName] || taskName;
      const taskColor = taskColors[ti % taskColors.length];
      const taskBreakdown = breakdownByTask[taskName];
      let taskSum = 0;

      groups.forEach((g) => {
        let groupSum = 0;
        g.keys.forEach((key) => {
          const val = taskBreakdown[key] || 0;
          if (val) {
            const label = labels[key] || key;
            // 子模块节点：跨任务合并（不带后缀）
            nodes.push({ name: label, itemStyle: { color: g.color } });
            links.push({ source: g.name, target: label, value: val });
            groupSum += val;
          }
        });
        if (groupSum) {
          // 大类节点：跨任务合并（不带后缀）
          nodes.push({ name: g.name, itemStyle: { color: g.color } });
          links.push({ source: taskLabel, target: g.name, value: groupSum });
          taskSum += groupSum;
        }
      });

      if (taskSum) {
        nodes.push({ name: taskLabel, itemStyle: { color: taskColor } });
        links.push({ source: '总输入', target: taskLabel, value: taskSum });
      }
    });
  } else {
    // 3-level sankey fallback (aggregate view)
    groups.forEach((g) => {
      let groupSum = 0;
      g.keys.forEach((key) => {
        const val = breakdown[key] || 0;
        if (val) {
          const label = labels[key] || key;
          nodes.push({ name: label, itemStyle: { color: g.color } });
          links.push({ source: g.name, target: label, value: val });
          groupSum += val;
        }
      });
      if (groupSum) {
        nodes.push({ name: g.name, itemStyle: { color: g.color } });
        links.push({ source: '总输入', target: g.name, value: groupSum });
      }
    });
  }

  if (!links.length) {
    container.innerHTML = '<div style="color:var(--text-2);padding:12px">暂无模块分布数据</div>';
    return;
  }

  // 去重 nodes：同名节点只保留一个（ECharts 按 name 聚合）
  const nodeMap = new Map();
  nodes.forEach((n) => { if (!nodeMap.has(n.name)) nodeMap.set(n.name, n); });
  const uniqueNodes = Array.from(nodeMap.values());

  // Sankey 数据结构变化大，每次重建实例避免增量更新内部状态错乱
  if (chart) {
    chart.dispose();
    window.removeEventListener('resize', container._sankeyResize);
  }
  chart = echarts.init(container, 'dark');
  const onResize = () => chart.resize();
  window.addEventListener('resize', onResize);
  container._sankeyResize = onResize;

  chart.setOption({
    backgroundColor: 'transparent',
    tooltip: {
      trigger: 'item',
      formatter: (params) => {
        if (params.dataType === 'edge') {
          return `${params.data.source} → ${params.data.target}<br/><b>${params.data.value.toLocaleString()} tokens</b>`;
        }
        return `<b>${params.name}</b>`;
      },
    },
    series: [{
      type: 'sankey',
      layout: 'none',
      emphasis: { focus: 'adjacency' },
      data: uniqueNodes,
      links: links,
      top: 10, bottom: 10, left: 10, right: hasTaskBreakdown ? 140 : 110,
      nodeWidth: hasTaskBreakdown ? 22 : 28,
      nodeGap: 10,
      layoutIterations: 32,
      lineStyle: { color: 'gradient', curveness: 0.5, opacity: 0.55 },
      label: {
        color: '#e8eaf0',
        fontSize: 11,
        formatter: (p) => p.name,
      },
      itemStyle: { borderWidth: 1, borderColor: '#0d1117' },
    }],
  });
}

function renderTimeSeries(container, hourly) {
  if (!container) return;
  let chart = echarts.getInstanceByDom(container);
  if (!hourly.length || typeof echarts === 'undefined') {
    if (chart) { chart.dispose(); window.removeEventListener('resize', container._tsResize); }
    container.innerHTML = '<div style="color:var(--text-2);padding:12px">暂无趋势数据</div>';
    return;
  }
  if (!chart) {
    chart = echarts.init(container, 'dark');
    const onResize = () => chart.resize();
    window.removeEventListener('resize', container._tsResize);
    window.addEventListener('resize', onResize);
    container._tsResize = onResize;
  }

  const dates = hourly.map((h) => new Date(h.hour_ts * 1000).toLocaleString('zh-CN', { month: 'short', day: 'numeric', hour: '2-digit' }));
  const promptData = hourly.map((h) => h.prompt_tokens || 0);
  const completionData = hourly.map((h) => h.completion_tokens || 0);

  chart.setOption({
    backgroundColor: 'transparent',
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'cross', label: { backgroundColor: '#6a7985' } },
    },
    legend: { data: ['Prompt', 'Completion'], textStyle: { color: '#c9d1d9', fontSize: 11 }, top: 0 },
    grid: { left: 10, right: 10, bottom: 10, top: 32, containLabel: true },
    xAxis: {
      type: 'category',
      boundaryGap: false,
      data: dates,
      axisLabel: { fontSize: 10, color: '#8b949e', rotate: 30 },
      axisLine: { lineStyle: { color: '#30363d' } },
    },
    yAxis: {
      type: 'value',
      axisLabel: { fontSize: 10, color: '#8b949e' },
      splitLine: { lineStyle: { color: '#21262d' } },
    },
    series: [
      {
        name: 'Prompt',
        type: 'line',
        smooth: true,
        showSymbol: false,
        areaStyle: { opacity: 0.15 },
        lineStyle: { width: 2 },
        itemStyle: { color: '#58a6ff' },
        data: promptData,
      },
      {
        name: 'Completion',
        type: 'line',
        smooth: true,
        showSymbol: false,
        areaStyle: { opacity: 0.15 },
        lineStyle: { width: 2 },
        itemStyle: { color: '#3fb950' },
        data: completionData,
      },
    ],
  }, true);
}

const _EMOTION_CN = {
  JOY: '喜悦', CONTENTMENT: '满足', RELIEF: '释然', EXCITEMENT: '兴奋',
  SADNESS: '悲伤', GRIEF: '悲痛', ANGER: '愤怒', IRRITATION: '恼怒',
  ANXIETY: '焦虑', LONELINESS: '孤独', FEAR: '恐惧', DISGUST: '厌恶',
  SURPRISE: '惊讶', TRUST: '信任', ANTICIPATION: '期待', LOVE: '喜爱',
  GRATITUDE: '感激', HOPE: '希望', NEUTRAL: '中性',
  CURIOSITY: '好奇', CONFUSION: '困惑',
  unknown: '未知', '': '未知',
};

function _emotionCn(name) {
  return _EMOTION_CN[name] || name;
}

function renderEmotionDistribution(container, distribution) {
  if (!container) return;
  let chart = echarts.getInstanceByDom(container);
  if (!Object.keys(distribution).length || typeof echarts === 'undefined') {
    if (chart) { chart.dispose(); window.removeEventListener('resize', container._edResize); }
    container.innerHTML = '<div style="color:var(--text-2);padding:12px">暂无情感分布数据</div>';
    return;
  }
  if (!chart) {
    chart = echarts.init(container, 'dark');
    const onResize = () => chart.resize();
    window.removeEventListener('resize', container._edResize);
    window.addEventListener('resize', onResize);
    container._edResize = onResize;
  }

  // Sort by count desc, translate labels
  const sorted = Object.entries(distribution)
    .map(([name, value]) => ({ name: _emotionCn(name) || '未知', value, raw: name }))
    .sort((a, b) => b.value - a.value);
  const colors = ['#58a6ff', '#3fb950', '#d29922', '#f85149', '#a371f7', '#e3b341', '#8b949e'];

  const total = sorted.reduce((s, d) => s + d.value, 0);

  chart.setOption({
    backgroundColor: 'transparent',
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      formatter: (params) => {
        const p = params[0];
        const pct = total ? ((p.value / total) * 100).toFixed(1) : 0;
        return `<b>${p.name}</b><br/>${p.value} 次（占比 ${pct}%）`;
      },
    },
    grid: { left: 10, right: 24, bottom: 10, top: 10, containLabel: true },
    xAxis: {
      type: 'value',
      axisLabel: { fontSize: 10, color: '#8b949e' },
      splitLine: { lineStyle: { color: '#21262d' } },
    },
    yAxis: {
      type: 'category',
      data: sorted.map((d) => d.name),
      axisLabel: { fontSize: 11, color: '#c9d1d9' },
      axisLine: { lineStyle: { color: '#30363d' } },
      axisTick: { show: false },
    },
    series: [{
      type: 'bar',
      data: sorted.map((d, i) => ({
        value: d.value,
        itemStyle: { color: colors[i % colors.length], borderRadius: [0, 4, 4, 0] },
      })),
      barWidth: '60%',
      label: {
        show: true,
        position: 'right',
        fontSize: 11,
        color: '#c9d1d9',
        formatter: (p) => `${p.value} 次`,
      },
    }],
  }, true);
}

function renderEmotionTimeline(container, events) {
  if (!container) return;
  let chart = echarts.getInstanceByDom(container);
  if (!events.length || typeof echarts === 'undefined') {
    if (chart) { chart.dispose(); window.removeEventListener('resize', container._etResize); }
    container.innerHTML = '<div style="color:var(--text-2);padding:12px">暂无情感时间线数据</div>';
    return;
  }
  if (!chart) {
    chart = echarts.init(container, 'dark');
    const onResize = () => chart.resize();
    window.removeEventListener('resize', container._etResize);
    window.addEventListener('resize', onResize);
    container._etResize = onResize;
  }

  const sorted = [...events].sort((a, b) => (a.timestamp || 0) - (b.timestamp || 0));
  const dates = sorted.map((e) => new Date((e.timestamp || 0) * 1000).toLocaleString('zh-CN', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }));
  const valenceData = sorted.map((e) => e.valence || 0);
  const arousalData = sorted.map((e) => e.arousal || 0);
  const intensityData = sorted.map((e) => e.intensity || 0);

  chart.setOption({
    backgroundColor: 'transparent',
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'cross', label: { backgroundColor: '#6a7985' } },
      formatter: (params) => {
        const idx = params[0].dataIndex;
        const ev = sorted[idx];
        const emotionLabel = ev ? (_emotionCn(ev.basic_emotion) || '未知') : '';
        let html = `<div style="font-size:12px;margin-bottom:4px">${params[0].axisValue}${emotionLabel ? ' · 情感: ' + emotionLabel : ''}</div>`;
        const map = {
          '愉悦度': { high: '积极/愉快', low: '消极/不快', unit: '' },
          '唤醒度': { high: '兴奋/激动', low: '平静/低落', unit: '' },
          '情感强度': { high: '情绪强烈', low: '情绪平淡', unit: '' },
        };
        for (const p of params) {
          const info = map[p.seriesName] || {};
          const v = p.value;
          const desc = v > 0.3 ? info.high : (v < -0.3 ? info.low : '中性');
          html += `<div style="display:flex;align-items:center;gap:6px;margin:2px 0">
            <span style="display:inline-block;width:8px;height:8px;border-radius:50%;background:${p.color}"></span>
            <span style="min-width:56px">${p.seriesName}</span>
            <strong>${v.toFixed(2)}</strong>
            <span style="color:#8b949e;font-size:11px">(${desc})</span>
          </div>`;
        }
        return html;
      },
    },
    legend: { data: ['愉悦度', '唤醒度', '情感强度'], textStyle: { color: '#c9d1d9', fontSize: 11 }, top: 0 },
    grid: { left: 10, right: 10, bottom: 10, top: 36, containLabel: true },
    xAxis: {
      type: 'category',
      data: dates,
      axisLabel: { fontSize: 10, color: '#8b949e', rotate: 30 },
      axisLine: { lineStyle: { color: '#30363d' } },
    },
    yAxis: {
      type: 'value',
      min: -1, max: 1,
      axisLabel: {
        fontSize: 10, color: '#8b949e',
        formatter: (v) => {
          if (v >= 0.8) return '高 +' + v.toFixed(1);
          if (v <= -0.8) return '低 ' + v.toFixed(1);
          if (Math.abs(v) < 0.1) return '中 0';
          return v.toFixed(1);
        },
      },
      splitLine: { lineStyle: { color: '#21262d' } },
    },
    series: [
      { name: '愉悦度', type: 'line', smooth: true, showSymbol: false, lineStyle: { width: 2 }, itemStyle: { color: '#58a6ff' }, data: valenceData },
      { name: '唤醒度', type: 'line', smooth: true, showSymbol: false, lineStyle: { width: 2 }, itemStyle: { color: '#f85149' }, data: arousalData },
      { name: '情感强度', type: 'line', smooth: true, showSymbol: false, lineStyle: { width: 2, type: 'dashed' }, itemStyle: { color: '#e3b341' }, data: intensityData },
    ],
  }, true);
}

function renderActiveHours(container, distribution) {
  if (!container) return;
  let chart = echarts.getInstanceByDom(container);
  if (!distribution.length || typeof echarts === 'undefined') {
    if (chart) { chart.dispose(); window.removeEventListener('resize', container._ahResize); }
    container.innerHTML = '<div style="color:var(--text-2);padding:12px">暂无活跃时段数据</div>';
    return;
  }
  if (!chart) {
    chart = echarts.init(container, 'dark');
    const onResize = () => chart.resize();
    window.removeEventListener('resize', container._ahResize);
    window.addEventListener('resize', onResize);
    container._ahResize = onResize;
  }

  const hours = Array.from({ length: 24 }, (_, i) => `${i}时`);
  const callsMap = Object.fromEntries(distribution.map((d) => [d.hour, d.calls || 0]));
  const data = hours.map((_, i) => callsMap[i] || 0);

  chart.setOption({
    backgroundColor: 'transparent',
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    grid: { left: 10, right: 10, bottom: 10, top: 10, containLabel: true },
    xAxis: {
      type: 'category',
      data: hours,
      axisLabel: { fontSize: 10, color: '#8b949e', interval: 2 },
      axisLine: { lineStyle: { color: '#30363d' } },
    },
    yAxis: {
      type: 'value',
      axisLabel: { fontSize: 10, color: '#8b949e' },
      splitLine: { lineStyle: { color: '#21262d' } },
    },
    series: [{
      type: 'bar',
      data,
      barWidth: '60%',
      itemStyle: {
        borderRadius: [3, 3, 0, 0],
        color: new echarts.graphic.LinearGradient(0, 0, 0, 1, [
          { offset: 0, color: '#58a6ff' },
          { offset: 1, color: '#1f6feb' },
        ]),
      },
    }],
  }, true);
}

function _renderExtraStats(prefix, res) {
  const ratioEl = $(`${prefix}Ratio`);
  const effEl = $(`${prefix}Efficiency`);
  const retryEl = $(`${prefix}RetryRate`);
  const durEl = $(`${prefix}AvgDuration`);
  const outRatioEl = $(`${prefix}OutputRatio`);
  const emptyEl = $(`${prefix}EmptyRate`);
  const emptyTabEl = $(`${prefix}EmptyRateTab`);
  const bloatEl = $(`${prefix}BloatAlert`);
  const failEl = $(`${prefix}FailureRate`);
  const depthEl = $(`${prefix}AvgDepth`);
  const maxDepthEl = $(`${prefix}MaxDepth`);

  const ratio = res.ratio || {};
  if (ratioEl) ratioEl.textContent = `${ratio.prompt_pct || 0}% / ${ratio.completion_pct || 0}%`;

  const eff = res.efficiency_stats || {};
  if (effEl) effEl.textContent = eff.chars_per_token ? `${eff.chars_per_token} 字/Token` : '—';

  const retry = res.retry_stats || {};
  if (retryEl) retryEl.textContent = retry.retry_rate_pct ? `${retry.retry_rate_pct}%` : '—';

  const dur = res.duration_stats || {};
  const overall = dur.overall || {};
  if (durEl) durEl.textContent = overall.avg_ms ? `${overall.avg_ms} ms` : '—';

  if (outRatioEl) outRatioEl.textContent = eff.output_ratio ? `${eff.output_ratio}` : '—';

  const empty = res.empty_reply_stats || {};
  const emptyText = empty.empty_rate_pct ? `${empty.empty_rate_pct}%` : '—';
  if (emptyEl) emptyEl.textContent = emptyText;
  if (emptyTabEl) emptyTabEl.textContent = emptyText;

  const fail = res.failure_stats || {};
  if (failEl) {
    const fr = fail.failure_rate_pct || 0;
    failEl.textContent = fr ? `${fr}%` : '—';
    failEl.style.color = fr > 5 ? 'var(--danger)' : (fr > 1 ? 'var(--warning)' : '');
  }

  const depth = res.depth_stats || {};
  if (depthEl) depthEl.textContent = depth.avg_depth ? `${depth.avg_depth}` : '—';
  if (maxDepthEl) maxDepthEl.textContent = depth.max_depth ? `最大 ${depth.max_depth}` : '—';

  const comp = res.period_comparison || {};
  if (bloatEl) {
    const chg = comp.change_total_tokens || 0;
    const calls = comp.current?.total_calls || 0;
    if (!calls) {
      bloatEl.textContent = '—';
      bloatEl.style.color = '';
    } else if (chg > 20) {
      bloatEl.textContent = `↑ ${chg}%`;
      bloatEl.style.color = 'var(--danger)';
    } else if (chg < -20) {
      bloatEl.textContent = `↓ ${Math.abs(chg)}%`;
      bloatEl.style.color = 'var(--success)';
    } else {
      bloatEl.textContent = `${chg > 0 ? '+' : ''}${chg}%`;
      bloatEl.style.color = 'var(--text-2)';
    }
  }
}

async function loadTokenStats() {
  const callsEl = $('dashTokenCalls');
  const promptEl = $('dashTokenPrompt');
  const completionEl = $('dashTokenCompletion');
  const totalEl = $('dashTokenTotal');
  const avgEl = $('dashTokenAvgRound');
  const avgDetailEl = $('dashTokenAvgRoundDetail');
  if (!callsEl || !totalEl) return;
  try {
    const res = await get('/tokens');
    const dataKey = JSON.stringify(res);
    if (_lastTokenData === dataKey) return;
    _lastTokenData = dataKey;

    const summary = res.summary || {};
    animateNumber(callsEl, summary.total_calls || 0, 500);
    animateNumber(promptEl, summary.total_prompt_tokens || 0, 500);
    animateNumber(completionEl, summary.total_completion_tokens || 0, 500);
    animateNumber(totalEl, summary.total_tokens || 0, 500);
    const avg = res.response_avg || {};
    if (avgEl) animateNumber(avgEl, avg.avg_total_tokens || 0, 500);
    if (avgDetailEl) {
      const calls = avg.total_calls || 0;
      avgDetailEl.textContent = calls ? `${calls} 次回复 · ${(avg.avg_prompt_tokens || 0).toLocaleString()} + ${(avg.avg_completion_tokens || 0).toLocaleString()}` : '暂无回复记录';
    }
  } catch (e) {
    _lastTokenData = null;
  }
}

async function loadTelemetry() {
  const container = $('dashSkillStats') || $('skillsTelemetryStats');
  const totalEl = $('dashSkillTotalCalls') || $('skillsTelemetryTotalCalls');
  if (!container) return;
  try {
    const res = await get('/telemetry');
    const dataKey = JSON.stringify(res);
    if (_lastTelemetryData === dataKey) return; // 数据未变化，跳过重建
    _lastTelemetryData = dataKey;

    const skills = res.skills || {};
    const total = res.total_calls || 0;
    if (totalEl) totalEl.textContent = String(total);
    const names = Object.keys(skills).sort((a, b) => skills[b].calls - skills[a].calls);
    if (!names.length) {
      container.innerHTML = '<div style="color:var(--text-2);padding:12px">暂无 Skill 调用记录</div>';
      return;
    }
    container.innerHTML = names.map((name) => {
      const s = skills[name];
      const successRate = s.success_rate || 0;
      const color = successRate >= 95 ? 'var(--success)' : successRate >= 80 ? 'var(--warning)' : 'var(--danger)';
      return `
        <div class="stat-card">
          <div class="label">${name}</div>
          <div class="value">${s.calls}</div>
          <div style="font-size:11px;color:var(--text-2)">
            成功率 <span style="color:${color}">${successRate}%</span> &nbsp;|&nbsp; 平均 ${s.avg_ms || 0}ms
          </div>
        </div>
      `;
    }).join('');
    applyStagger(container, '.stat-card');
  } catch (e) {
    _lastTelemetryData = null;
    container.innerHTML = '<div style="color:var(--text-2);padding:12px">统计加载失败</div>';
  }
}

async function startPersona(name) {
  const res = await post(`/personas/${name}/start`, {});
  toast(res.success ? `人格 ${name} 已启动` : res.error || '启动失败', res.success ? 'success' : 'error');
  loadPersonas();
}

async function stopPersona(name) {
  const res = await post(`/personas/${name}/stop`, {});
  toast(res.success ? `人格 ${name} 已停止` : res.error || '停止失败', res.success ? 'success' : 'error');
  loadPersonas();
}

// ── Page Loader Registrations (core) ──────────────────
registerPageLoader('dashboard', {
  init: async () => { renderPersonaCards(); loadProviders(); _lastTelemetryData = null; _lastTokenData = null; loadTokenStats(); },
  refresh: () => { renderPersonaCards(); },
});
