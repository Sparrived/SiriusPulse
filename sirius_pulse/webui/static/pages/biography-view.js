import { store } from '../store.js';
import { get } from '../app.js';
import { toast, $, animateNumber } from '../components.js';
import { renderNeuralNav, consumeNavParams, showParamHint, navigateWithParams } from './memory-nav.js';

export async function init(container) {
  const name = store.currentPersona;
  if (!name) {
    container.innerHTML = '<div class="bio-empty">请先选择人格</div>';
    return;
  }

  renderNeuralNav('biography-view');
  $('bioViewRefreshBtn')?.addEventListener('click', () => loadBiographies());

  const params = consumeNavParams();
  if (params?.userId) {
    showParamHint(`用户: ${params.userName || params.userId}`, null);
  }

  await loadBiographies(params?.userId);
}

async function loadBiographies(highlightUserId) {
  const name = store.currentPersona;
  if (!name) return;
  try {
    const data = await get(`/personas/${name}/memory/biographies`);
    const bios = data.biographies || [];
    renderBios(bios, highlightUserId);
  } catch (e) {
    toast('加载传记失败: ' + e.message, 'error');
  }
}

function renderBios(bios, highlightUserId) {
  const grid = $('bioGrid');
  if (!grid) return;

  if (!bios.length) {
    grid.innerHTML = '<div class="bio-empty">暂无用户传记数据。当演化链中有带 subject_user_id 的记录时，传记将自动生成。</div>';
    return;
  }

  grid.innerHTML = bios.map(b => {
    const isHighlight = highlightUserId && b.user_id === highlightUserId;
    const anchors = (b.identity_anchors || []).map(a => `<span class="bio-anchor">${esc(a)}</span>`).join('');
    const rels = (b.relationships || []).map(r =>
      `<div class="bio-rel-item"><span class="bio-rel-dot"></span>${esc(r.target || '')} · ${esc(r.relation || '')}</div>`
    ).join('');

    return `
    <div class="bio-user-card" data-uid="${b.user_id}" data-name="${esc(b.name)}" ${isHighlight ? 'style="border-color:rgba(0,255,200,0.5);box-shadow:0 0 24px rgba(0,255,200,0.15)"' : ''}>
      <div class="bio-user-name">${esc(b.name || b.user_id)}${isHighlight ? ' <span style="font-size:11px;color:#00ffc8;margin-left:6px">← 联动目标</span>' : ''}</div>
      <div class="bio-user-bio">${esc(b.short_bio || '暂无传记摘要')}</div>
      ${anchors ? `<div class="bio-anchors">${anchors}</div>` : ''}
      ${rels ? `<div class="bio-relationships"><div class="bio-rel-title">关系</div>${rels}</div>` : ''}
      <div class="bio-fact-stats">
        <span class="bio-fact-stat"><span class="bio-fact-dot active"></span><span class="bio-fact-count">${b.active_fact_count || 0}</span> 活跃</span>
        <span class="bio-fact-stat"><span class="bio-fact-dot superseded"></span><span class="bio-fact-count">${b.superseded_fact_count || 0}</span> 取代</span>
        <span class="bio-fact-stat"><span class="bio-fact-dot uncertain"></span><span class="bio-fact-count">${b.uncertain_fact_count || 0}</span> 待定</span>
      </div>
      <div class="bio-gaps" id="gaps-${b.user_id}"></div>
    </div>`;
  }).join('');

  grid.querySelectorAll('.bio-user-card').forEach(card => {
    card.style.cursor = 'pointer';
    card.addEventListener('click', () => toggleGaps(card));
  });

  if (highlightUserId) {
    const target = grid.querySelector(`[data-uid="${highlightUserId}"]`);
    if (target) {
      setTimeout(() => target.scrollIntoView({ behavior: 'smooth', block: 'center' }), 200);
    }
  }
}

async function toggleGaps(card) {
  const uid = card.dataset.uid;
  const gapsEl = $('gaps-' + uid);
  if (!gapsEl) return;

  if (gapsEl.classList.contains('open')) {
    gapsEl.classList.remove('open');
    return;
  }

  const name = store.currentPersona;
  try {
    const data = await get(`/personas/${name}/memory/gaps/${uid}`);
    const gaps = data.gaps || [];
    if (!gaps.length) {
      gapsEl.innerHTML = '<div style="font-size:12px;color:var(--text-3)">无知识缺口</div>';
    } else {
      gapsEl.innerHTML = gaps.map(g =>
        `<div class="bio-gap-item">
          <span class="bio-gap-badge ${g.importance}">${g.importance}</span>
          <span>${esc(g.description)}</span>
        </div>`
      ).join('');
    }
    gapsEl.classList.add('open');
  } catch (e) {
    gapsEl.innerHTML = '<div style="font-size:12px;color:var(--danger)">加载缺口数据失败</div>';
    gapsEl.classList.add('open');
  }
}

function esc(s) {
  const d = document.createElement('div');
  d.textContent = s || '';
  return d.innerHTML;
}
