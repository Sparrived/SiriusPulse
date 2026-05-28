import { store } from '../store.js';
import { get, post } from '../app.js';
import { toast, flashSuccess, $ } from '../components.js';

let currentModal = null;
let bioData = null;

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
          <div class="card-title">传记概览</div>
        </div>
        <button class="btn btn-sm" id="refreshBio">刷新</button>
      </div>
      <div class="stat-grid" id="bioStats" style="padding:16px"></div>
    </div>
    <div class="card" style="margin-bottom:20px">
      <div class="card-header">
        <div class="card-title">传记卡片</div>
      </div>
      <div id="bioCards" style="padding:16px">
        <div style="color:var(--text-3)">加载中...</div>
      </div>
    </div>
    <div class="card">
      <div class="card-header">
        <div class="card-title">别名管理</div>
      </div>
      <div id="aliasSection" style="padding:16px">
        <div style="color:var(--text-3)">加载中...</div>
      </div>
    </div>
  `;

  $('refreshBio').addEventListener('click', () => loadBiography());
  await loadBiography();
}

async function loadBiography() {
  const name = store.currentPersona;
  try {
    bioData = await get(`/personas/${name}/biography`);
    const aliasIndex = bioData.alias_index || {};
    const aliases = [];
    for (const [alias, entries] of Object.entries(aliasIndex)) {
      for (const entry of entries) {
        aliases.push({
          alias,
          user_id: entry.user_id || '',
          weight: entry.weight,
          source: entry.source || '',
        });
      }
    }
    bioData.aliases = aliases;
    renderStats();
    renderCards();
    renderAliases();
  } catch (e) {
    toast('加载传记数据失败: ' + e.message, 'error');
  }
}

function renderStats() {
  const cards = bioData.cards || [];
  const totalDistilled = cards.reduce((sum, c) => sum + (c.distilled_points || []).length, 0);
  const aliasCount = (bioData.aliases || []).length;
  const lastUpdate = cards.length
    ? cards.reduce((max, c) => {
        const t = c.last_updated || '';
        return t > max ? t : max;
      }, '')
    : '';

  $('bioStats').innerHTML = `
    <div class="stat-card">
      <div class="stat-label">传记卡片</div>
      <div class="stat-value">${cards.length}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">提炼要点</div>
      <div class="stat-value">${totalDistilled}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">别名数量</div>
      <div class="stat-value">${aliasCount}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">最近更新</div>
      <div class="stat-value" style="font-size:14px">${lastUpdate ? new Date(lastUpdate).toLocaleString('zh-CN') : '—'}</div>
    </div>
  `;
}

function renderCards() {
  const cards = bioData.cards || [];
  const el = $('bioCards');
  if (!cards.length) {
    el.innerHTML = '<div style="padding:40px;text-align:center;color:var(--text-3)">暂无传记卡片</div>';
    return;
  }
  el.innerHTML = `<div style="display:grid;gap:12px">${cards.map(c => {
    const anchors = (c.identity_anchors || []).slice(0, 3);
    const bio = (c.short_bio || '').slice(0, 80);
    const rels = c.relationships || [];
    const pendingCount = (c.pending_messages || []).length;
    const distilledCount = (c.distilled_points || []).length;
    return `
      <div class="card" style="margin:0;cursor:pointer" data-user-id="${c.user_id}">
        <div style="padding:16px">
          <div style="display:flex;justify-content:space-between;align-items:flex-start;margin-bottom:8px">
            <div>
              <span style="font-size:15px;font-weight:600">${c.name || c.user_id || '未知'}</span>
              ${c.user_id ? `<span style="font-size:12px;color:var(--text-3);margin-left:8px">${c.user_id}</span>` : ''}
            </div>
            <div style="display:flex;gap:6px">
              ${pendingCount > 0 ? `<span class="tag tag-accent" style="font-size:11px">${pendingCount} 待处理</span>` : ''}
              <span class="tag" style="font-size:11px">${distilledCount} 要点</span>
            </div>
          </div>
          ${anchors.length ? `<div style="display:flex;gap:6px;flex-wrap:wrap;margin-bottom:8px">${anchors.map(a => `<span class="tag" style="font-size:11px">${a}</span>`).join('')}</div>` : ''}
          ${bio ? `<div style="font-size:13px;color:var(--text-2);line-height:1.5;margin-bottom:8px">${bio}${(c.short_bio || '').length > 80 ? '...' : ''}</div>` : ''}
          ${rels.length ? `<div style="font-size:12px;color:var(--text-3)">关系: ${rels.slice(0, 3).map(r => r.name || r.user_id || '').join(', ')}${rels.length > 3 ? ' ...' : ''}</div>` : ''}
        </div>
      </div>
    `;
  }).join('')}</div>`;

  el.querySelectorAll('[data-user-id]').forEach(card => {
    card.addEventListener('click', () => openDetailModal(card.dataset.userId));
  });
}

function openDetailModal(userId) {
  const cards = bioData.cards || [];
  const card = cards.find(c => c.user_id === userId) || cards.find(c => (c.name || '') === userId);
  if (!card) return;
  closeModal();

  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay';
  overlay.innerHTML = `
    <div class="modal" style="max-width:650px;max-height:85vh;overflow-y:auto">
      <div class="modal-header">
        <span style="font-size:16px;font-weight:600">${card.name || card.user_id || '详情'}</span>
        <button class="btn btn-sm" id="modalClose">✕</button>
      </div>
      <div class="modal-body" id="modalBody"></div>
    </div>
  `;
  document.body.appendChild(overlay);
  currentModal = overlay;
  overlay.addEventListener('click', (e) => { if (e.target === overlay) closeModal(); });
  $('modalClose').addEventListener('click', closeModal);

  const anchors = card.identity_anchors || [];
  const rels = card.relationships || [];
  const distilled = card.distilled_points || [];
  const pending = card.pending_messages || [];

  $('modalBody').innerHTML = `
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:16px">
      <div>
        <div style="font-size:12px;color:var(--text-3)">User ID</div>
        <div style="font-size:14px">${card.user_id || '—'}</div>
      </div>
      <div>
        <div style="font-size:12px;color:var(--text-3)">别名</div>
        <div style="font-size:14px">${(card.aliases || []).join(', ') || '—'}</div>
      </div>
      <div>
        <div style="font-size:12px;color:var(--text-3)">最近更新</div>
        <div style="font-size:14px">${card.last_updated ? new Date(card.last_updated).toLocaleString('zh-CN') : '—'}</div>
      </div>
      <div>
        <div style="font-size:12px;color:var(--text-3)">最近提炼</div>
        <div style="font-size:14px">${card.last_distilled ? new Date(card.last_distilled).toLocaleString('zh-CN') : '—'}</div>
      </div>
    </div>

    ${anchors.length ? `
      <div style="margin-bottom:16px">
        <div style="font-size:14px;font-weight:600;margin-bottom:8px">身份锚点</div>
        <div style="display:flex;gap:6px;flex-wrap:wrap">${anchors.map(a => `<span class="tag">${a}</span>`).join('')}</div>
      </div>
    ` : ''}

    ${card.short_bio ? `
      <div style="margin-bottom:16px">
        <div style="font-size:14px;font-weight:600;margin-bottom:8px">简要传记</div>
        <div style="font-size:13px;color:var(--text-2);line-height:1.6;white-space:pre-wrap">${card.short_bio}</div>
      </div>
    ` : ''}

    ${rels.length ? `
      <div style="margin-bottom:16px">
        <div style="font-size:14px;font-weight:600;margin-bottom:8px">关系列表</div>
        <div style="display:grid;gap:6px">${rels.map(r => `
          <div style="padding:8px 12px;background:var(--surface-2,rgba(255,255,255,0.03));border-radius:6px;font-size:13px;display:flex;justify-content:space-between">
            <span>${r.name || r.user_id || '—'}</span>
            <span style="color:var(--text-3)">${r.relation || ''}</span>
          </div>
        `).join('')}</div>
      </div>
    ` : ''}

    ${distilled.length ? `
      <div style="margin-bottom:16px">
        <div style="font-size:14px;font-weight:600;margin-bottom:8px">提炼要点 (${distilled.length})</div>
        <div style="display:grid;gap:6px">${distilled.map(dp => `
          <div style="padding:8px 12px;background:var(--surface-2,rgba(255,255,255,0.03));border-radius:6px;font-size:13px;color:var(--text-2)">${typeof dp === 'string' ? dp : dp.point || JSON.stringify(dp)}</div>
        `).join('')}</div>
      </div>
    ` : ''}

    ${pending.length ? `
      <div>
        <div style="font-size:14px;font-weight:600;margin-bottom:8px">待处理消息 (${pending.length})</div>
        <div style="max-height:250px;overflow-y:auto;display:grid;gap:6px">${pending.map(pm => `
          <div style="padding:8px 12px;background:var(--surface-2,rgba(255,255,255,0.03));border-radius:6px;font-size:12px;color:var(--text-2)">${typeof pm === 'string' ? pm : pm.content || JSON.stringify(pm)}</div>
        `).join('')}</div>
      </div>
    ` : ''}
  `;
}

function renderAliases() {
  const aliases = bioData.aliases || [];
  const name = store.currentPersona;
  const el = $('aliasSection');

  let tableHtml = '';
  if (aliases.length) {
    tableHtml = `
      <div style="overflow-x:auto;margin-bottom:16px">
        <table style="width:100%;border-collapse:collapse;font-size:13px">
          <thead>
            <tr style="border-bottom:1px solid var(--border)">
              <th style="padding:8px 12px;text-align:left;color:var(--text-3)">别名</th>
              <th style="padding:8px 12px;text-align:left;color:var(--text-3)">用户 ID</th>
              <th style="padding:8px 12px;text-align:left;color:var(--text-3)">权重</th>
              <th style="padding:8px 12px;text-align:left;color:var(--text-3)">来源</th>
              <th style="padding:8px 12px;text-align:right;color:var(--text-3)">操作</th>
            </tr>
          </thead>
          <tbody>
            ${aliases.map(a => `
              <tr style="border-bottom:1px solid var(--border)">
                <td style="padding:8px 12px">${a.alias || a.name || ''}</td>
                <td style="padding:8px 12px;color:var(--text-2)">${a.user_id || ''}</td>
                <td style="padding:8px 12px;color:var(--text-2)">${a.weight != null ? a.weight : '—'}</td>
                <td style="padding:8px 12px;color:var(--text-2)">${a.source || '—'}</td>
                <td style="padding:8px 12px;text-align:right">
                  <button class="btn btn-sm delete-alias-btn" data-alias="${a.alias || a.name}" data-user-id="${a.user_id || ''}">删除</button>
                </td>
              </tr>
            `).join('')}
          </tbody>
        </table>
      </div>
    `;
  } else {
    tableHtml = '<div style="padding:20px;text-align:center;color:var(--text-3);margin-bottom:16px">暂无别名</div>';
  }

  el.innerHTML = `
    ${tableHtml}
    <div style="display:flex;gap:12px;align-items:flex-end;flex-wrap:wrap">
      <div class="form-group" style="margin:0;flex:1;min-width:150px">
        <label>别名</label>
        <input type="text" id="newAlias" placeholder="新别名">
      </div>
      <div class="form-group" style="margin:0;flex:1;min-width:150px">
        <label>用户 ID</label>
        <input type="text" id="newAliasUserId" placeholder="用户 ID">
      </div>
      <div class="form-group" style="margin:0;flex:1;min-width:150px">
        <label>用户名</label>
        <input type="text" id="newAliasUserName" placeholder="用户名（可选）">
      </div>
      <button class="btn btn-primary" id="addAliasBtn" style="white-space:nowrap">添加别名</button>
    </div>
  `;

  $('addAliasBtn').addEventListener('click', addAlias);
  el.querySelectorAll('.delete-alias-btn').forEach(btn => {
    btn.addEventListener('click', () => deleteAlias(btn.dataset.alias, btn.dataset.userId));
  });
}

async function addAlias() {
  const name = store.currentPersona;
  const alias = $('newAlias').value.trim();
  const userId = $('newAliasUserId').value.trim();
  const userName = $('newAliasUserName').value.trim();
  if (!alias || !userId) {
    toast('请填写别名和用户 ID', 'error');
    return;
  }
  try {
    await post(`/personas/${name}/biography/aliases`, { action: 'add', alias, user_id: userId, user_name: userName });
    toast('别名添加成功');
    await loadBiography();
  } catch (e) {
    toast('添加失败: ' + e.message, 'error');
  }
}

async function deleteAlias(alias, userId) {
  const name = store.currentPersona;
  try {
    await post(`/personas/${name}/biography/aliases`, { action: 'delete', alias, user_id: userId });
    toast('别名已删除');
    await loadBiography();
  } catch (e) {
    toast('删除失败: ' + e.message, 'error');
  }
}

function closeModal() {
  if (currentModal) {
    currentModal.remove();
    currentModal = null;
  }
}
