import { store } from '../store.js';
import { get } from '../app.js';
import { toast, $ } from '../components.js';

let currentEntries = [];
let diaryData = null;
let activeKeyword = '';
let activeGroup = '';
let activeSearch = '';
let currentPage = 0;
let totalRecords = 0;
const PAGE_SIZE = 50;

export async function init(container) {
  const name = store.currentPersona;
  if (!name) {
    container.innerHTML = `
      <div class="card">
        <div class="card-header">
          <div class="card-title">日记记忆</div>
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

  currentPage = 0;

  container.innerHTML = `
    <div class="card">
      <div class="card-header">
        <div class="card-title">日记记忆</div>
        <div style="display:flex;gap:12px;align-items:center">
          <input type="text" id="diarySearch" placeholder="搜索日记内容..." style="width:180px;background:var(--surface-2);border:1px solid var(--border);border-radius:6px;padding:6px 10px;font-size:13px;color:var(--text-1)">
          <select id="diaryGroupFilter" class="btn btn-sm">
            <option value="">全部群组</option>
          </select>
        </div>
      </div>
      <div class="stat-grid" id="diaryStats"></div>
      <div id="diaryKeywords" style="margin-top:16px"></div>
    </div>
    <div style="margin-top:20px" id="diaryEntries"></div>
    <div id="diaryPagination" style="display:flex;justify-content:space-between;align-items:center;padding:12px 0;margin-top:12px"></div>
  `;

  $('diarySearch').addEventListener('input', (e) => {
    activeSearch = e.target.value.trim();
    currentPage = 0;
    loadData();
  });

  $('diaryGroupFilter').addEventListener('change', (e) => {
    activeGroup = e.target.value;
    currentPage = 0;
    activeKeyword = '';
    loadData();
  });

  await loadData();
}

async function loadData() {
  const name = store.currentPersona;
  if (!name) {
    toast('请先选择一个人格', 'error');
    return;
  }

  const params = new URLSearchParams({
    limit: String(PAGE_SIZE),
    offset: String(currentPage * PAGE_SIZE),
  });
  if (activeGroup) params.set('group_id', activeGroup);
  if (activeSearch) params.set('search', activeSearch);
  if (activeKeyword) params.set('keyword', activeKeyword);

  try {
    diaryData = await get(`/personas/${name}/diary?${params}`);
    currentEntries = diaryData.entries || [];
    totalRecords = diaryData.total || 0;
    renderStats();
    renderGroups();
    renderKeywords();
    filterAndRender();
    renderPagination();
  } catch (e) {
    toast('加载日记数据失败', 'error');
  }
}

function renderStats() {
  const stats = diaryData.stats || {};
  const el = $('diaryStats');
  if (!el) return;
  el.innerHTML = `
    <div class="stat-card">
      <div class="stat-label">日记总数</div>
      <div class="stat-value">${stats.total || 0}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">群组数量</div>
      <div class="stat-value">${stats.groups || 0}</div>
    </div>
  `;
}

function renderGroups() {
  const groups = diaryData.groups || [];
  const sel = $('diaryGroupFilter');
  sel.innerHTML = `<option value="">全部群组</option>` +
    groups.map(g => `<option value="${g}">${g}</option>`).join('');
  if (activeGroup) sel.value = activeGroup;
}

function renderKeywords() {
  const stats = diaryData.stats || {};
  const topKeywords = stats.top_keywords || [];
  const el = $('diaryKeywords');
  if (!topKeywords.length) {
    el.innerHTML = '';
    return;
  }
  el.innerHTML = topKeywords.map(([kw, count]) => {
    const isActive = kw === activeKeyword;
    return `<span class="tag${isActive ? ' tag-accent' : ''}" data-keyword="${kw}" style="cursor:pointer;margin:2px">${kw} (${count})</span>`;
  }).join('');

  el.querySelectorAll('[data-keyword]').forEach(tag => {
    tag.addEventListener('click', () => {
      const kw = tag.dataset.keyword;
      activeKeyword = activeKeyword === kw ? '' : kw;
      currentPage = 0;
      loadData();
    });
  });
}

function filterAndRender() {
  renderEntries(currentEntries);
}

function renderPagination() {
  const el = $('diaryPagination');
  if (!el) return;

  const totalPages = Math.max(1, Math.ceil(totalRecords / PAGE_SIZE));
  const isFirst = currentPage === 0;
  const isLast = currentPage >= totalPages - 1;

  el.innerHTML = `
    <span style="font-size:12px;color:var(--text-3)">
      共 ${totalRecords} 条，第 ${currentPage + 1}/${totalPages} 页
    </span>
    <div style="display:flex;gap:8px">
      <button class="btn btn-sm" id="diaryPrev" ${isFirst ? 'disabled' : ''}>上一页</button>
      <button class="btn btn-sm" id="diaryNext" ${isLast ? 'disabled' : ''}>下一页</button>
    </div>
  `;

  const prevBtn = $('diaryPrev');
  const nextBtn = $('diaryNext');
  if (prevBtn) prevBtn.addEventListener('click', () => { currentPage--; loadData(); });
  if (nextBtn) nextBtn.addEventListener('click', () => { currentPage++; loadData(); });
}

function formatTimestamp(ts) {
  if (!ts) return '—';
  try {
    return new Date(ts).toLocaleString('zh-CN');
  } catch {
    return ts;
  }
}

function renderEntries(entries) {
  const el = $('diaryEntries');
  if (!entries.length) {
    el.innerHTML = `
      <div class="card">
        <div style="color:var(--text-3);padding:40px;text-align:center">
          ${activeKeyword ? `没有包含关键词「${activeKeyword}」的日记` : '暂无日记'}
        </div>
      </div>
    `;
    return;
  }

  el.innerHTML = entries.map(entry => `
    <div class="card" style="margin-bottom:16px">
      <div class="card-header">
        <div style="display:flex;align-items:center;gap:12px">
          <span class="tag">${entry.group_id || '未知群组'}</span>
          <span style="font-size:12px;color:var(--text-3)">${formatTimestamp(entry.created_at)}</span>
        </div>
      </div>
      ${entry.summary ? `<div style="font-weight:600;font-size:15px;margin-bottom:8px">${entry.summary}</div>` : ''}
      <div style="white-space:pre-wrap;color:var(--text-2);font-size:14px;line-height:1.6">${entry.content || ''}</div>
      ${entry.keywords && entry.keywords.length ? `
        <div style="margin-top:12px;display:flex;flex-wrap:wrap;gap:4px">
          ${entry.keywords.map(kw => {
            const isActive = kw === activeKeyword;
            return `<span class="tag${isActive ? ' tag-accent' : ''}" data-keyword="${kw}" style="cursor:pointer">${kw}</span>`;
          }).join('')}
        </div>
      ` : ''}
    </div>
  `).join('');

  el.querySelectorAll('[data-keyword]').forEach(tag => {
    tag.addEventListener('click', () => {
      const kw = tag.dataset.keyword;
      activeKeyword = activeKeyword === kw ? '' : kw;
      currentPage = 0;
      loadData();
    });
  });
}
