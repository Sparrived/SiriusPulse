import { store } from '../store.js';
import { get } from '../app.js';
import { toast } from '../components.js';
import {
  renderLineChart,
  renderBarChart,
  renderPieChart,
  renderSankeyChart,
  disposeChart,
} from '../charts.js';
import { createRealtimeRefresh } from './realtime.js';
import { createScopedPage } from '../page-context.js';

const scopedPage = createScopedPage();
const $ = scopedPage.$;

const TASK_LABELS = {
  response_generate: '主模型调用',
  cognition_analyze: '认知分析',
  diary_generate: '日记生成',
  memory_unit_extract: '记忆提取',
  memory_unit_deduplicate: '记忆去重',
};

let data = null;
let activeRange = 'all';
let activeTab = 'overview';
let currentPage = 1;
const PAGE_SIZE = 10;
const realtime = createRealtimeRefresh(() => loadData(true), { resources: ['tokens'], debounceMs: 500 });

export function dispose() {
  scopedPage.use(null, null);
  realtime.stop();
}

export async function init(container, params = {}) {
  scopedPage.use(params?.ctx, container);
  const name = store.currentPersona;
  if (!name) {
    container.innerHTML = `
      <div class="card">
        <div class="card-header">
          <div class="card-title">Token 使用分析</div>
        </div>
        <div style="padding:40px;text-align:center;color:var(--text-3)">
          <div style="font-size:48px;margin-bottom:16px">✦</div>
          <div style="font-size:16px;margin-bottom:8px">请先选择人格</div>
          <div style="font-size:13px">在顶部导航栏中选择要查看的人格</div>
        </div>
      </div>
    `;
    return;
  }

  container.innerHTML = `
    <div class="card">
      <div class="card-header">
        <div class="card-title">Token 使用分析</div>
        <div class="range-selector" id="tokenRange">
          <button class="btn btn-sm" data-range="all">全部</button>
          <button class="btn btn-sm" data-range="today">今天</button>
          <button class="btn btn-sm" data-range="7d">7天</button>
          <button class="btn btn-sm" data-range="30d">30天</button>
        </div>
      </div>
      <div class="stat-grid" id="tokenStats"></div>
    </div>
    <div class="card" style="margin-top:20px">
      <div class="tabs" id="tokenTabs">
        <button class="tab-btn active" data-tab="overview">概览</button>
        <button class="tab-btn" data-tab="module">模块</button>
        <button class="tab-btn" data-tab="dimension">维度</button>
        <button class="tab-btn" data-tab="detail">明细</button>
      </div>
      <div id="tokenTabPanels"></div>
    </div>
  `;

  bindRangeEvents();
  bindTabEvents();
  await loadData();
  realtime.start();
}

function bindRangeEvents() {
  const el = $('tokenRange');
  el.querySelectorAll('[data-range]').forEach(btn => {
    btn.addEventListener('click', () => {
      el.querySelectorAll('[data-range]').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      activeRange = btn.dataset.range;
      currentPage = 1;
      loadData(false);
    });
  });
  el.querySelector('[data-range="all"]').classList.add('active');
}

function bindTabEvents() {
  const tabs = $('tokenTabs');
  tabs.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      tabs.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      activeTab = btn.dataset.tab;
      currentPage = 1;
      renderTabContent();
    });
  });
}

function buildRangeParams() {
  const now = Date.now() / 1000;
  if (activeRange === 'today') {
    const start = new Date();
    start.setHours(0, 0, 0, 0);
    return `?start=${start.getTime() / 1000}&end=${now}`;
  }
  if (activeRange === '7d') return `?start=${now - 7 * 86400}&end=${now}`;
  if (activeRange === '30d') return `?start=${now - 30 * 86400}&end=${now}`;
  return '';
}

async function loadData(silent = false) {
  const name = store.currentPersona;
  if (!name) {
    toast('请先选择一个人格', 'error');
    return;
  }
  try {
    const params = buildRangeParams();
    data = await get(`/persona/tokens${params}`);
    renderStats();
    renderTabContent();
  } catch (e) {
    if (e?.name === 'AbortError') return;
    if (!silent) toast('加载 Token 数据失败', 'error');
    else console.warn('token realtime refresh failed:', e);
  }
}

function renderStats() {
  const s = data.summary || {};
  const cache = data.cache_stats || {};
  const cacheObserved = (cache.cache_info_calls || 0) > 0;
  const cacheRate = cacheObserved ? `${cache.cache_hit_rate_pct || 0}%` : '未记录';
  const el = $('tokenStats');
  if (!el) return;
  el.innerHTML = `
    <div class="stat-card">
      <div class="stat-label">总调用次数</div>
      <div class="stat-value">${(s.total_calls || 0).toLocaleString()}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Prompt Tokens</div>
      <div class="stat-value">${(s.total_prompt_tokens || 0).toLocaleString()}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">Completion Tokens</div>
      <div class="stat-value">${(s.total_completion_tokens || 0).toLocaleString()}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">总 Tokens</div>
      <div class="stat-value">${(s.total_tokens || 0).toLocaleString()}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">缓存命中 Prompt</div>
      <div class="stat-value">${(cache.cached_prompt_tokens || 0).toLocaleString()}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">未缓存 Prompt</div>
      <div class="stat-value">${(cache.uncached_prompt_tokens || 0).toLocaleString()}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">缓存命中率</div>
      <div class="stat-value">${cacheRate}</div>
      <div class="stat-label">数据覆盖 ${(cache.cache_info_coverage_pct || 0)}%</div>
    </div>
  `;
}

function renderTabContent() {
  const panels = $('tokenTabPanels');
  disposeAllCharts(panels);
  panels.innerHTML = '';

  if (activeTab === 'overview') renderOverviewTab(panels);
  else if (activeTab === 'module') renderModuleTab(panels);
  else if (activeTab === 'dimension') renderDimensionTab(panels);
  else if (activeTab === 'detail') renderDetailTab(panels);
}

function disposeAllCharts(container) {
  container.querySelectorAll('[data-chart]').forEach(el => disposeChart(el));
}

function formatHourLabel(ts) {
  const d = new Date(ts * 1000);
  const mm = String(d.getMonth() + 1).padStart(2, '0');
  const dd = String(d.getDate()).padStart(2, '0');
  const hh = String(d.getHours()).padStart(2, '0');
  return `${mm}-${dd} ${hh}`;
}

function renderOverviewTab(panels) {
  panels.innerHTML = `
    <div class="tab-panel active" style="display:grid;grid-template-columns:1fr 1fr;gap:20px;padding:20px 0">
      <div class="card">
        <div class="card-header"><div class="card-title">Token 使用趋势</div></div>
        <div data-chart="ts" style="min-height:300px"></div>
      </div>
      <div class="card">
        <div class="card-header"><div class="card-title">活跃时段分布</div></div>
        <div data-chart="dist" style="min-height:300px"></div>
      </div>
    </div>
  `;

  const hourly = data.hourly || [];
  if (hourly.length) {
    const labels = hourly.map(h => formatHourLabel(h.hour_ts));
    const hasCache = hourly.some(h => (h.cached_prompt_tokens || 0) + (h.uncached_prompt_tokens || 0) > 0);
    const series = [
      { name: 'Prompt Tokens', data: hourly.map(h => h.prompt_tokens) },
      { name: 'Completion Tokens', data: hourly.map(h => h.completion_tokens) },
    ];
    if (hasCache) {
      series.push(
        { name: '缓存命中 Prompt', data: hourly.map(h => h.cached_prompt_tokens || 0) },
        { name: '未缓存 Prompt', data: hourly.map(h => h.uncached_prompt_tokens || 0) },
      );
    }
    renderLineChart(panels.querySelector('[data-chart="ts"]'), {
      labels,
      dualAxis: true,
      colors: hasCache ? ['#4c9aff', '#36d399', '#f59e0b', '#ef6c6c'] : ['#4c9aff', '#36d399'],
      series,
    });
  }

  const dist = data.hourly_distribution || [];
  if (dist.length) {
    const labels = dist.map(d => `${d.hour}:00`);
    renderBarChart(panels.querySelector('[data-chart="dist"]'), {
      labels,
      data: [{ name: '调用次数', values: dist.map(d => d.calls) }],
      colors: ['#4c9aff'],
    });
  }
}

function renderModuleTab(panels) {
  panels.innerHTML = `
    <div class="tab-panel active" style="padding:20px 0">
      <div class="card">
        <div class="card-header"><div class="card-title">模块 Token 分布（桑基图）</div></div>
        <div data-chart="sankey" style="min-height:400px"></div>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-top:20px">
        <div class="card">
          <div class="card-header"><div class="card-title">模块 Token 分布</div></div>
          <div data-chart="section" style="min-height:300px"></div>
        </div>
        <div class="card">
          <div class="card-header"><div class="card-title">任务调用占比</div></div>
          <div data-chart="task-pie" style="min-height:300px"></div>
        </div>
      </div>
    </div>
  `;

  const sectionBreakdown = data.section_breakdown || {};
  const sectionBreakdownByTask = data.section_breakdown_by_task || {};

  // 渲染桑基图
  renderSankeyChart(
    panels.querySelector('[data-chart="sankey"]'),
    sectionBreakdown,
    sectionBreakdownByTask
  );

  // 渲染柱状图
  const sectionKeys = Object.keys(sectionBreakdown);
  if (sectionKeys.length) {
    renderBarChart(panels.querySelector('[data-chart="section"]'), {
      labels: sectionKeys,
      data: [{ name: 'Tokens', values: sectionKeys.map(k => sectionBreakdown[k]) }],
      colors: ['#7c4dff'],
    });
  }

  // 渲染饼图
  const byTask = data.by_task || [];
  if (byTask.length) {
    renderPieChart(panels.querySelector('[data-chart="task-pie"]'), {
      data: byTask.map(t => ({
        name: TASK_LABELS[t.name] || t.name,
        value: t.total_tokens,
      })),
    });
  }
}

function renderDimensionTab(panels) {
  panels.innerHTML = `
    <div class="tab-panel active" style="display:grid;grid-template-columns:1fr 1fr;gap:20px;padding:20px 0">
      <div class="card">
        <div class="card-header"><div class="card-title">按模型</div></div>
        <div data-chart="model" style="min-height:300px"></div>
      </div>
      <div class="card">
        <div class="card-header"><div class="card-title">按群组</div></div>
        <div data-chart="group" style="min-height:300px"></div>
      </div>
      <div class="card">
        <div class="card-header"><div class="card-title">按 Provider</div></div>
        <div data-chart="provider" style="min-height:300px"></div>
      </div>
      <div class="card">
        <div class="card-header"><div class="card-title">按任务</div></div>
        <div data-chart="task" style="min-height:300px"></div>
      </div>
    </div>
  `;

  renderDimensionChart(panels, 'model', data.by_model || []);
  renderDimensionChart(panels, 'group', data.by_group || []);
  renderDimensionChart(panels, 'provider', data.by_provider || []);
  renderDimensionChart(panels, 'task', data.by_task || [], true);
}

function renderDimensionChart(panels, key, items, translateTask = false) {
  const el = panels.querySelector(`[data-chart="${key}"]`);
  if (!items.length) return;
  const labels = items.map(i => {
    if (translateTask) return TASK_LABELS[i.name] || i.name;
    return i.name || '未分类';
  });
  const series = [
    { name: 'Prompt', values: items.map(i => i.prompt_tokens) },
    { name: 'Completion', values: items.map(i => i.completion_tokens) },
  ];
  renderBarChart(el, {
    labels,
    data: series,
    stacked: true,
    colors: ['#4c9aff', '#36d399'],
    horizontal: true,
  });
}

function renderDetailTab(panels) {
  panels.innerHTML = `
    <div class="tab-panel active" style="padding:20px 0">
      <div id="detailTable"></div>
      <div id="detailPager" style="display:flex;justify-content:center;gap:8px;margin-top:16px"></div>
    </div>
  `;
  renderDetailTable();
}

function renderDetailTable() {
  const records = data.recent_with_breakdown || [];
  const totalPages = Math.max(1, Math.ceil(records.length / PAGE_SIZE));
  if (currentPage > totalPages) currentPage = totalPages;
  const start = (currentPage - 1) * PAGE_SIZE;
  const page = records.slice(start, start + PAGE_SIZE);

  const tableEl = $('detailTable');
  if (!tableEl) return;
  if (!records.length) {
    tableEl.innerHTML = '<div style="color:var(--text-3);padding:24px;text-align:center">暂无记录</div>';
    const pager = $('detailPager');
    if (pager) pager.innerHTML = '';
    return;
  }

  tableEl.innerHTML = `
    <table class="table">
      <thead>
        <tr>
          <th>时间</th>
          <th>任务</th>
          <th>模型</th>
          <th>Prompt</th>
          <th>Completion</th>
          <th>缓存命中</th>
          <th>未缓存</th>
          <th>Top-3 模块</th>
        </tr>
      </thead>
      <tbody>
        ${page.map(r => {
          const ts = r.timestamp ? new Date(r.timestamp * 1000).toLocaleString('zh-CN') : '—';
          const task = TASK_LABELS[r.task_name] || r.task_name || '—';
          const cacheAvailable = Boolean(r.cache_info_available);
          const bd = r.breakdown || {};
          const top3 = Object.entries(bd)
            .sort((a, b) => b[1] - a[1])
            .slice(0, 3)
            .map(([k, v]) => `${k}: ${v}`)
            .join(', ');
          return `
            <tr>
              <td>${ts}</td>
              <td>${task}</td>
              <td>${r.model || '—'}</td>
              <td>${(r.prompt_tokens || 0).toLocaleString()}</td>
              <td>${(r.completion_tokens || 0).toLocaleString()}</td>
              <td>${cacheAvailable ? (r.cached_prompt_tokens || 0).toLocaleString() : '—'}</td>
              <td>${cacheAvailable ? (r.uncached_prompt_tokens || 0).toLocaleString() : '—'}</td>
              <td style="font-size:12px;color:var(--text-2)">${top3 || '—'}</td>
            </tr>
          `;
        }).join('')}
      </tbody>
    </table>
  `;

  const pager = $('detailPager');
  if (!pager) return;
  pager.innerHTML = Array.from({ length: totalPages }, (_, i) => {
    const p = i + 1;
    return `<button class="btn btn-sm${p === currentPage ? ' btn-primary' : ''}" data-page="${p}">${p}</button>`;
  }).join('');
  pager.querySelectorAll('[data-page]').forEach(btn => {
    btn.addEventListener('click', () => {
      currentPage = parseInt(btn.dataset.page, 10);
      renderDetailTable();
    });
  });
}
