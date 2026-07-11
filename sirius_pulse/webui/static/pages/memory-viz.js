import { store } from '../store.js';
import { get, post, put, del } from '../app.js';
import { confirmDanger, toast, statCard } from '../components.js';
import { setChartOption, getChart } from '../charts.js';
import { createPageContext, createScopedPage } from '../page-context.js';

const scopedPage = createScopedPage();
const $ = scopedPage.$;

const ROLE_COLORS = {
  human: '#58a6ff',
  assistant: '#3fb950',
  system: '#a371f7',
};

const TABS = [
  { id: 'conversations', label: '基础记忆', hint: '近期对话', canCreate: false },
  { id: 'diary', label: '日记记忆', hint: '长期总结', canCreate: true },
  { id: 'units', label: '记忆单元', hint: 'MemoryUnit', canCreate: true },
  { id: 'glossary', label: '名词解释', hint: '概念词典', canCreate: true },
  { id: 'users', label: '用户画像', hint: '语义档案', canCreate: true },
];

const TAB_ENDPOINTS = {
  diary: '/persona/diary?limit=200',
  units: '/persona/memory-units?limit=200',
  glossary: '/persona/glossary?limit=200',
  users: '/persona/users?limit=200',
};

let state = {
  tab: 'conversations',
  search: '',
  group: '',
  data: { diary: null, units: null, glossary: null, users: null },
  viz: null,
  loadedTabs: new Set(),
  loadingTabs: new Set(),
  vizLoaded: false,
  vizLoading: false,
};

let conversationAnalysisModule = null;
let conversationAnalysisContext = null;
let conversationAnalysisMounted = false;

export function dispose() {
  closeDedupeModal();
  disposeConversationAnalysis();
  scopedPage.use(null, null);
}

export async function init(container, params = {}) {
  scopedPage.use(params?.ctx, container);
  if (!store.currentPersona) {
    container.innerHTML = `
      <div class="card">
        <div style="padding:60px;text-align:center;color:var(--text-3)">请先选择人格</div>
      </div>
    `;
    return;
  }

  container.innerHTML = `
    <style>
      .memory-hero {
        display:grid;
        grid-template-columns:minmax(0,1fr) auto;
        gap:16px;
        align-items:end;
        margin-bottom:18px;
        padding:22px;
        border:1px solid color-mix(in srgb, var(--accent) 26%, var(--border));
        border-radius:18px;
        background:
          radial-gradient(circle at 12% 18%, color-mix(in srgb, var(--accent) 24%, transparent) 0, transparent 32%),
          linear-gradient(135deg, color-mix(in srgb, var(--bg-2) 86%, var(--accent) 14%), var(--bg-1));
        box-shadow:0 18px 48px rgba(0,0,0,0.22);
      }
      .memory-kicker {
        display:inline-flex;
        align-items:center;
        gap:6px;
        margin-bottom:8px;
        padding:4px 10px;
        border:1px solid color-mix(in srgb, var(--accent) 34%, var(--border));
        border-radius:999px;
        color:var(--accent);
        background:color-mix(in srgb, var(--accent) 10%, transparent);
        font-size:12px;
        font-weight:600;
      }
      .memory-title {
        font-size:30px;
        font-weight:800;
        letter-spacing:0;
        color:var(--text-1);
      }
      .memory-subtitle {
        margin-top:4px;
        color:var(--text-3);
        font-size:13px;
      }
      .memory-toolbar {
        display:flex;
        gap:8px;
        flex-wrap:wrap;
        align-items:center;
        justify-content:flex-end;
        padding:10px;
        border:1px solid rgba(255,255,255,0.08);
        border-radius:14px;
        background:rgba(0,0,0,0.16);
        backdrop-filter:blur(10px);
      }
      .memory-tabs {
        display:grid;
        grid-template-columns:repeat(5,minmax(0,1fr));
        gap:8px;
        margin:18px 0;
      }
      .memory-tab {
        border:1px solid var(--border);
        background:linear-gradient(180deg, color-mix(in srgb, var(--bg-2) 94%, var(--accent) 6%), var(--bg-2));
        color:var(--text-2);
        border-radius:14px;
        padding:12px;
        cursor:pointer;
        text-align:left;
        min-width:0;
        transition:transform .16s ease, border-color .16s ease, box-shadow .16s ease;
      }
      .memory-tab:hover {
        transform:translateY(-1px);
        border-color:color-mix(in srgb, var(--accent) 44%, var(--border));
      }
      .memory-tab.active {
        border-color:var(--accent);
        background:var(--accent-dim);
        color:var(--text-1);
        box-shadow:0 10px 28px color-mix(in srgb, var(--accent) 18%, transparent);
      }
      .memory-tab-name {
        display:block;
        font-weight:600;
        font-size:13px;
      }
      .memory-tab-hint {
        display:block;
        color:var(--text-3);
        font-size:11px;
        margin-top:2px;
      }
      .memory-workspace {
        display:grid;
        grid-template-columns:minmax(0,1fr) 340px;
        gap:16px;
        align-items:start;
      }
      .memory-list {
        display:flex;
        flex-direction:column;
        gap:10px;
      }
      .memory-item {
        border:1px solid var(--border);
        border-radius:14px;
        background:color-mix(in srgb, var(--bg-2) 92%, var(--accent) 8%);
        padding:14px;
        transition:transform .16s ease, border-color .16s ease, background .16s ease;
      }
      .memory-item:hover {
        transform:translateY(-1px);
        border-color:color-mix(in srgb, var(--accent) 38%, var(--border));
        background:color-mix(in srgb, var(--bg-2) 86%, var(--accent) 14%);
      }
      .memory-item-head {
        display:flex;
        justify-content:space-between;
        gap:12px;
        align-items:flex-start;
        margin-bottom:8px;
      }
      .memory-item-title {
        color:var(--text-1);
        font-weight:600;
        word-break:break-word;
      }
      .memory-item-meta {
        display:flex;
        flex-wrap:wrap;
        gap:6px;
        color:var(--text-3);
        font-size:11px;
        margin-top:4px;
      }
      .memory-pill {
        display:inline-flex;
        align-items:center;
        max-width:180px;
        border:1px solid var(--border);
        border-radius:999px;
        padding:2px 8px;
        color:var(--text-2);
        background:var(--bg-1);
        font-size:11px;
        overflow:hidden;
        text-overflow:ellipsis;
        white-space:nowrap;
      }
      .memory-content {
        color:var(--text-2);
        line-height:1.7;
        white-space:pre-wrap;
        word-break:break-word;
      }
      .memory-actions {
        display:flex;
        gap:6px;
        flex-shrink:0;
      }
      .memory-side {
        position:sticky;
        top:76px;
      }
      .memory-side .card,
      .memory-workspace > .card,
      .memory-chart-grid > .card {
        border-radius:16px;
      }
      .memory-form-grid {
        display:grid;
        gap:10px;
      }
      .memory-form-grid label {
        font-size:12px;
        color:var(--text-3);
        margin-bottom:4px;
      }
      .memory-empty {
        padding:52px 16px;
        color:var(--text-3);
        text-align:center;
        border:1px dashed var(--border);
        border-radius:8px;
        background:var(--bg-2);
      }
      .memory-chart-grid {
        display:grid;
        grid-template-columns:1fr 1fr;
        gap:16px;
        margin-top:16px;
      }
      @media (max-width: 1100px) {
        .memory-workspace,
        .memory-chart-grid,
        .memory-hero {
          grid-template-columns:1fr;
        }
        .memory-side {
          position:static;
        }
      }
      @media (max-width: 760px) {
        .memory-tabs {
          grid-template-columns:repeat(2,minmax(0,1fr));
        }
      }
    </style>

    <div class="memory-hero">
      <div>
        <div class="memory-kicker">新记忆模块 · CRUD</div>
        <div class="memory-title">记忆管理工作台</div>
        <div class="memory-subtitle">面向当前运行中的记忆系统：统一检索基础记忆、记忆单元、日记、术语和用户画像，并直接新增、编辑、删除可维护条目。</div>
      </div>
      <div class="memory-toolbar">
        <select id="memoryGroupFilter" style="width:160px"></select>
        <input id="memorySearch" type="search" placeholder="搜索记忆内容..." style="width:220px">
        <button class="btn btn-primary" id="memoryCreateBtn">新增</button>
        <button class="btn" id="memoryRefreshBtn">刷新</button>
        <button class="btn" id="memoryDedupeBtn" style="display:none">清理重复</button>
      </div>
    </div>

    <div class="stat-grid" id="memoryStats"></div>
    <div class="memory-tabs" id="memoryTabs"></div>

    <div id="memoryCrudView">
      <div class="memory-workspace">
        <div class="card">
          <div class="card-header">
            <div>
              <div class="card-title" id="memoryListTitle">记忆列表</div>
              <div class="card-subtitle" id="memoryListSubtitle">按当前筛选展示</div>
            </div>
          </div>
          <div id="memoryList" class="memory-list"></div>
        </div>
        <div class="memory-side">
          <div class="card">
            <div class="card-header">
              <div>
                <div class="card-title" id="memoryFormTitle">选择一条记忆</div>
                <div class="card-subtitle" id="memoryFormSubtitle">可编辑的记忆会在这里打开</div>
              </div>
            </div>
            <div id="memoryForm"></div>
          </div>
        </div>
      </div>
    </div>

    <div id="basicMemoryAnalysisView" style="display:none">
      <div class="card" style="margin-bottom:16px">
        <div class="card-header">
          <div>
            <div class="card-title">基础记忆 · 对话分析</div>
            <div class="card-subtitle">完整历史对话分析、检索、群组/发言人筛选、钉住消息、链路展开与删除能力。</div>
          </div>
        </div>
      </div>
      <div id="basicMemoryConversationAnalysis"></div>
    </div>

    <div class="memory-chart-grid" id="memoryChartsView">
      <div class="card">
        <div class="card-header">
          <div>
            <div class="card-title">基础记忆时间线</div>
            <div class="card-subtitle">按天统计用户、AI 与系统消息</div>
          </div>
          <select id="timelineRange" class="btn btn-sm" style="width:120px">
            <option value="7">近 7 天</option>
            <option value="30" selected>近 30 天</option>
            <option value="90">近 90 天</option>
            <option value="0">全部</option>
          </select>
        </div>
        <div id="timelineChart" class="chart-container" style="min-height:320px"></div>
      </div>
      <div class="card">
        <div class="card-header">
          <div>
            <div class="card-title">日记语义聚类</div>
            <div class="card-subtitle">基于 embedding 的主题分布</div>
          </div>
        </div>
        <div id="clusterChart" class="chart-container" style="min-height:320px"></div>
      </div>
    </div>
  `;

  bindEvents();
  renderTabs();
  closeEditor();
  renderAll();
  await loadActiveTab();
}

function bindEvents() {
  $('memoryRefreshBtn')?.addEventListener('click', refreshActiveTab);
  $('memoryDedupeBtn')?.addEventListener('click', openDedupeModal);
  $('memoryCreateBtn')?.addEventListener('click', () => openEditor(state.tab, null));
  $('memorySearch')?.addEventListener('input', (event) => {
    state.search = event.target.value.trim().toLowerCase();
    renderAll();
  });
  $('memoryGroupFilter')?.addEventListener('change', (event) => {
    state.group = event.target.value;
    renderAll();
    if (state.vizLoaded) renderTimeline(state.viz?.basic_timeline || {});
  });
  $('timelineRange')?.addEventListener('change', () => renderTimeline(state.viz?.basic_timeline || {}));
}

async function refreshActiveTab() {
  if (state.tab === 'conversations') {
    disposeConversationAnalysis();
    renderAll();
    await mountConversationAnalysis();
    return;
  }
  await loadActiveTab({ force: true });
}

async function loadActiveTab({ force = false } = {}) {
  const tab = state.tab;
  if (!scopedPage.isActive()) return;
  if (tab === 'conversations') {
    renderAll();
    await mountConversationAnalysis();
    return;
  }

  const endpoint = TAB_ENDPOINTS[tab];
  if (!endpoint) return;
  if (!force && state.loadedTabs.has(tab)) {
    renderAll();
    await loadChartsForActiveTab(tab);
    return;
  }

  state.loadingTabs.add(tab);
  renderAll();
  try {
    state.data[tab] = await get(endpoint);
    if (!scopedPage.isActive() || state.tab !== tab) return;
    state.loadedTabs.add(tab);
    await loadChartsForActiveTab(tab, { force });
  } catch (error) {
    if (error?.name === 'AbortError') return;
    if (!scopedPage.isActive()) return;
    toast('加载记忆数据失败: ' + error.message, 'error');
  } finally {
    state.loadingTabs.delete(tab);
    if (scopedPage.isActive() && state.tab === tab) renderAll();
  }
}

async function loadChartsForActiveTab(tab = state.tab, { force = false } = {}) {
  if (!scopedPage.isActive() || state.tab !== tab) return;
  if (tab !== 'diary') return;
  if (!force && state.vizLoaded) {
    renderTimeline(state.viz?.basic_timeline || {});
    renderCluster(state.viz?.diary_entries || []);
    return;
  }
  if (state.vizLoading) return;
  state.vizLoading = true;
  try {
    state.viz = await get('/persona/memory-viz');
    if (!scopedPage.isActive() || state.tab !== tab) return;
    state.vizLoaded = true;
    renderTimeline(state.viz.basic_timeline || {});
    renderCluster(state.viz.diary_entries || []);
  } catch (error) {
    if (error?.name === 'AbortError') return;
    if (!scopedPage.isActive()) return;
    toast('加载记忆图表失败: ' + error.message, 'error');
  } finally {
    state.vizLoading = false;
  }
}

function renderAll() {
  renderTabs();
  renderStats();
  renderGroupFilter();
  renderList();
  updateCreateButton();
  updateConversationAnalysisView();
}

function renderTabs() {
  const el = $('memoryTabs');
  if (!el) return;
  el.innerHTML = TABS.map((tab) => `
    <button class="memory-tab ${state.tab === tab.id ? 'active' : ''}" data-tab="${tab.id}">
      <span class="memory-tab-name">${tab.label}</span>
      <span class="memory-tab-hint">${tab.hint}</span>
    </button>
  `).join('');
  el.querySelectorAll('[data-tab]').forEach((btn) => {
    btn.addEventListener('click', () => {
      state.tab = btn.dataset.tab;
      closeEditor();
      renderAll();
      loadActiveTab();
    });
  });
}

function renderStats() {
  const data = state.data;
  const el = $('memoryStats');
  if (!el) return;
  const count = (tab, value) => state.loadingTabs.has(tab) ? '加载中' : state.loadedTabs.has(tab) ? (value || 0) : '未加载';
  el.innerHTML = [
    statCard('基础记忆', '按需', '打开后加载完整对话分析', '◲'),
    statCard('日记条目', count('diary', data.diary?.total), '可新增、编辑、删除', '◫'),
    statCard('记忆单元', count('units', data.units?.total), '检查点提炼', '▣'),
    statCard('术语数量', count('glossary', data.glossary?.total), '人格级词典', '◱'),
    statCard('用户画像', count('users', data.users?.total), '按群组维护', '◎'),
  ].join('');
}

function renderGroupFilter() {
  const select = $('memoryGroupFilter');
  if (!select) return;
  const groups = new Set();
  if (state.tab === 'diary') (state.data.diary?.groups || []).forEach((g) => groups.add(g));
  if (state.tab === 'units') (state.data.units?.groups || []).forEach((g) => groups.add(g));
  if (state.tab === 'users') (state.data.users?.groups || []).forEach((g) => groups.add(g));
  const current = state.group;
  select.innerHTML = '<option value="">全部群组</option>' +
    [...groups].sort().map((g) => `<option value="${escapeHtml(g)}">${escapeHtml(g)}</option>`).join('');
  select.value = [...groups].includes(current) ? current : '';
  state.group = select.value;
  select.disabled = !groups.size;
}

function updateCreateButton() {
  const btn = $('memoryCreateBtn');
  const tab = TABS.find((item) => item.id === state.tab);
  if (!btn || !tab) return;
  btn.style.display = tab.canCreate ? '' : 'none';
  btn.textContent = state.tab === 'users' ? '新增画像' : state.tab === 'units' ? '新增单元' : '新增记忆';
  const dedupeBtn = $('memoryDedupeBtn');
  if (dedupeBtn) dedupeBtn.style.display = state.tab === 'units' ? '' : 'none';
}

let dedupePollTimer = null;
let dedupeStatus = { status: 'idle' };

function closeDedupeModal() { if (dedupePollTimer) clearInterval(dedupePollTimer); dedupePollTimer = null; document.getElementById('memoryDedupeModal')?.remove(); }
async function openDedupeModal() {
  closeDedupeModal(); const overlay = document.createElement('div'); overlay.id = 'memoryDedupeModal'; overlay.className = 'modal-overlay';
  overlay.innerHTML = '<div class="modal"><div class="modal-header">重复记忆扫描</div><div class="modal-body" id="memoryDedupeBody"></div><div class="modal-footer" id="memoryDedupeFooter"></div></div>'; document.body.appendChild(overlay);
  dedupeStatus = await get('/persona/memory-units/dedupe/status'); renderDedupeStatus(); if (['queued', 'scanning', 'applying'].includes(dedupeStatus.status)) startDedupePolling();
}
function startDedupePolling() { if (dedupePollTimer) clearInterval(dedupePollTimer); dedupePollTimer = setInterval(pollDedupeStatus, 1000); }
async function pollDedupeStatus() { dedupeStatus = await get('/persona/memory-units/dedupe/status'); if (['ready', 'completed', 'stale', 'failed'].includes(dedupeStatus.status)) { clearInterval(dedupePollTimer); dedupePollTimer = null; } renderDedupeStatus(); if (dedupeStatus.status === 'completed') await loadActiveTab({ force: true }); }
function renderDedupeStatus() {
  const body = $('memoryDedupeBody'); const footer = $('memoryDedupeFooter'); if (!body || !footer) return;
  const labels = { idle: '尚未扫描', queued: '等待执行', scanning: '正在扫描', ready: '扫描完成', applying: '正在应用', completed: '清理完成', stale: '记忆数据已变化，请重新扫描', failed: '任务失败' };
  body.textContent = labels[dedupeStatus.status] || dedupeStatus.status;
  footer.innerHTML = `<button class="btn" id="memoryDedupeScan">扫描重复</button>${dedupeStatus.status === 'ready' ? '<button class="btn btn-danger" id="memoryDedupeApply">应用清理</button>' : ''}`;
  $('memoryDedupeScan')?.addEventListener('click', async () => { dedupeStatus = await post('/persona/memory-units/dedupe/scan', {}); renderDedupeStatus(); startDedupePolling(); });
  $('memoryDedupeApply')?.addEventListener('click', async () => { if (confirmDanger('确定应用本次重复记忆清理吗？系统会先创建完整备份。')) { await post('/persona/memory-units/dedupe/apply', { job_id: dedupeStatus.job_id }); dedupeStatus.status = 'applying'; renderDedupeStatus(); startDedupePolling(); } });
}

function updateConversationAnalysisView() {
  const isConversationsTab = state.tab === 'conversations';
  const isDiaryTab = state.tab === 'diary';
  const crudView = $('memoryCrudView');
  const analysisView = $('basicMemoryAnalysisView');
  const chartsView = $('memoryChartsView');
  const groupFilter = $('memoryGroupFilter');
  const searchInput = $('memorySearch');

  if (crudView) crudView.style.display = isConversationsTab ? 'none' : '';
  if (analysisView) analysisView.style.display = isConversationsTab ? '' : 'none';
  if (chartsView) chartsView.style.display = isDiaryTab ? '' : 'none';
  if (groupFilter) groupFilter.style.display = isConversationsTab ? 'none' : '';
  if (searchInput) searchInput.style.display = isConversationsTab ? 'none' : '';
  if (searchInput) searchInput.placeholder = state.tab === 'users' ? '搜索用户画像...' : '搜索记忆内容...';

  if (isConversationsTab) {
    mountConversationAnalysis();
  } else {
    disposeConversationAnalysis();
  }
}

async function mountConversationAnalysis() {
  const container = $('basicMemoryConversationAnalysis');
  if (!container || conversationAnalysisMounted) return;
  conversationAnalysisMounted = true;
  try {
    conversationAnalysisModule = conversationAnalysisModule || await import('./conversation-history.js');
    if (!scopedPage.isActive() || state.tab !== 'conversations') {
      conversationAnalysisMounted = false;
      return;
    }
    conversationAnalysisContext = createPageContext({ container });
    const initFn = conversationAnalysisModule.default || conversationAnalysisModule.init;
    await initFn?.(container, { ctx: conversationAnalysisContext, embedded: true });
    if (!scopedPage.isActive() || state.tab !== 'conversations') disposeConversationAnalysis();
  } catch (error) {
    if (error?.name === 'AbortError') return;
    conversationAnalysisMounted = false;
    if (!scopedPage.isActive()) return;
    container.innerHTML = `<div class="memory-empty">加载对话分析失败：${escapeHtml(error.message)}</div>`;
  }
}

function disposeConversationAnalysis() {
  conversationAnalysisModule?.dispose?.();
  conversationAnalysisContext?.cleanup?.();
  conversationAnalysisContext = null;
  conversationAnalysisMounted = false;
}

function getActiveItems() {
  if (state.tab === 'diary') return state.data.diary?.entries || [];
  if (state.tab === 'units') return state.data.units?.units || [];
  if (state.tab === 'glossary') return state.data.glossary?.terms || [];
  if (state.tab === 'users') return state.data.users?.users || [];
  return [];
}

function renderList() {
  const list = $('memoryList');
  const title = $('memoryListTitle');
  const subtitle = $('memoryListSubtitle');
  if (!list || !title || !subtitle) return;

  const tab = TABS.find((item) => item.id === state.tab);
  title.textContent = tab?.label || '记忆列表';

  if (state.loadingTabs.has(state.tab)) {
    subtitle.textContent = '正在加载当前模块';
    list.innerHTML = '<div class="memory-empty">加载中…</div>';
    return;
  }
  if (!state.loadedTabs.has(state.tab)) {
    subtitle.textContent = '切换到该模块后加载';
    list.innerHTML = '<div class="memory-empty">该记忆模块尚未加载</div>';
    return;
  }

  const filtered = getActiveItems().filter(matchesFilters);
  subtitle.textContent = `当前显示 ${filtered.length} 条`;
  if (!filtered.length) {
    list.innerHTML = '<div class="memory-empty">当前筛选下没有记忆数据</div>';
    return;
  }
  list.innerHTML = filtered.map((item, index) => renderItem(item, index)).join('');
  list.querySelectorAll('[data-edit]').forEach((btn) => {
    btn.addEventListener('click', () => openEditor(state.tab, filtered[Number(btn.dataset.edit)]));
  });
  list.querySelectorAll('[data-delete]').forEach((btn) => {
    btn.addEventListener('click', () => deleteItem(state.tab, filtered[Number(btn.dataset.delete)]));
  });
}

function matchesFilters(item) {
  const group = String(item.group_id || item.group || '');
  if (state.group && group !== state.group) return false;
  if (!state.search) return true;
  const haystack = JSON.stringify(item).toLowerCase();
  return haystack.includes(state.search);
}

function renderItem(item, index) {
  if (state.tab === 'diary') {
    const title = item.summary || item.content?.slice(0, 80) || '未命名日记';
    return renderItemShell({
      title,
      meta: [item.group_id, formatDate(item.created_at), ...(item.keywords || []).slice(0, 5)],
      content: item.content || '',
      index,
      editable: true,
    });
  }
  if (state.tab === 'units') {
    return renderItemShell({
      title: item.summary || item.unit_id || '未命名记忆单元',
      meta: [item.group_id, item.unit_type, item.lifespan, `显著度 ${formatPercent(item.salience)}`, ...(item.keywords || []).slice(0, 4)],
      content: [
        item.topics?.length ? `话题: ${item.topics.join(', ')}` : '',
        item.participants?.length ? `参与者: ${item.participants.join(', ')}` : '',
        `创建: ${formatDate(item.created_at)}`,
      ].filter(Boolean).join('\n'),
      index,
      editable: true,
    });
  }
  if (state.tab === 'glossary') {
    return renderItemShell({
      title: item.term || '未命名术语',
      meta: [item.domain, `置信度 ${formatPercent(item.confidence)}`, `使用 ${item.usage_count || 0} 次`],
      content: item.definition || '',
      index,
      editable: true,
    });
  }
  if (state.tab === 'users') {
    return renderItemShell({
      title: item.name || item.user_id || '未知用户',
      meta: [item.user_id, `互动 ${item.interaction_count || 0} 次`, `参与度 ${formatPercent(item.engagement_rate)}`],
      content: `首次互动: ${formatDate(item.first_interaction_at)}\n最近互动: ${formatDate(item.last_interaction_at)}`,
      index,
      editable: true,
    });
  }
  return renderItemShell({
    title: item.speaker_name || item.user_id || item.role || '消息',
    meta: [item.group_id, item.role, formatDate(item.timestamp)],
    content: item.content || '',
    index,
    editable: false,
    deletable: true,
  });
}

function renderItemShell({ title, meta, content, index, editable, deletable = editable }) {
  const actions = (editable || deletable)
    ? `<div class="memory-actions">
        ${editable ? `<button class="btn btn-sm" data-edit="${index}">编辑</button>` : ''}
        ${deletable ? `<button class="btn btn-sm btn-danger" data-delete="${index}">删除</button>` : ''}
      </div>`
    : '';
  return `
    <div class="memory-item">
      <div class="memory-item-head">
        <div>
          <div class="memory-item-title">${escapeHtml(title)}</div>
          <div class="memory-item-meta">${meta.filter(Boolean).map((m) => `<span class="memory-pill">${escapeHtml(m)}</span>`).join('')}</div>
        </div>
        ${actions}
      </div>
      <div class="memory-content">${escapeHtml(content || '暂无内容')}</div>
    </div>
  `;
}

function openEditor(tab, item) {
  const form = $('memoryForm');
  const title = $('memoryFormTitle');
  const subtitle = $('memoryFormSubtitle');
  if (!form || !title || !subtitle) return;
  const isNew = !item;
  title.textContent = isNew ? '新增记忆' : '编辑记忆';
  subtitle.textContent = TABS.find((t) => t.id === tab)?.label || '';
  if (tab === 'diary') {
    form.innerHTML = diaryForm(item);
  } else if (tab === 'units') {
    form.innerHTML = unitForm(item);
  } else if (tab === 'glossary') {
    form.innerHTML = glossaryForm(item);
  } else if (tab === 'users') {
    form.innerHTML = userForm(item);
  } else {
    form.innerHTML = '<div class="memory-empty">基础记忆来自运行窗口和归档，仅支持浏览检索。</div>';
    return;
  }
  $('memorySaveBtn')?.addEventListener('click', () => saveEditor(tab, item));
  $('memoryCancelBtn')?.addEventListener('click', closeEditor);
}

function closeEditor() {
  const form = $('memoryForm');
  const title = $('memoryFormTitle');
  const subtitle = $('memoryFormSubtitle');
  if (title) title.textContent = '选择一条记忆';
  if (subtitle) subtitle.textContent = '可编辑的记忆会在这里打开';
  if (form) form.innerHTML = '<div class="memory-empty">从左侧选择条目，或点击新增创建长期记忆。</div>';
}

function diaryForm(item = {}) {
  return `
    <div class="memory-form-grid">
      ${field('群组 ID', 'editGroupId', item.group_id || state.group || 'default')}
      ${field('摘要', 'editSummary', item.summary || '')}
      ${field('关键词', 'editKeywords', (item.keywords || []).join(', '), '逗号分隔')}
      ${textarea('内容', 'editContent', item.content || '', 8)}
      <div style="display:flex;gap:8px">
        <button class="btn btn-primary" id="memorySaveBtn">保存</button>
        <button class="btn" id="memoryCancelBtn">取消</button>
      </div>
    </div>
  `;
}

function unitForm(item = {}) {
  return `
    <div class="memory-form-grid">
      ${field('群组 ID', 'editGroupId', item.group_id || state.group || 'default')}
      ${field('类型', 'editUnitType', item.unit_type || 'event')}
      ${field('作用域', 'editScope', item.scope || 'group')}
      ${field('作用域 ID', 'editScopeId', item.scope_id || '')}
      ${textarea('摘要', 'editSummary', item.summary || '', 5)}
      ${field('参与者', 'editParticipants', (item.participants || []).join(', '), '逗号分隔')}
      ${field('话题', 'editTopics', (item.topics || []).join(', '), '逗号分隔')}
      ${field('关键词', 'editKeywords', (item.keywords || []).join(', '), '逗号分隔')}
      ${field('显著度', 'editSalience', item.salience ?? 0.5, '', 'number', '0', '1', '0.05')}
      ${field('置信度', 'editConfidence', item.confidence ?? 0.7, '', 'number', '0', '1', '0.05')}
      ${field('生命周期', 'editLifespan', item.lifespan || 'medium')}
      ${field('来源 ID', 'editSourceIds', (item.source_ids || []).join(', '), '逗号分隔')}
      <label style="display:flex;gap:8px;align-items:center">
        <input id="editShouldPrompt" type="checkbox" ${item.should_prompt ?? true ? 'checked' : ''} style="width:auto">
        回复时可提示
      </label>
      <div style="display:flex;gap:8px">
        <button class="btn btn-primary" id="memorySaveBtn">保存</button>
        <button class="btn" id="memoryCancelBtn">取消</button>
      </div>
    </div>
  `;
}

function glossaryForm(item = {}) {
  return `
    <div class="memory-form-grid">
      ${field('术语', 'editTerm', item.term || '')}
      ${textarea('定义', 'editDefinition', item.definition || '', 5)}
      ${field('领域', 'editDomain', item.domain || 'custom')}
      ${field('置信度', 'editConfidence', item.confidence ?? 0.8, '', 'number', '0', '1', '0.05')}
      ${field('使用次数', 'editUsageCount', item.usage_count || 1, '', 'number', '0', '', '1')}
      ${field('相关术语', 'editRelatedTerms', (item.related_terms || []).join(', '), '逗号分隔')}
      ${textarea('上下文例句', 'editExamples', (item.context_examples || []).join('\n'), 4)}
      <div style="display:flex;gap:8px">
        <button class="btn btn-primary" id="memorySaveBtn">保存</button>
        <button class="btn" id="memoryCancelBtn">取消</button>
      </div>
    </div>
  `;
}

function userForm(item = {}) {
  return `
    <div class="memory-form-grid">
      ${field('群组 ID', 'editGroupId', item.group_id || state.group || '', '必填')}
      ${field('用户 ID', 'editUserId', item.user_id || '', '', 'text', '', '', '', Boolean(item.user_id))}
      ${field('名称', 'editUserName', item.name || '')}
      ${field('参与度', 'editEngagement', item.engagement_rate ?? 0, '', 'number', '0', '1', '0.01')}
      ${field('互动次数', 'editInteractionCount', item.interaction_count || 0, '', 'number', '0', '', '1')}
      ${field('首次互动', 'editFirstAt', item.first_interaction_at || '')}
      ${field('最近互动', 'editLastAt', item.last_interaction_at || '')}
      <div style="display:flex;gap:8px">
        <button class="btn btn-primary" id="memorySaveBtn">保存</button>
        <button class="btn" id="memoryCancelBtn">取消</button>
      </div>
    </div>
  `;
}

function field(label, id, value, hint = '', type = 'text', min = '', max = '', step = '', disabled = false) {
  return `
    <div>
      <label for="${id}">${label}${hint ? ` <span style="color:var(--text-3)">(${hint})</span>` : ''}</label>
      <input id="${id}" type="${type}" value="${escapeAttr(value)}"${min !== '' ? ` min="${min}"` : ''}${max !== '' ? ` max="${max}"` : ''}${step !== '' ? ` step="${step}"` : ''}${disabled ? ' disabled' : ''}>
    </div>
  `;
}

function textarea(label, id, value, rows) {
  return `
    <div>
      <label for="${id}">${label}</label>
      <textarea id="${id}" rows="${rows}">${escapeHtml(value)}</textarea>
    </div>
  `;
}

async function saveEditor(tab, item) {
  try {
    if (tab === 'diary') {
      const payload = {
        group_id: $('editGroupId').value.trim() || 'default',
        summary: $('editSummary').value.trim(),
        keywords: splitList($('editKeywords').value),
        content: $('editContent').value.trim(),
      };
      if (item?.entry_id) await put(`/persona/diary/${encodeURIComponent(item.entry_id)}`, payload);
      else await post('/persona/diary', payload);
    } else if (tab === 'units') {
      const payload = {
        group_id: $('editGroupId').value.trim() || 'default',
        unit_type: $('editUnitType').value.trim() || 'event',
        scope: $('editScope').value.trim() || 'group',
        scope_id: $('editScopeId').value.trim(),
        summary: $('editSummary').value.trim(),
        participants: splitList($('editParticipants').value),
        topics: splitList($('editTopics').value),
        keywords: splitList($('editKeywords').value),
        salience: Number($('editSalience').value || 0),
        confidence: Number($('editConfidence').value || 0),
        lifespan: $('editLifespan').value.trim() || 'medium',
        should_prompt: $('editShouldPrompt').checked,
        source_ids: splitList($('editSourceIds').value),
      };
      if (!payload.summary) {
        toast('记忆单元摘要不能为空', 'error');
        return;
      }
      if (item?.unit_id) await put(`/persona/memory-units/${encodeURIComponent(item.unit_id)}`, payload);
      else await post('/persona/memory-units', payload);
    } else if (tab === 'glossary') {
      const payload = {
        term: $('editTerm').value.trim(),
        definition: $('editDefinition').value.trim(),
        domain: $('editDomain').value.trim() || 'custom',
        confidence: Number($('editConfidence').value || 0),
        usage_count: Number($('editUsageCount').value || 0),
        related_terms: splitList($('editRelatedTerms').value),
        context_examples: $('editExamples').value.split('\n').map((v) => v.trim()).filter(Boolean),
      };
      if (item?.term) await put(`/persona/glossary/${encodeURIComponent(item.term)}`, payload);
      else await post('/persona/glossary', payload);
    } else if (tab === 'users') {
      const userId = (item?.user_id || $('editUserId').value).trim();
      const payload = {
        group_id: $('editGroupId').value.trim(),
        name: $('editUserName').value.trim(),
        engagement_rate: Number($('editEngagement').value || 0),
        interaction_count: Number($('editInteractionCount').value || 0),
        first_interaction_at: $('editFirstAt').value.trim(),
        last_interaction_at: $('editLastAt').value.trim(),
      };
      if (!userId || !payload.group_id) {
        toast('用户 ID 和群组 ID 不能为空', 'error');
        return;
      }
      await put(`/persona/users/${encodeURIComponent(userId)}`, payload);
    }
    toast('记忆已保存');
    closeEditor();
    await loadActiveTab({ force: true });
  } catch (error) {
    if (error?.name === 'AbortError') return;
    toast('保存失败: ' + error.message, 'error');
  }
}


function buildConversationDeletePath(item) {
  const params = new URLSearchParams();
  const key = conversationEntryKey(item);
  if (key) params.set('key', key);
  if (item.group_id) params.set('group_id', item.group_id);
  return `/persona/conversations?${params.toString()}`;
}

function conversationEntryKey(item) {
  const entryId = String(item.entry_id || '').trim();
  if (entryId) return `id:${entryId}`;
  const timestamp = item.timestamp || '';
  const role = item.role || '';
  const userId = item.user_id || '';
  const content = String(item.content || '').slice(0, 120);
  return `fallback:${timestamp}:${role}:${userId}:${content}`;
}

async function deleteItem(tab, item) {
  const label = tab === 'diary'
    ? (item.summary || item.entry_id)
    : tab === 'units'
      ? (item.summary || item.unit_id)
      : tab === 'glossary'
      ? item.term
      : tab === 'conversations'
        ? (item.content || item.entry_id || item.timestamp || '\u8be5\u6d88\u606f').slice(0, 40)
        : (item.name || item.user_id);
  if (!confirmDanger(`确定删除「${label}」吗？此操作不可撤销。`)) return;
  try {
    if (tab === 'diary') {
      await del(`/persona/diary/${encodeURIComponent(item.entry_id)}`);
    } else if (tab === 'units') {
      await del(`/persona/memory-units/${encodeURIComponent(item.unit_id)}`);
    } else if (tab === 'glossary') {
      await del(`/persona/glossary/${encodeURIComponent(item.term)}`);
    } else if (tab === 'users') {
      const group = item.group_id || state.group || '';
      const suffix = group ? `?group_id=${encodeURIComponent(group)}` : '';
      await del(`/persona/users/${encodeURIComponent(item.user_id)}${suffix}`);
    } else if (tab === 'conversations') {
      await del(buildConversationDeletePath(item));
    }
    toast('记忆已删除');
    closeEditor();
    await loadActiveTab({ force: true });
  } catch (error) {
    if (error?.name === 'AbortError') return;
    toast('删除失败: ' + error.message, 'error');
  }
}

function renderTimeline(timeline) {
  const container = $('timelineChart');
  if (!container) return;
  const buckets = timeline.buckets || {};
  const allDays = timeline.days || [];
  const recent = timeline.recent || [];

  if (!allDays.length && !recent.length) {
    container.innerHTML = '<div class="memory-empty">暂无基础记忆时间线数据</div>';
    return;
  }

  const daysRange = Number($('timelineRange')?.value || '30');
  let filteredDays = allDays;
  if (daysRange > 0) {
    const cutoff = new Date();
    cutoff.setDate(cutoff.getDate() - daysRange);
    const cutoffStr = cutoff.toISOString().slice(0, 10);
    filteredDays = allDays.filter((day) => day >= cutoffStr);
  }

  const dailyData = {};
  for (const day of filteredDays) {
    const dayGroups = buckets[day] || {};
    const agg = { human: 0, assistant: 0, system: 0 };
    for (const [gid, counts] of Object.entries(dayGroups)) {
      if (state.group && gid !== state.group) continue;
      for (const role of ['human', 'assistant', 'system']) {
        agg[role] += counts[role] || 0;
      }
    }
    if (agg.human + agg.assistant + agg.system > 0) dailyData[day] = agg;
  }

  const days = Object.keys(dailyData).sort();
  if (!days.length) {
    container.innerHTML = '<div class="memory-empty">当前筛选下暂无时间线数据</div>';
    return;
  }

  const roles = [
    { key: 'human', name: '用户消息', color: ROLE_COLORS.human },
    { key: 'assistant', name: 'AI 回复', color: ROLE_COLORS.assistant },
    { key: 'system', name: '系统消息', color: ROLE_COLORS.system },
  ];

  getChart(container);
  setChartOption(container, {
    backgroundColor: 'transparent',
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      formatter: (params) => {
        const day = params[0]?.axisValue || '';
        const lines = params
          .filter((p) => p.value > 0)
          .map((p) => `${p.marker} ${p.seriesName}: <b>${p.value}</b>`)
          .join('<br/>');
        const preview = recent
          .filter((row) => row.timestamp?.startsWith(day) && (!state.group || row.group_id === state.group))
          .slice(0, 3)
          .map((row) => `${escapeHtml(row.speaker_name || row.role)}: ${escapeHtml((row.content || '').slice(0, 42))}`)
          .join('<br/>');
        return `<b>${day}</b><br/>${lines}${preview ? `<div style="margin-top:6px;padding-top:6px;border-top:1px solid #30363d;color:#8b949e">${preview}</div>` : ''}`;
      },
    },
    legend: { data: roles.map((role) => role.name), textStyle: { color: '#8b949e', fontSize: 11 }, top: 0 },
    grid: { left: 8, right: 8, bottom: 8, top: 36, containLabel: true },
    xAxis: {
      type: 'category',
      data: days,
      axisLabel: { fontSize: 10, color: '#8b949e', rotate: days.length > 20 ? 45 : 0, formatter: (value) => value.slice(5) },
      axisLine: { lineStyle: { color: '#30363d' } },
    },
    yAxis: {
      type: 'value',
      minInterval: 1,
      axisLabel: { fontSize: 10, color: '#8b949e' },
      splitLine: { lineStyle: { color: '#21262d' } },
    },
    series: roles.map((role) => ({
      name: role.name,
      type: 'bar',
      stack: 'total',
      barMaxWidth: 26,
      itemStyle: { color: role.color },
      emphasis: { focus: 'series' },
      data: days.map((day) => dailyData[day][role.key] || 0),
    })),
  });
}

function renderCluster(entries) {
  const container = $('clusterChart');
  if (!container) return;
  const withEmbedding = entries.filter((entry) => entry.embedding && entry.embedding.length >= 2);
  if (!withEmbedding.length) {
    container.innerHTML = '<div class="memory-empty">暂无日记 embedding 数据</div>';
    return;
  }

  const keywords = [...new Set(withEmbedding.flatMap((entry) => entry.keywords || []))].slice(0, 10);
  const palette = ['#58a6ff', '#3fb950', '#a371f7', '#e3b341', '#f78166', '#d2a8ff', '#79c0ff', '#ffa657', '#ff7b72', '#56d4dd'];
  const colorMap = Object.fromEntries(keywords.map((kw, idx) => [kw, palette[idx % palette.length]]));

  getChart(container);
  setChartOption(container, {
    backgroundColor: 'transparent',
    tooltip: {
      formatter: (params) => {
        const raw = params.data.raw;
        return `<b>${escapeHtml(raw.summary || '')}</b><br/>${escapeHtml((raw.content || '').slice(0, 120))}`;
      },
    },
    legend: { data: keywords, textStyle: { color: '#8b949e', fontSize: 11 }, top: 0, type: 'scroll' },
    grid: { left: 8, right: 8, bottom: 8, top: 36, containLabel: true },
    xAxis: { type: 'value', axisLabel: { fontSize: 10, color: '#8b949e' }, splitLine: { lineStyle: { color: '#21262d' } } },
    yAxis: { type: 'value', axisLabel: { fontSize: 10, color: '#8b949e' }, splitLine: { lineStyle: { color: '#21262d' } } },
    series: keywords.map((kw) => ({
      name: kw,
      type: 'scatter',
      symbolSize: 10,
      itemStyle: { color: colorMap[kw] },
      data: withEmbedding
        .filter((entry) => (entry.keywords || []).includes(kw))
        .map((entry) => ({ value: [entry.embedding[0], entry.embedding[1]], raw: entry })),
    })),
  });
}

function splitList(value) {
  return String(value || '')
    .replace(/，/g, ',')
    .split(',')
    .map((item) => item.trim())
    .filter(Boolean);
}

function formatDate(value) {
  if (!value) return '';
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString('zh-CN', { hour12: false });
}

function formatPercent(value) {
  const num = Number(value || 0);
  return `${Math.round(num * 100)}%`;
}

function escapeHtml(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function escapeAttr(value) {
  return escapeHtml(value).replace(/\n/g, '&#10;');
}
