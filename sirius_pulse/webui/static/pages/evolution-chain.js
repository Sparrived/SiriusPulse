import { store } from '../store.js';
import { get } from '../app.js';
import { toast, $ } from '../components.js';
import { renderNeuralNav, consumeNavParams, showParamHint, makeClickableUser, navigateWithParams } from './memory-nav.js';

let allRecords = [];

export async function init(container) {
  const name = store.currentPersona;
  if (!name) {
    container.innerHTML = '<div class="evo-empty">请先选择人格</div>';
    return;
  }

  renderNeuralNav('evolution-chain');
  $('evoRefreshBtn')?.addEventListener('click', () => loadRecords());
  $('evoStatusFilter')?.addEventListener('change', () => filterAndRender());
  $('evoSearch')?.addEventListener('input', debounce(() => filterAndRender(), 250));

  const params = consumeNavParams();
  if (params?.subject) {
    $('evoSearch').value = params.subject;
    showParamHint(`主语: ${params.subject}`, () => { $('evoSearch').value = ''; filterAndRender(); });
  }
  if (params?.status) {
    $('evoStatusFilter').value = params.status;
  }

  await loadRecords();
}

async function loadRecords() {
  const name = store.currentPersona;
  if (!name) return;
  try {
    const status = $('evoStatusFilter')?.value || '';
    const params = new URLSearchParams({ limit: '300' });
    if (status) params.set('status', status);
    const data = await get(`/personas/${name}/memory/evolution?${params}`);
    allRecords = data.records || [];
    filterAndRender();
  } catch (e) {
    toast('加载演化链失败: ' + e.message, 'error');
  }
}

function filterAndRender() {
  const query = ($('evoSearch')?.value || '').trim().toLowerCase();
  let filtered = allRecords;
  if (query) {
    filtered = filtered.filter(r =>
      (r.subject || '').toLowerCase().includes(query) ||
      (r.predicate || '').toLowerCase().includes(query) ||
      (r.obj || '').toLowerCase().includes(query)
    );
  }
  renderRecords(filtered);
}

function renderRecords(records) {
  const list = $('evoList');
  if (!list) return;

  if (!records.length) {
    list.innerHTML = '<div class="evo-empty">暂无演化链记录</div>';
    return;
  }

  list.innerHTML = records.map(r => {
    const conf = Math.round((r.confidence || 0) * 100);
    const time = r.extracted_at ? formatTime(r.extracted_at) : '';
    return `
    <div class="evo-record" data-status="${r.status}" data-id="${r.record_id}" data-subject="${esc(r.subject)}" data-uid="${esc(r.subject_user_id || '')}">
      <div class="evo-triple">
        <span class="evo-triple-node mem-clickable-subject">${esc(r.subject)}</span>
        <span class="evo-triple-arrow">→</span>
        <span class="evo-triple-predicate">${esc(r.predicate)}</span>
        <span class="evo-triple-arrow">→</span>
        <span class="evo-triple-obj">${esc(r.obj)}</span>
      </div>
      <div class="evo-meta">
        <span class="evo-badge ${r.status}">${statusLabel(r.status)}</span>
        <span>置信度</span>
        <span class="evo-confidence-bar"><span class="evo-confidence-fill" style="width:${conf}%"></span></span>
        <span style="font-family:var(--font-mono)">${conf}%</span>
        <span>${r.source_type || ''}</span>
        ${time ? `<span>${time}</span>` : ''}
        ${r.supersedes?.length ? `<span>取代 ${r.supersedes.length} 条</span>` : ''}
      </div>
      <div class="evo-history" id="hist-${r.record_id}"></div>
    </div>`;
  }).join('');

  list.querySelectorAll('.evo-record').forEach(el => {
    el.addEventListener('click', () => toggleHistory(el));
  });

  list.querySelectorAll('.mem-clickable-subject').forEach(el => {
    el.addEventListener('click', (e) => {
      e.stopPropagation();
      const record = el.closest('.evo-record');
      const uid = record?.dataset.uid;
      if (uid) {
        navigateWithParams('biography-view', { userId: uid, userName: record.dataset.subject });
      } else {
        const subject = record?.dataset.subject;
        if (subject) $('evoSearch').value = subject;
        filterAndRender();
      }
    });
  });
}

async function toggleHistory(el) {
  const id = el.dataset.id;
  const histEl = $('hist-' + id);
  if (!histEl) return;

  if (histEl.classList.contains('open')) {
    histEl.classList.remove('open');
    return;
  }

  const name = store.currentPersona;
  try {
    const data = await get(`/personas/${name}/memory/evolution/${id}/history`);
    const history = data.history || [];
    if (!history.length) {
      histEl.innerHTML = '<div style="color:var(--text-3);font-size:12px">无演化历史</div>';
    } else {
      histEl.innerHTML = history.map(h =>
        `<div class="evo-history-item">
          <span class="evo-badge ${h.status}" style="margin-right:6px">${statusLabel(h.status)}</span>
          ${esc(h.subject)} → ${esc(h.predicate)} → ${esc(h.obj)}
          <span style="margin-left:8px;opacity:0.6">${Math.round((h.confidence || 0) * 100)}%</span>
          ${h.extracted_at ? `<span style="margin-left:8px;opacity:0.5">${formatTime(h.extracted_at)}</span>` : ''}
        </div>`
      ).join('');
    }
    histEl.classList.add('open');
  } catch (e) {
    histEl.innerHTML = '<div style="color:var(--danger);font-size:12px">加载历史失败</div>';
    histEl.classList.add('open');
  }
}

function statusLabel(s) {
  const map = { active: '活跃', superseded: '已取代', uncertain: '待确认', rejected: '已拒绝', shadow: '影子' };
  return map[s] || s;
}

function formatTime(ts) {
  try {
    const d = new Date(ts);
    const now = Date.now();
    const diff = (now - d.getTime()) / 1000;
    if (diff < 60) return '刚刚';
    if (diff < 3600) return Math.floor(diff / 60) + '分钟前';
    if (diff < 86400) return Math.floor(diff / 3600) + '小时前';
    return d.toLocaleDateString('zh-CN');
  } catch { return ts; }
}

function esc(s) {
  const d = document.createElement('div');
  d.textContent = s || '';
  return d.innerHTML;
}

function debounce(fn, ms) {
  let t;
  return (...args) => { clearTimeout(t); t = setTimeout(() => fn(...args), ms); };
}
