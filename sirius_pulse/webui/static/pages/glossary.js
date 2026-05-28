import { store } from '../store.js';
import { get } from '../app.js';
import { toast, $ } from '../components.js';

let allTerms = [];
let activeSearch = '';
let activeGroup = '';

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
    filterAndRender();
  });
  $('glossaryGroupFilter').addEventListener('change', (e) => {
    activeGroup = e.target.value;
    filterAndRender();
  });

  await loadGlossary();
}

async function loadGlossary() {
  const name = store.currentPersona;
  try {
    const data = await get(`/personas/${name}/glossary`);
    allTerms = data.terms || [];
    renderStats(data);
    renderGroupFilter(data);
    filterAndRender();
  } catch (e) {
    toast('加载术语表失败: ' + e.message, 'error');
  }
}

function renderStats(data) {
  const groups = new Set(allTerms.map(t => t.group).filter(Boolean));
  $('glossaryStats').innerHTML = `
    <div class="stat-card">
      <div class="stat-label">术语总数</div>
      <div class="stat-value">${allTerms.length}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">分组数量</div>
      <div class="stat-value">${data.groups ? data.groups.length : groups.size}</div>
    </div>
  `;
}

function renderGroupFilter(data) {
  const groups = data.groups || [...new Set(allTerms.map(t => t.group).filter(Boolean))];
  const sel = $('glossaryGroupFilter');
  sel.innerHTML = '<option value="">全部分组</option>' +
    groups.map(g => `<option value="${g}">${g}</option>`).join('');
  if (activeGroup) sel.value = activeGroup;
}

function filterAndRender() {
  let filtered = allTerms;
  if (activeGroup) {
    filtered = filtered.filter(t => t.group === activeGroup);
  }
  if (activeSearch) {
    filtered = filtered.filter(t =>
      (t.term || '').toLowerCase().includes(activeSearch) ||
      (t.definition || '').toLowerCase().includes(activeSearch)
    );
  }
  renderTerms(filtered);
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
}
