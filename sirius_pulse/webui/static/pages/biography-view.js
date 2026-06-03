import { store } from '../store.js';
import { get, post } from '../app.js';
import { toast, $, animateNumber } from '../components.js';
import { renderNeuralNav, consumeNavParams, showParamHint, navigateWithParams } from './memory-nav.js';

let allBios = [];
let debounceTimer = null;

export async function init(container) {
  const name = store.currentPersona;
  if (!name) {
    container.innerHTML = '<div class="bio-empty">请先选择人格</div>';
    return;
  }

  renderNeuralNav('biography-view');
  $('bioViewRefreshBtn')?.addEventListener('click', () => loadBiographies());
  $('bioSearch')?.addEventListener('input', () => {
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(() => filterAndRender(), 250);
  });
  $('bioSort')?.addEventListener('change', () => filterAndRender());

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
    allBios = data.biographies || [];
    filterAndRender(highlightUserId);
  } catch (e) {
    toast('加载传记失败: ' + e.message, 'error');
  }
}

function filterAndRender(highlightUserId) {
  const query = ($('bioSearch')?.value || '').trim().toLowerCase();
  const sortBy = $('bioSort')?.value || 'facts';

  let filtered = allBios;

  // 搜索过滤
  if (query) {
    filtered = filtered.filter(b => {
      const name = (b.name || '').toLowerCase();
      const userId = (b.user_id || '').toLowerCase();
      const bio = (b.short_bio || '').toLowerCase();
      const aliases = (b.aliases || []).join(' ').toLowerCase();
      const anchors = (b.identity_anchors || []).join(' ').toLowerCase();
      return name.includes(query) || userId.includes(query) || bio.includes(query) ||
             aliases.includes(query) || anchors.includes(query);
    });
  }

  // 排序
  switch (sortBy) {
    case 'name':
      filtered.sort((a, b) => (a.name || a.user_id || '').localeCompare(b.name || b.user_id || ''));
      break;
    case 'recent':
      filtered.sort((a, b) => (b.generated_at || '').localeCompare(a.generated_at || ''));
      break;
    case 'facts':
    default:
      filtered.sort((a, b) => (b.active_fact_count || 0) - (a.active_fact_count || 0));
      break;
  }

  renderStats(filtered);
  renderBios(filtered, highlightUserId);
}

function renderStats(bios) {
  const el = $('bioStats');
  if (!el) return;

  const totalAliases = bios.reduce((sum, b) => sum + (b.aliases || []).length, 0);
  const totalFacts = bios.reduce((sum, b) => sum + (b.active_fact_count || 0), 0);
  const totalClaims = bios.reduce((sum, b) => sum + (b.source_claim_ids || []).length, 0);

  el.innerHTML = `
    <span class="bio-stat-item">用户 <span class="bio-stat-value">${bios.length}</span></span>
    <span class="bio-stat-item">别名 <span class="bio-stat-value">${totalAliases}</span></span>
    <span class="bio-stat-item">活跃事实 <span class="bio-stat-value">${totalFacts}</span></span>
    <span class="bio-stat-item">画像 claims <span class="bio-stat-value">${totalClaims}</span></span>
  `;
}

function renderBios(bios, highlightUserId) {
  const grid = $('bioGrid');
  if (!grid) return;

  if (!bios.length) {
    grid.innerHTML = '<div class="bio-empty">暂无匹配的用户传记数据</div>';
    return;
  }

  grid.innerHTML = bios.map(b => {
    const isHighlight = highlightUserId && b.user_id === highlightUserId;
    const anchors = (b.identity_anchors || []).map(a => `<span class="bio-anchor">${esc(a)}</span>`).join('');
    const rels = (b.relationships || []).map(r =>
      `<div class="bio-rel-item"><span class="bio-rel-dot"></span>${esc(r.target || '')} · ${esc(r.relation || '')}</div>`
    ).join('');
    const aliases = (b.aliases || []).map(a =>
      `<span class="bio-alias">${esc(a)}<span class="bio-alias-delete" data-alias="${esc(a)}" data-uid="${b.user_id}" title="标记为 shadow">×</span></span>`
    ).join('');
    const claimCount = (b.source_claim_ids || []).length;
    const firstClaimId = claimCount ? b.source_claim_ids[0] : '';

    return `
    <div class="bio-user-card" data-uid="${b.user_id}" data-name="${esc(b.name)}" ${isHighlight ? 'style="border-color:rgba(0,255,200,0.5);box-shadow:0 0 24px rgba(0,255,200,0.15)"' : ''}>
      <div class="bio-user-header">
        <div class="bio-user-name">${esc(b.name || b.user_id)}${isHighlight ? ' <span style="font-size:11px;color:#00ffc8;margin-left:6px">← 联动目标</span>' : ''}</div>
        <div class="bio-user-id">${esc(b.user_id)}</div>
      </div>
      ${aliases ? `<div class="bio-aliases"><span class="bio-aliases-label">别名</span>${aliases}</div>` : ''}
      <div class="bio-user-bio">${esc(b.short_bio || '暂无传记摘要')}</div>
      ${anchors ? `<div class="bio-anchors">${anchors}</div>` : ''}
      ${rels ? `<div class="bio-relationships"><div class="bio-rel-title">关系</div>${rels}</div>` : ''}
      <div class="bio-fact-stats">
        <span class="bio-fact-stat"><span class="bio-fact-dot active"></span><span class="bio-fact-count">${b.active_fact_count || 0}</span> 活跃</span>
        <span class="bio-fact-stat"><span class="bio-fact-dot superseded"></span><span class="bio-fact-count">${b.superseded_fact_count || 0}</span> 取代</span>
        <span class="bio-fact-stat"><span class="bio-fact-dot uncertain"></span><span class="bio-fact-count">${b.uncertain_fact_count || 0}</span> 待定</span>
        ${claimCount ? `<button class="bio-claim-link" data-uid="${esc(b.user_id)}" data-name="${esc(b.name)}" data-claim-id="${esc(firstClaimId)}">${claimCount} claims</button>` : ''}
      </div>
      <div class="bio-gaps" id="gaps-${b.user_id}"></div>
    </div>`;
  }).join('');

  grid.querySelectorAll('.bio-user-card').forEach(card => {
    card.style.cursor = 'pointer';
    card.addEventListener('click', (e) => {
      if (!e.target.classList.contains('bio-alias-delete')) {
        toggleGaps(card);
      }
    });
  });

  grid.querySelectorAll('.bio-alias-delete').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      shadowAlias(btn.dataset.alias, btn.dataset.uid);
    });
  });

  grid.querySelectorAll('.bio-claim-link').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      navigateWithParams('memory-claims', {
        userId: btn.dataset.uid,
        userName: btn.dataset.name,
        claimId: btn.dataset.claimId,
      });
    });
  });

  if (highlightUserId) {
    const target = grid.querySelector(`[data-uid="${highlightUserId}"]`);
    if (target) {
      setTimeout(() => target.scrollIntoView({ behavior: 'smooth', block: 'center' }), 200);
    }
  }
}

async function shadowAlias(alias, userId) {
  const name = store.currentPersona;
  if (!name) return;

  if (!confirm(`确定要将别名 "${alias}" 标记为 shadow 吗？\n\nShadow 状态的别名将不再参与召回，但保留可追溯性。`)) return;

  try {
    await post(`/personas/${name}/biography/aliases`, {
      action: 'shadow',
      alias,
      user_id: userId,
    });
    toast(`别名 "${alias}" 已标记为 shadow`);
    await loadBiographies();
  } catch (e) {
    toast('操作失败: ' + e.message, 'error');
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
