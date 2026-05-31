import { store } from '../store.js';
import { get } from '../app.js';
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
    $('sitCount').textContent = filtered.length;
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

  timeline.querySelectorAll('.mem-clickable-topic').forEach(el => {
    el.addEventListener('click', (e) => {
      e.stopPropagation();
      navigateWithParams('evolution-chain', { subject: el.dataset.topic });
    });
  });
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
