import { store } from '../store.js';
import { get, del } from '../app.js';
import { toast, $ } from '../components.js';
import { renderNeuralNav, consumeNavParams, showParamHint, navigateWithParams } from './memory-nav.js';

let cachedSituations = [];

export async function init(container) {
  const name = store.currentPersona;
  if (!name) {
    container.innerHTML = '<div class="sit-empty">请先选择人格</div>';
    return;
  }

  renderNeuralNav('situation-timeline');
  $('sitRefreshBtn')?.addEventListener('click', () => loadSituations());
  $('sitSelectAll')?.addEventListener('change', handleSelectAll);
  $('sitDeleteBtn')?.addEventListener('click', handleDeleteSelected);

  const params = consumeNavParams();
  if (params?.topic) {
    showParamHint(`话题: ${params.topic}`, () => { renderTimeline(cachedSituations); });
  }

  await loadSituations(params?.topic);
}

async function loadSituations(topicFilter) {
  const name = store.currentPersona;
  if (!name) return;
  try {
    const data = await get(`/personas/${name}/memory/situations?limit=200`);
    cachedSituations = data.situations || [];
    let filtered = cachedSituations;
    if (topicFilter) {
      filtered = cachedSituations.filter(s =>
        (s.topics || []).some(t => t.includes(topicFilter) || topicFilter.includes(t))
      );
    }
    const cnt = $('sitCount');
    if (cnt) cnt.textContent = filtered.length;
    renderTimeline(filtered);
  } catch (e) {
    toast('加载情景时间线失败: ' + e.message, 'error');
  }
}

function renderTimeline(situations) {
  const timeline = $('sitTimeline');
  if (!timeline) return;

  if (!situations.length) {
    timeline.innerHTML = '<div class="sit-empty">暂无情景记录</div>';
    return;
  }

  timeline.innerHTML = situations.map((s, i) => {
    const time = s.created_at ? formatTime(s.created_at) : '';
    const topics = (s.topics || []).map(t => `<span class="sit-tag mem-clickable-topic" data-topic="${esc(t)}">${esc(t)}</span>`).join('');
    const triples = (s.triples || []).map(t =>
      `<div class="sit-triple-row">
        <span class="sit-triple-s">${esc(t.subject)}</span>
        <span class="sit-triple-arrow">→</span>
        <span class="sit-triple-p">${esc(t.predicate)}</span>
        <span class="sit-triple-arrow">→</span>
        <span class="sit-triple-o">${esc(t.obj)}</span>
      </div>`
    ).join('');

    return `
    <div class="sit-node" style="animation-delay:${i * 0.06}s">
      <input type="checkbox" class="sit-checkbox" data-id="${s.situation_id}">
      <div class="sit-card" data-id="${s.situation_id}">
        <div class="sit-time">${time}</div>
        <div class="sit-summary">${esc(s.summary)}</div>
        ${topics ? `<div class="sit-tags">${topics}</div>` : ''}
        <div class="sit-stats">
          <span class="sit-stat-item"><span class="sit-stat-dot ok"></span>验证 ${s.validated_triple_count || 0}</span>
          <span class="sit-stat-item"><span class="sit-stat-dot reject"></span>拒绝 ${s.rejected_triple_count || 0}</span>
          <span class="sit-stat-item">参与者 ${(s.participants || []).length}</span>
          ${s.group_id ? `<span class="sit-stat-item">${esc(s.group_id)}</span>` : ''}
        </div>
        ${triples ? `<div class="sit-triples" id="triples-${s.situation_id}">${triples}</div>` : ''}
      </div>
    </div>`;
  }).join('');

  timeline.querySelectorAll('.sit-card').forEach(card => {
    card.addEventListener('click', () => {
      const id = card.dataset.id;
      const el = $('triples-' + id);
      if (el) el.classList.toggle('open');
    });
  });

  timeline.querySelectorAll('.sit-checkbox').forEach(cb => {
    cb.addEventListener('change', updateDeleteButton);
  });

  timeline.querySelectorAll('.mem-clickable-topic').forEach(el => {
    el.addEventListener('click', (e) => {
      e.stopPropagation();
      navigateWithParams('evolution-chain', { subject: el.dataset.topic });
    });
  });

  updateDeleteButton();
}

function handleSelectAll(e) {
  const checked = e.target.checked;
  document.querySelectorAll('.sit-checkbox').forEach(cb => {
    cb.checked = checked;
  });
  updateDeleteButton();
}

function updateDeleteButton() {
  const selected = document.querySelectorAll('.sit-checkbox:checked');
  const btn = $('sitDeleteBtn');
  if (btn) {
    btn.classList.toggle('visible', selected.length > 0);
    btn.textContent = selected.length > 0 ? `删除选中 (${selected.length})` : '删除选中';
  }
}

function getSelectedIds() {
  return Array.from(document.querySelectorAll('.sit-checkbox:checked')).map(cb => cb.dataset.id);
}

async function handleDeleteSelected() {
  const ids = getSelectedIds();
  if (!ids.length) return;

  const name = store.currentPersona;
  if (!name) return;

  if (!confirm(`确定要删除 ${ids.length} 条情景记录吗？此操作不可恢复。`)) return;

  try {
    const result = await del(`/personas/${name}/memory/situations`, { situation_ids: ids });
    toast(`成功删除 ${result.deleted} 条情景记录`);
    await loadSituations();
  } catch (e) {
    toast('删除失败: ' + e.message, 'error');
  }
}

function formatTime(ts) {
  try {
    const d = new Date(ts);
    const now = Date.now();
    const diff = (now - d.getTime()) / 1000;
    if (diff < 60) return '刚刚';
    if (diff < 3600) return Math.floor(diff / 60) + '分钟前';
    if (diff < 86400) return Math.floor(diff / 3600) + '小时前';
    return d.toLocaleDateString('zh-CN') + ' ' + d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit' });
  } catch { return ts; }
}

function esc(s) {
  const d = document.createElement('div');
  d.textContent = s || '';
  return d.innerHTML;
}
