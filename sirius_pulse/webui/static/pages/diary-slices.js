import { store } from '../store.js';
import { get } from '../app.js';
import { toast, $ } from '../components.js';
import { renderNeuralNav } from './memory-nav.js';

let debounceTimer = null;

export async function init(container) {
  const name = store.currentPersona;
  if (!name) {
    container.innerHTML = '<div class="ds-empty">请先选择人格</div>';
    return;
  }

  renderNeuralNav('diary-slices');
  $('dsRefreshBtn')?.addEventListener('click', () => loadSlices());
  $('dsSearch')?.addEventListener('input', () => {
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(() => loadSlices(), 300);
  });

  await loadSlices();
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

    return `
    <div class="ds-slice">
      <div class="ds-slice-summary">${esc(summary)}</div>
      ${content && content !== summary ? `<div class="ds-slice-content" onclick="this.classList.toggle('expanded')">${esc(content)}</div>` : ''}
      ${topics ? `<div class="ds-slice-topics">${topics}</div>` : ''}
      ${subjects ? `<div class="ds-slice-subjects">${subjects}</div>` : ''}
      <div class="ds-slice-meta">
        ${timeRange ? `<span>${timeRange}</span>` : ''}
        ${group ? `<span>${esc(group)}</span>` : ''}
        ${participants.length ? `<span>${participants.length} 人参与</span>` : ''}
      </div>
    </div>`;
  }).join('');
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
