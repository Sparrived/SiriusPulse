import { store } from '../store.js';
import { get } from '../app.js';
import { toast, $ } from '../components.js';

let allTerms = [];
let activeSearch = '';
let activeGroup = '';

const PAGE_SIZE = 50;
let currentPage = 0;
let totalRecords = 0;

const DOMAIN_COLORS = {
  tech: '#58a6ff',
  daily: '#a371f7',
  culture: '#e3b341',
  game: '#3fb950',
  custom: '#8b949e',
};

export async function init(container) {
  const name = store.currentPersona;
  if (!name) {
    container.innerHTML = `
      <div class="card">
        <div style="padding:60px;text-align:center;color:var(--text-3)">请先选择人格</div>
      </div>
    `;
    return;
  }

  container.innerHTML = `
    <div class="card" style="margin-bottom:20px">
      <div class="card-header">
        <div>
          <div class="card-title">术语表</div>
          <div class="card-subtitle">管理人格积累的专业术语和黑话</div>
        </div>
        <div style="display:flex;gap:12px;align-items:center">
          <select id="glossaryGroupFilter" class="btn btn-sm">
            <option value="">全部分组</option>
          </select>
          <input type="text" id="glossarySearch" placeholder="搜索术语..." style="width:200px;background:var(--surface-2);border:1px solid var(--border);border-radius:6px;padding:6px 10px;font-size:13px;color:var(--text-1)">
        </div>
      </div>
      <div class="stat-grid" id="glossaryStats" style="padding:16px"></div>
    </div>
    <div id="termList">
      <div style="color:var(--text-3);text-align:center;padding:40px">加载中...</div>
    </div>
  `;

  $('glossarySearch').addEventListener('input', (e) => {
    activeSearch = e.target.value.trim().toLowerCase();
    currentPage = 0;
    loadGlossary();
  });
  $('glossaryGroupFilter').addEventListener('change', (e) => {
    activeGroup = e.target.value;
    currentPage = 0;
    loadGlossary();
  });

  await loadGlossary();
}

async function loadGlossary() {
  const name = store.currentPersona;
  try {
    const offset = currentPage * PAGE_SIZE;
    let url = `/persona/glossary?limit=${PAGE_SIZE}&offset=${offset}`;
    if (activeSearch) {
      url += `&search=${encodeURIComponent(activeSearch)}`;
    }
    if (activeGroup) {
      url += `&group=${encodeURIComponent(activeGroup)}`;
    }
    const data = await get(url);
    allTerms = data.terms || [];
    totalRecords = data.total || 0;
    renderStats(data);
    renderGroupFilter(data);
    filterAndRender();
  } catch (e) {
    toast('加载术语表失败: ' + e.message, 'error');
  }
}

function renderStats(data) {
  const stats = data.stats || {};
  const totalCount = stats.total || totalRecords || allTerms.length;
  const el = $('glossaryStats');
  if (!el) return;
  el.innerHTML = `
    <div class="stat-card">
      <div class="stat-label">术语总数</div>
      <div class="stat-value">${totalCount}</div>
    </div>
  `;
}

function renderGroupFilter(data) {
  const groups = data.groups || [...new Set(allTerms.map(t => t.group).filter(Boolean))];
  const sel = $('glossaryGroupFilter');
  if (!sel) return;
  sel.innerHTML = '<option value="">全部分组</option>' +
    groups.map(g => `<option value="${g}">${g}</option>`).join('');
  if (activeGroup) sel.value = activeGroup;
}

function filterAndRender() {
  renderTerms(allTerms);
}

function confidenceBadge(conf) {
  if (conf >= 0.8) return '<span class="tag" style="font-size:11px;color:var(--success)">~ 高</span>';
  if (conf >= 0.6) return '<span class="tag tag-accent" style="font-size:11px">? 中</span>';
  return '<span class="tag" style="font-size:11px;color:var(--danger)">? 低</span>';
}

function domainBadge(domain) {
  const color = DOMAIN_COLORS[domain] || DOMAIN_COLORS.custom;
  const label = domain || 'custom';
  return `<span class="tag" style="font-size:11px;border-color:${color};color:${color}">${label}</span>`;
}

function renderTerms(terms) {
  const el = $('termList');
  if (!terms.length) {
    el.innerHTML = `
      <div class="card">
        <div style="padding:40px;text-align:center;color:var(--text-3)">
          ${activeSearch ? `没有匹配「${activeSearch}」的术语` : '暂无术语'}
        </div>
      </div>
    `;
    return;
  }

  el.innerHTML = `<div style="display:grid;gap:12px">${terms.map(t => {
    const contexts = (t.context_examples || []).slice(0, 3);
    const related = (t.related_terms || t.related || []).slice(0, 5);
    return `
      <div class="card" style="margin:0">
        <div class="card-header">
          <div style="display:flex;align-items:center;gap:10px;flex-wrap:wrap">
            <span style="font-size:15px;font-weight:700">${t.term || ''}</span>
            ${t.confidence != null ? confidenceBadge(t.confidence) : ''}
            ${domainBadge(t.domain)}
          </div>
          <div style="display:flex;gap:12px;font-size:12px;color:var(--text-3)">
            ${t.usage_count != null ? `<span>使用 ${t.usage_count} 次</span>` : ''}
            ${t.source ? `<span>来源: ${t.source}</span>` : ''}
          </div>
        </div>
        ${t.definition ? `<div style="padding:0 16px 12px;font-size:13px;color:var(--text-2);line-height:1.6">${t.definition}</div>` : ''}
        ${contexts.length ? `
          <div style="padding:0 16px 12px;display:grid;gap:6px">
            ${contexts.map(ctx => `
              <div style="padding:8px 12px;border-left:3px solid var(--accent);background:var(--surface-2,rgba(255,255,255,0.03));border-radius:0 6px 6px 0;font-size:12px;color:var(--text-2);line-height:1.5">${ctx}</div>
            `).join('')}
          </div>
        ` : ''}
        ${related.length ? `
          <div style="padding:0 16px 16px;display:flex;gap:6px;flex-wrap:wrap">
            <span style="font-size:12px;color:var(--text-3);line-height:24px">相关:</span>
            ${related.map(r => `<span class="tag" style="font-size:11px">${typeof r === 'string' ? r : r.term || ''}</span>`).join('')}
          </div>
        ` : ''}
      </div>
    `;
  }).join('')}</div>`;
  renderPagination(terms.length);
}

function renderPagination(displayedCount) {
  const totalPages = Math.ceil(totalRecords / PAGE_SIZE);
  if (totalPages <= 1 && !activeGroup) return;

  const paginationEl = document.createElement('div');
  paginationEl.style.cssText = 'display:flex;justify-content:center;align-items:center;gap:12px;padding:16px;margin-top:12px';

  const prevDisabled = currentPage === 0;
  const nextDisabled = currentPage >= totalPages - 1;

  const start = currentPage * PAGE_SIZE + 1;
  const end = Math.min((currentPage + 1) * PAGE_SIZE, totalRecords);

  let infoText = `显示 ${start}-${end} / 共 ${totalRecords} 条`;
  if (activeGroup) {
    infoText += ` (当前分组: ${displayedCount} 条)`;
  }

  paginationEl.innerHTML = `
    <button id="prevPage" class="btn btn-sm" ${prevDisabled ? 'disabled' : ''}>上一页</button>
    <span style="font-size:13px;color:var(--text-2)">${infoText}</span>
    <button id="nextPage" class="btn btn-sm" ${nextDisabled ? 'disabled' : ''}>下一页</button>
  `;

  const el = $('termList');
  el.appendChild(paginationEl);

  $('prevPage').addEventListener('click', () => {
    if (currentPage > 0) {
      currentPage--;
      loadGlossary();
    }
  });

  $('nextPage').addEventListener('click', () => {
    if (currentPage < totalPages - 1) {
      currentPage++;
      loadGlossary();
    }
  });
}
