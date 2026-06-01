import { store } from '../store.js';
import { get, del } from '../app.js';
import { toast, $ } from '../components.js';
import { renderNeuralNav, navigateWithParams } from './memory-nav.js';

let debounceTimer = null;
let isMultiMode = false;

export async function init(container) {
  const name = store.currentPersona;
  if (!name) {
    container.innerHTML = '<div class="ds-empty">请先选择人格</div>';
    return;
  }

  renderNeuralNav('diary-slices');
  $('dsRefreshBtn')?.addEventListener('click', () => loadSlices());
  $('dsSelectAll')?.addEventListener('change', handleSelectAll);
  $('dsDeleteBtn')?.addEventListener('click', handleDeleteSelected);
  $('dsMultiBtn')?.addEventListener('click', toggleMultiMode);
  $('dsSearch')?.addEventListener('input', () => {
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(() => loadSlices(), 300);
  });

  await loadSlices();
}

function toggleMultiMode() {
  isMultiMode = !isMultiMode;
  const btn = $('dsMultiBtn');
  const selectAllWrap = $('dsSelectAllWrap');

  if (btn) btn.classList.toggle('active', isMultiMode);
  if (selectAllWrap) selectAllWrap.classList.toggle('visible', isMultiMode);

  document.querySelectorAll('.ds-slice-check').forEach(cb => {
    cb.classList.toggle('visible', isMultiMode);
  });
  document.querySelectorAll('.ds-slice').forEach(slice => {
    slice.classList.toggle('selecting', isMultiMode);
  });

  if (!isMultiMode) {
    document.querySelectorAll('.ds-slice-check').forEach(cb => { cb.checked = false; });
    updateDeleteButton();
  }
}

async function loadSlices() {
  const name = store.currentPersona;
  if (!name) return;
  try {
    const search = $('dsSearch')?.value?.trim() || '';
    const params = new URLSearchParams({ limit: '200' });
    if (search) params.set('search', search);
    const data = await get(`/personas/${name}/memory/diary-slices?${params}`);
    renderSlices(data.slices || []);
  } catch (e) {
    toast('加载记忆切片失败: ' + e.message, 'error');
  }
}

function renderSlices(slices) {
  const masonry = $('dsMasonry');
  if (!masonry) return;

  if (!slices.length) {
    masonry.innerHTML = '<div class="ds-empty">暂无记忆切片数据</div>';
    return;
  }

  masonry.innerHTML = slices.map(s => {
    const summary = s.summary || s.content?.substring(0, 80) || '无摘要';
    const content = s.content || '';
    const hasMore = content.length > 120;
    const topics = (s.topics || s.keywords || []).map(t => `<span class="ds-slice-topic">${esc(t)}</span>`).join('');
    const subjects = (s.triple_subjects || []).map(t => `<span class="ds-slice-subject">${esc(t)}</span>`).join('');
    const timeStart = s.time_range_start ? formatTime(s.time_range_start) : '';
    const timeEnd = s.time_range_end ? formatTime(s.time_range_end) : '';
    const timeRange = timeStart && timeEnd ? `${timeStart} — ${timeEnd}` : (timeStart || '');
    const group = s._group_id || '';
    const participants = s.participants || [];
    const linkedSituations = s._linked_situations || [];

    const linkedHtml = linkedSituations.length > 0 ? `
      <div class="ds-slice-linked">
        <div class="ds-linked-title">关联情景 (${linkedSituations.length})</div>
        <div class="ds-linked-list">
          ${linkedSituations.map(sit => `
            <div class="ds-linked-item" data-situation-id="${sit.situation_id}">
              <span class="ds-linked-time">${formatLinkedTime(sit.created_at)}</span>
              <span class="ds-linked-summary">${esc(sit.summary || '无摘要')}</span>
              <div class="ds-linked-tags">
                ${(sit.topics || []).slice(0, 2).map(t => `<span class="ds-linked-tag">${esc(t)}</span>`).join('')}
              </div>
            </div>
          `).join('')}
        </div>
      </div>
    ` : '';

    return `
    <div class="ds-slice">
      <input type="checkbox" class="ds-slice-check" data-id="${s.slice_id}">
      <div class="ds-slice-summary">${esc(summary)}</div>
      ${content && content !== summary ? `<div class="ds-slice-content" onclick="this.classList.toggle('expanded')">${esc(content)}</div>` : ''}
      ${topics ? `<div class="ds-slice-topics">${topics}</div>` : ''}
      ${subjects ? `<div class="ds-slice-subjects">${subjects}</div>` : ''}
      <div class="ds-slice-meta">
        ${timeRange ? `<span>${timeRange}</span>` : ''}
        ${group ? `<span>${esc(group)}</span>` : ''}
        ${participants.length ? `<span>${participants.length} 人参与</span>` : ''}
      </div>
      ${linkedHtml}
    </div>`;
  }).join('');

  masonry.querySelectorAll('.ds-slice-check').forEach(cb => {
    cb.addEventListener('change', updateDeleteButton);
  });

  masonry.querySelectorAll('.ds-linked-item').forEach(el => {
    el.addEventListener('click', () => {
      navigateWithParams('situation-timeline', { situation_id: el.dataset.situationId });
    });
  });

  updateDeleteButton();
}

function formatLinkedTime(ts) {
  try {
    const d = new Date(ts);
    return d.toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' });
  } catch { return ''; }
}

function handleSelectAll(e) {
  const checked = e.target.checked;
  document.querySelectorAll('.ds-slice-check').forEach(cb => {
    cb.checked = checked;
  });
  updateDeleteButton();
}

function updateDeleteButton() {
  const selected = document.querySelectorAll('.ds-slice-check:checked');
  const btn = $('dsDeleteBtn');
  if (btn) {
    btn.classList.toggle('visible', selected.length > 0);
    btn.textContent = selected.length > 0 ? `删除选中 (${selected.length})` : '删除选中';
  }
}

function getSelectedIds() {
  return Array.from(document.querySelectorAll('.ds-slice-check:checked')).map(cb => cb.dataset.id);
}

async function handleDeleteSelected() {
  const ids = getSelectedIds();
  if (!ids.length) return;

  const name = store.currentPersona;
  if (!name) return;

  if (!confirm(`确定要删除 ${ids.length} 条记忆切片吗？此操作不可恢复。`)) return;

  try {
    const result = await del(`/personas/${name}/memory/diary-slices`, { slice_ids: ids });
    toast(`成功删除 ${result.deleted} 条记忆切片`);
    await loadSlices();
  } catch (e) {
    toast('删除失败: ' + e.message, 'error');
  }
}

function formatTime(ts) {
  try {
    const d = new Date(ts);
    return d.toLocaleDateString('zh-CN', { month: '2-digit', day: '2-digit' }) + ' ' +
           d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
  } catch { return ts; }
}

function esc(s) {
  const d = document.createElement('div');
  d.textContent = s || '';
  return d.innerHTML;
}
