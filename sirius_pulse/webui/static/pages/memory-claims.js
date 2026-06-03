import { store } from '../store.js';
import { get } from '../app.js';
import { toast, $ } from '../components.js';
import { renderNeuralNav, consumeNavParams, showParamHint, navigateWithParams } from './memory-nav.js';

let allClaims = [];
let lastStats = {};
let debounceTimer = null;

export async function init(container) {
  const name = store.currentPersona;
  if (!name) {
    container.innerHTML = '<div class="claims-empty">请先选择人格</div>';
    return;
  }

  renderNeuralNav('memory-claims');
  $('claimsRefreshBtn')?.addEventListener('click', () => loadClaims());
  $('claimsSearch')?.addEventListener('input', () => {
    clearTimeout(debounceTimer);
    debounceTimer = setTimeout(() => filterAndRender(), 180);
  });
  for (const id of ['claimsStatus', 'claimsType', 'claimsAttribution']) {
    $(id)?.addEventListener('change', () => loadClaims());
  }

  const params = consumeNavParams();
  if (params?.userId) {
    const search = $('claimsSearch');
    if (search) search.value = params.userId;
    showParamHint(`用户: ${params.userName || params.userId}`, () => {
      const el = $('claimsSearch');
      if (el) el.value = '';
      filterAndRender();
    });
  }
  if (params?.claimId) {
    showParamHint(`Claim: ${params.claimId}`, null);
  }

  await loadClaims(params?.claimId);
}

async function loadClaims(highlightClaimId) {
  const name = store.currentPersona;
  if (!name) return;
  try {
    const qs = new URLSearchParams({ limit: '300' });
    const status = $('claimsStatus')?.value || '';
    const type = $('claimsType')?.value || '';
    const attribution = $('claimsAttribution')?.value || '';
    if (status) qs.set('status', status);
    if (type) qs.set('fact_type', type);
    if (attribution) qs.set('attribution', attribution);

    const data = await get(`/personas/${name}/memory/claims?${qs}`);
    allClaims = data.claims || [];
    lastStats = data.stats || {};
    renderStats();
    filterAndRender(highlightClaimId);
  } catch (e) {
    toast('加载证据账本失败: ' + e.message, 'error');
  }
}

function renderStats() {
  const el = $('claimsStats');
  if (!el) return;
  const byStatus = lastStats.by_status || {};
  el.innerHTML = [
    ['Claims', lastStats.total_claims || 0],
    ['Evidence', lastStats.total_evidence || 0],
    ['Active', byStatus.active || 0],
    ['Candidate', byStatus.candidate || 0],
  ].map(([label, value]) => `
    <div class="claims-stat">
      <div class="claims-stat-label">${esc(label)}</div>
      <div class="claims-stat-value">${Number(value || 0).toLocaleString()}</div>
    </div>
  `).join('');
}

function filterAndRender(highlightClaimId) {
  const q = ($('claimsSearch')?.value || '').trim().toLowerCase();
  let claims = allClaims;
  if (q) {
    claims = claims.filter(c => [
      c.claim_id,
      c.subject_user_id,
      c.subject_label,
      c.fact_type,
      c.predicate,
      c.object_value,
      c.value,
      c.source_group_id,
      c.source_situation_id,
    ].some(v => String(v || '').toLowerCase().includes(q)));
  }
  renderClaims(claims, highlightClaimId);
}

function renderClaims(claims, highlightClaimId) {
  const list = $('claimsList');
  if (!list) return;
  if (!claims.length) {
    list.innerHTML = '<div class="claims-empty">暂无匹配的 claim</div>';
    return;
  }

  list.innerHTML = claims.map(claim => {
    const conf = Math.round((claim.confidence || 0) * 100);
    const highlighted = highlightClaimId && claim.claim_id === highlightClaimId;
    const value = claim.value || [claim.predicate, claim.object_value].filter(Boolean).join(' ');
    return `
      <div class="claim-row" data-claim-id="${esc(claim.claim_id)}" ${highlighted ? 'style="border-color:rgba(0,255,200,0.55);box-shadow:0 0 24px rgba(0,255,200,0.15)"' : ''}>
        <div class="claim-main">
          <div>
            <div class="claim-subject">${esc(claim.subject_label || claim.subject_user_id || 'unknown')}</div>
            <div class="claim-user-id">${esc(claim.subject_user_id || claim.claim_id)}</div>
          </div>
          <div>
            <div class="claim-value">${esc(value)}</div>
            <div class="claim-meta">
              <span class="claim-pill ${esc(claim.status)}">${esc(claim.status)}</span>
              <span class="claim-pill">${esc(claim.fact_type || 'other')}</span>
              <span class="claim-pill">${esc(claim.attribution || 'inferred')}</span>
              <span class="claim-pill">${conf}%</span>
              ${claim.profile_safe ? '<span class="claim-pill active">profile-safe</span>' : ''}
              ${claim.source ? `<span class="claim-pill">${esc(claim.source)}</span>` : ''}
              ${claim.observed_at ? `<span class="claim-pill">${formatDate(claim.observed_at)}</span>` : ''}
            </div>
          </div>
          <div class="claim-actions">
            <button class="claims-btn claim-evidence-btn" data-claim-id="${esc(claim.claim_id)}">证据</button>
          </div>
        </div>
        <div class="claim-evidence" id="claim-ev-${esc(claim.claim_id)}"></div>
      </div>
    `;
  }).join('');

  list.querySelectorAll('.claim-evidence-btn').forEach(btn => {
    btn.addEventListener('click', (e) => {
      e.stopPropagation();
      toggleEvidence(btn.dataset.claimId);
    });
  });

  list.querySelectorAll('.claim-row').forEach(row => {
    row.addEventListener('dblclick', () => {
      const claim = allClaims.find(c => c.claim_id === row.dataset.claimId);
      if (claim?.subject_user_id) {
        navigateWithParams('biography-view', {
          userId: claim.subject_user_id,
          userName: claim.subject_label,
        });
      }
    });
  });

  if (highlightClaimId) {
    const target = list.querySelector(`[data-claim-id="${highlightClaimId}"]`);
    if (target) {
      setTimeout(() => {
        target.scrollIntoView({ behavior: 'smooth', block: 'center' });
        toggleEvidence(highlightClaimId);
      }, 180);
    }
  }
}

async function toggleEvidence(claimId) {
  const el = $('claim-ev-' + claimId);
  if (!el) return;
  if (el.classList.contains('open')) {
    el.classList.remove('open');
    return;
  }

  el.innerHTML = '<div class="claim-evidence-run">加载证据链...</div>';
  el.classList.add('open');
  const name = store.currentPersona;
  try {
    const data = await get(`/personas/${name}/memory/claims/${claimId}/provenance`);
    const run = data.extraction_run || {};
    const evidence = data.evidence || [];
    el.innerHTML = `
      <div class="claim-evidence-run">
        ${run.task ? `<span>task: ${esc(run.task)}</span>` : ''}
        ${run.model ? `<span>model: ${esc(run.model)}</span>` : ''}
        ${run.prompt_version ? `<span>prompt: ${esc(run.prompt_version)}</span>` : ''}
        ${run.created_at ? `<span>${formatDate(run.created_at)}</span>` : ''}
      </div>
      ${evidence.length ? evidence.map(item => `
        <div class="claim-evidence-item">
          <div class="claim-evidence-speaker">
            ${esc(item.speaker_name || item.speaker_user_id || item.source_type || 'evidence')}
            ${item.group_id ? ` · ${esc(item.group_id)}` : ''}
            ${item.observed_at ? ` · ${formatDate(item.observed_at)}` : ''}
          </div>
          <div class="claim-evidence-quote">${esc(item.content_quote || item.message_id || item.content_digest)}</div>
        </div>
      `).join('') : '<div class="claim-evidence-run">这条 claim 暂无原文快照，通常来自旧数据迁移或手工记录。</div>'}
    `;
  } catch (e) {
    el.innerHTML = '<div class="claim-evidence-run" style="color:var(--danger)">加载证据链失败</div>';
  }
}

function formatDate(ts) {
  if (!ts) return '';
  try {
    return new Date(ts).toLocaleString('zh-CN', {
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
    });
  } catch {
    return ts;
  }
}

function esc(s) {
  const d = document.createElement('div');
  d.textContent = s || '';
  return d.innerHTML;
}
