import { store } from '../store.js';
import { get } from '../app.js';
import { toast, $ } from '../components.js';

let allEntries = [];
let diaryData = null;
let activeKeyword = '';
let activeGroup = '';

export async function init(container) {
  container.innerHTML = `
    <div class="card">
      <div class="card-header">
        <div class="card-title">日记记忆</div>
        <div style="display:flex;gap:12px;align-items:center">
          <select id="diaryGroupFilter" class="btn btn-sm">
            <option value="">全部群组</option>
          </select>
        </div>
      </div>
      <div class="stat-grid" id="diaryStats"></div>
      <div id="diaryKeywords" style="margin-top:16px"></div>
    </div>
    <div style="margin-top:20px" id="diaryEntries"></div>
  `;

  $('diaryGroupFilter').addEventListener('change', (e) => {
    activeGroup = e.target.value;
    filterAndRender();
  });

  await loadData();
}

async function loadData() {
  const name = store.currentPersona;
  if (!name) {
    toast('请先选择一个人格', 'error');
    return;
  }
  try {
    diaryData = await get(`/personas/${name}/diary`);
    allEntries = diaryData.entries || [];
    renderStats();
    renderGroups();
    renderKeywords();
    filterAndRender();
  } catch (e) {
    toast('加载日记数据失败', 'error');
  }
}

function renderStats() {
  const stats = diaryData.stats || {};
  $('diaryStats').innerHTML = `
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
      renderKeywords();
      filterAndRender();
    });
  });
}

function filterAndRender() {
  let filtered = allEntries;
  if (activeGroup) {
    filtered = filtered.filter(e => e.group_id === activeGroup);
  }
  if (activeKeyword) {
    filtered = filtered.filter(e => (e.keywords || []).includes(activeKeyword));
  }
  renderEntries(filtered);
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
      renderKeywords();
      filterAndRender();
    });
  });
}
