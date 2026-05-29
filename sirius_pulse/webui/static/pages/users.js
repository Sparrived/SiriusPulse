import { store } from '../store.js';
import { get, post } from '../app.js';
import { toast, flashSuccess, $ } from '../components.js';

let usersData = null;
let bioData = null;
let activeGroup = '';
let currentModal = null;

export async function init(container) {
  const name = store.currentPersona;
  if (!name) {
    container.innerHTML = `
      <div class="card">
        <div style="padding:60px;text-align:center;color:var(--text-3)">
          <div style="font-size:48px;margin-bottom:16px">✦</div>
          <div style="font-size:16px;margin-bottom:8px">请先选择人格</div>
        </div>
      </div>
    `;
    return;
  }

  container.innerHTML = `
    <div class="card" style="margin-bottom:20px">
      <div class="card-header">
        <div class="card-title">用户档案</div>
        <div style="display:flex;gap:12px;align-items:center">
          <select id="usersGroupFilter" class="btn btn-sm">
            <option value="">全部群组</option>
          </select>
          <button class="btn btn-sm" id="refreshAll">刷新</button>
        </div>
      </div>
      <div class="stat-grid" id="unifiedStats"></div>
    </div>
    <div id="usersGrid" style="display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:16px;margin-bottom:20px"></div>
    <div class="card" id="aliasCard">
      <div class="card-header" style="cursor:pointer" id="aliasToggle">
        <div class="card-title">别名管理</div>
        <span id="aliasArrow" style="color:var(--text-3);transition:transform 0.2s">▸</span>
      </div>
      <div id="aliasSection" style="display:none;padding-top:16px"></div>
    </div>
  `;

  const usersGroupFilter = $('usersGroupFilter');
  if (usersGroupFilter) {
    usersGroupFilter.addEventListener('change', (e) => {
      activeGroup = e.target.value;
      renderCards();
    });
  }
  
  const refreshAll = $('refreshAll');
  if (refreshAll) {
    refreshAll.addEventListener('click', loadAll);
  }
  
  const aliasToggle = $('aliasToggle');
  if (aliasToggle) {
    aliasToggle.addEventListener('click', toggleAliasSection);
  }

  await loadAll();
}

function toggleAliasSection() {
  const section = $('aliasSection');
  const arrow = $('aliasArrow');
  if (!section || !arrow) return;
  const isOpen = section.style.display !== 'none';
  section.style.display = isOpen ? 'none' : 'block';
  arrow.style.transform = isOpen ? '' : 'rotate(90deg)';
}

async function loadAll() {
  const name = store.currentPersona;
  try {
    const [uData, bData] = await Promise.all([
      get(`/personas/${name}/users`),
      get(`/personas/${name}/biography`),
    ]);
    usersData = uData;
    bioData = bData;

    // 处理别名数据
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

    renderGroups();
    renderUnifiedStats();
    renderCards();
    renderAliases();
  } catch (e) {
    toast('加载数据失败: ' + e.message, 'error');
  }
}

function renderGroups() {
  const groups = usersData.groups || [];
  const sel = $('usersGroupFilter');
  if (!sel) return;
  sel.innerHTML = `<option value="">全部群组</option>` +
    groups.map(g => `<option value="${g}"${g === activeGroup ? ' selected' : ''}>${g}</option>`).join('');
}

function renderUnifiedStats() {
  const users = usersData.users || [];
  const groups = usersData.groups || [];
  const cards = bioData.cards || [];
  const totalDistilled = cards.reduce((sum, c) => sum + (c.distilled_points || []).length, 0);
  const aliasCount = (bioData.aliases || []).length;
  const lastUpdate = cards.length
    ? cards.reduce((max, c) => {
        const t = c.last_updated || '';
        return t > max ? t : max;
      }, '')
    : '';

  const unifiedStats = $('unifiedStats');
  if (!unifiedStats) return;
  
  unifiedStats.innerHTML = `
    <div class="stat-card">
      <div class="stat-label">用户总数</div>
      <div class="stat-value">${users.length}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">群组数量</div>
      <div class="stat-value">${groups.length}</div>
    </div>
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

function hashColor(str) {
  let hash = 0;
  for (let i = 0; i < str.length; i++) {
    hash = str.charCodeAt(i) + ((hash << 5) - hash);
  }
  const h = Math.abs(hash) % 360;
  return `hsl(${h}, 55%, 55%)`;
}

function formatDate(iso) {
  if (!iso) return '—';
  try {
    return new Date(iso).toLocaleDateString('zh-CN');
  } catch {
    return '—';
  }
}

function engagementColor(rate) {
  const pct = Math.round(rate * 100);
  if (pct >= 70) return 'var(--success)';
  if (pct >= 40) return 'var(--accent)';
  return 'var(--danger)';
}

function calcFamiliarity(count) {
  return Math.log(count + 1) / Math.log(51);
}

function getBioForUser(userId) {
  const cards = bioData.cards || [];
  return cards.find(c => c.user_id === userId) || null;
}

function renderCards() {
  const users = usersData.users || [];
  const filteredUsers = activeGroup
    ? users.filter(u => (u.groups || []).includes(activeGroup))
    : users;
  const grid = $('usersGrid');
  if (!grid) return;

  if (!filteredUsers.length) {
    grid.innerHTML = `
      <div class="card" style="grid-column:1/-1">
        <div style="color:var(--text-3);padding:40px;text-align:center">暂无用户数据</div>
      </div>
    `;
    return;
  }

  grid.innerHTML = filteredUsers.map(u => {
    const pct = Math.round((u.engagement_rate || 0) * 100);
    const color = engagementColor(u.engagement_rate || 0);
    const fam = calcFamiliarity(u.interaction_count || 0);
    const famPct = Math.min(100, Math.round(fam * 100));
    const avatarLetter = (u.name || u.user_id || '?')[0];
    const avatarBg = hashColor(u.user_id || 'x');

    const bio = getBioForUser(u.user_id);
    const anchors = bio ? (bio.identity_anchors || []).slice(0, 3) : [];
    const shortBio = bio ? (bio.short_bio || '').slice(0, 60) : '';
    const relsCount = bio ? (bio.relationships || []).length : 0;
    const distilledCount = bio ? (bio.distilled_points || []).length : 0;

    return `
      <div class="card" style="cursor:pointer" data-user-id="${u.user_id}">
        <div style="display:flex;align-items:center;gap:14px;margin-bottom:16px">
          <div style="width:48px;height:48px;border-radius:50%;background:${avatarBg};display:flex;align-items:center;justify-content:center;font-size:20px;font-weight:700;color:#fff;flex-shrink:0">${avatarLetter}</div>
          <div style="min-width:0;flex:1">
            <div style="display:flex;align-items:center;gap:8px">
              <span style="font-weight:600;font-size:15px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0">${u.name || '未知'}</span>
              ${distilledCount > 0 ? `<span class="tag" style="font-size:10px;padding:2px 6px;flex-shrink:0">${distilledCount} 要点</span>` : ''}
            </div>
            <div style="font-size:12px;color:var(--text-3);font-family:var(--font-mono)">${u.user_id || '—'}</div>
          </div>
        </div>
        ${anchors.length ? `<div style="display:flex;gap:4px;flex-wrap:wrap;margin-bottom:12px">${anchors.map(a => `<span class="tag" style="font-size:10px;padding:2px 6px">${a}</span>`).join('')}</div>` : ''}
        ${shortBio ? `<div style="font-size:12px;color:var(--text-2);margin-bottom:12px;line-height:1.4">${shortBio}${(bio.short_bio || '').length > 60 ? '...' : ''}</div>` : ''}
        <div style="display:flex;gap:16px;font-size:12px;color:var(--text-2);margin-bottom:14px">
          <div>交互 <strong>${(u.interaction_count || 0).toLocaleString()}</strong> 次</div>
          <div>最近 ${formatDate(u.last_interaction_at)}</div>
          ${relsCount > 0 ? `<div>关系 ${relsCount}</div>` : ''}
        </div>
        <div style="margin-bottom:12px">
          <div style="display:flex;justify-content:space-between;font-size:12px;margin-bottom:4px">
            <span style="color:var(--text-3)">互动率</span>
            <span style="color:${color};font-weight:600">${pct}%</span>
          </div>
          <div style="height:6px;border-radius:3px;background:var(--border);overflow:hidden">
            <div style="height:100%;width:${pct}%;background:${color};border-radius:3px;transition:width 0.4s"></div>
          </div>
        </div>
        <div>
          <div style="display:flex;justify-content:space-between;font-size:12px;margin-bottom:4px">
            <span style="color:var(--text-3)">熟悉度</span>
            <span style="color:var(--text-2);font-weight:600">${famPct}%</span>
          </div>
          <div style="height:6px;border-radius:3px;background:var(--border);overflow:hidden">
            <div style="height:100%;width:${famPct}%;background:var(--accent);border-radius:3px;transition:width 0.4s"></div>
          </div>
        </div>
      </div>
    `;
  }).join('');

  grid.querySelectorAll('[data-user-id]').forEach(card => {
    card.addEventListener('click', () => openDetailModal(card.dataset.userId));
  });
}

function openDetailModal(userId) {
  const users = usersData.users || [];
  const user = users.find(u => u.user_id === userId);
  const bio = getBioForUser(userId);

  if (!user && !bio) return;
  closeModal();

  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay';
  overlay.innerHTML = `
    <div class="modal" style="max-width:700px;max-height:85vh;overflow-y:auto">
      <div class="modal-header">
        <span style="font-size:16px;font-weight:600">${user?.name || bio?.name || userId || '详情'}</span>
        <button class="btn btn-sm" id="modalClose">✕</button>
      </div>
      <div class="modal-body" id="modalBody"></div>
    </div>
  `;
  document.body.appendChild(overlay);
  currentModal = overlay;
  overlay.addEventListener('click', (e) => { if (e.target === overlay) closeModal(); });
  const modalClose = $('modalClose');
  if (modalClose) {
    modalClose.addEventListener('click', closeModal);
  }

  const modalBody = $('modalBody');
  if (!modalBody) return;

  // 交互统计区
  if (user) {
    const pct = Math.round((user.engagement_rate || 0) * 100);
    const color = engagementColor(user.engagement_rate || 0);
    const fam = calcFamiliarity(user.interaction_count || 0);
    const famPct = Math.min(100, Math.round(fam * 100));

    modalBody.innerHTML += `
      <div style="margin-bottom:20px">
        <div style="font-size:14px;font-weight:600;margin-bottom:12px">交互统计</div>
        <div style="display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;margin-bottom:16px">
          <div style="padding:12px;background:var(--surface-2,rgba(255,255,255,0.03));border-radius:8px">
            <div style="font-size:12px;color:var(--text-3)">交互次数</div>
            <div style="font-size:20px;font-weight:600">${(user.interaction_count || 0).toLocaleString()}</div>
          </div>
          <div style="padding:12px;background:var(--surface-2,rgba(255,255,255,0.03));border-radius:8px">
            <div style="font-size:12px;color:var(--text-3)">最近交互</div>
            <div style="font-size:14px">${formatDate(user.last_interaction_at)}</div>
          </div>
          <div style="padding:12px;background:var(--surface-2,rgba(255,255,255,0.03));border-radius:8px">
            <div style="font-size:12px;color:var(--text-3)">互动率</div>
            <div style="font-size:20px;font-weight:600;color:${color}">${pct}%</div>
          </div>
          <div style="padding:12px;background:var(--surface-2,rgba(255,255,255,0.03));border-radius:8px">
            <div style="font-size:12px;color:var(--text-3)">熟悉度</div>
            <div style="font-size:20px;font-weight:600">${famPct}%</div>
          </div>
        </div>
        ${user.groups?.length ? `<div style="font-size:12px;color:var(--text-3)">所属群组: ${user.groups.join(', ')}</div>` : ''}
      </div>
    `;
  }

  // 传记详情区
  if (bio) {
    const anchors = bio.identity_anchors || [];
    const rels = bio.relationships || [];
    const distilled = bio.distilled_points || [];
    const pending = bio.pending_messages || [];

    modalBody.innerHTML += `
      <div style="border-top:1px solid var(--border);padding-top:20px">
        <div style="font-size:14px;font-weight:600;margin-bottom:12px">人物传记</div>
        <div style="display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:16px">
          <div>
            <div style="font-size:12px;color:var(--text-3)">User ID</div>
            <div style="font-size:14px">${bio.user_id || '—'}</div>
          </div>
          <div>
            <div style="font-size:12px;color:var(--text-3)">别名</div>
            <div style="font-size:14px">${(bio.aliases || []).join(', ') || '—'}</div>
          </div>
          <div>
            <div style="font-size:12px;color:var(--text-3)">最近更新</div>
            <div style="font-size:14px">${bio.last_updated ? new Date(bio.last_updated).toLocaleString('zh-CN') : '—'}</div>
          </div>
          <div>
            <div style="font-size:12px;color:var(--text-3)">最近提炼</div>
            <div style="font-size:14px">${bio.last_distilled ? new Date(bio.last_distilled).toLocaleString('zh-CN') : '—'}</div>
          </div>
        </div>

        ${anchors.length ? `
          <div style="margin-bottom:16px">
            <div style="font-size:13px;font-weight:600;margin-bottom:8px">身份锚点</div>
            <div style="display:flex;gap:6px;flex-wrap:wrap">${anchors.map(a => `<span class="tag">${a}</span>`).join('')}</div>
          </div>
        ` : ''}

        ${bio.short_bio ? `
          <div style="margin-bottom:16px">
            <div style="font-size:13px;font-weight:600;margin-bottom:8px">简要传记</div>
            <div style="font-size:13px;color:var(--text-2);line-height:1.6;white-space:pre-wrap">${bio.short_bio}</div>
          </div>
        ` : ''}

        ${rels.length ? `
          <div style="margin-bottom:16px">
            <div style="font-size:13px;font-weight:600;margin-bottom:8px">关系列表</div>
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
            <div style="font-size:13px;font-weight:600;margin-bottom:8px">提炼要点 (${distilled.length})</div>
            <div style="display:grid;gap:6px">${distilled.map(dp => `
              <div style="padding:8px 12px;background:var(--surface-2,rgba(255,255,255,0.03));border-radius:6px;font-size:13px;color:var(--text-2)">${typeof dp === 'string' ? dp : dp.point || JSON.stringify(dp)}</div>
            `).join('')}</div>
          </div>
        ` : ''}

        ${pending.length ? `
          <div>
            <div style="font-size:13px;font-weight:600;margin-bottom:8px">待处理消息 (${pending.length})</div>
            <div style="max-height:200px;overflow-y:auto;display:grid;gap:6px">${pending.map(pm => `
              <div style="padding:8px 12px;background:var(--surface-2,rgba(255,255,255,0.03));border-radius:6px;font-size:12px;color:var(--text-2)">${typeof pm === 'string' ? pm : pm.content || JSON.stringify(pm)}</div>
            `).join('')}</div>
          </div>
        ` : ''}
      </div>
    `;
  }
}

function renderAliases() {
  const aliases = bioData.aliases || [];
  const el = $('aliasSection');
  if (!el) return;

  // 构建用户 ID -> 名称的映射
  const users = usersData.users || [];
  const userNameMap = new Map();
  users.forEach(u => {
    if (u.user_id && u.name) {
      userNameMap.set(u.user_id, u.name);
    }
  });

  // 获取用户昵称
  const getUserName = (userId) => userNameMap.get(userId) || '';

  // 按别名归类
  const groupedByAlias = new Map();
  aliases.forEach(a => {
    const aliasName = a.alias || a.name || '';
    if (!groupedByAlias.has(aliasName)) {
      groupedByAlias.set(aliasName, []);
    }
    groupedByAlias.get(aliasName).push(a);
  });

  let contentHtml = '';
  if (aliases.length) {
    // 按别名归类显示
    const sortedAliases = Array.from(groupedByAlias.entries()).sort((a, b) => a[0].localeCompare(b[0]));
    contentHtml = `
      <div style="display:grid;gap:12px;margin-bottom:16px">
        ${sortedAliases.map(([aliasName, entries]) => `
          <div style="background:var(--surface-1,var(--bg-2));border:1px solid var(--border);border-radius:var(--radius-md);overflow:hidden">
            <div style="padding:10px 14px;background:var(--surface-2,var(--bg-3));display:flex;align-items:center;justify-content:space-between">
              <div style="display:flex;align-items:center;gap:10px">
                <span style="font-weight:600;font-size:14px;color:var(--text-1)">${aliasName}</span>
                <span class="tag" style="font-size:11px">${entries.length} 个用户</span>
              </div>
            </div>
            <div style="padding:8px">
              <table style="width:100%;border-collapse:collapse;font-size:13px;table-layout:fixed">
                <thead>
                  <tr style="border-bottom:1px solid var(--border)">
                    <th style="padding:6px 10px;text-align:left;color:var(--text-3);font-weight:500;width:30%">用户</th>
                    <th style="padding:6px 10px;text-align:left;color:var(--text-3);font-weight:500;width:30%">ID</th>
                    <th style="padding:6px 10px;text-align:left;color:var(--text-3);font-weight:500;width:12%">权重</th>
                    <th style="padding:6px 10px;text-align:left;color:var(--text-3);font-weight:500;width:18%">来源</th>
                    <th style="padding:6px 10px;text-align:right;color:var(--text-3);font-weight:500;width:10%">操作</th>
                  </tr>
                </thead>
                <tbody>
                  ${entries.map(a => {
                    const userName = getUserName(a.user_id);
                    return `
                      <tr style="border-bottom:1px solid var(--border)">
                        <td style="padding:6px 10px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">
                          <div style="display:flex;align-items:center;gap:8px">
                            <div style="width:24px;height:24px;border-radius:50%;background:${hashColor(a.user_id || 'x')};display:flex;align-items:center;justify-content:center;font-size:10px;color:#fff;flex-shrink:0">${(userName || a.user_id || '?')[0]}</div>
                            <span style="overflow:hidden;text-overflow:ellipsis;color:${userName ? 'var(--text-1)' : 'var(--text-3)'}">${userName || '未知'}</span>
                          </div>
                        </td>
                        <td style="padding:6px 10px;color:var(--text-2);font-family:var(--font-mono);font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${a.user_id || ''}</td>
                        <td style="padding:6px 10px;color:var(--text-2)">${a.weight != null ? a.weight.toFixed(2) : '—'}</td>
                        <td style="padding:6px 10px;color:var(--text-2);overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${a.source || '—'}</td>
                        <td style="padding:6px 10px;text-align:right">
                          <button class="btn btn-sm btn-ghost delete-alias-btn" data-alias="${aliasName}" data-user-id="${a.user_id || ''}" style="color:var(--danger)">删除</button>
                        </td>
                      </tr>
                    `;
                  }).join('')}
                </tbody>
              </table>
            </div>
          </div>
        `).join('')}
      </div>
    `;
  } else {
    contentHtml = '<div style="padding:20px;text-align:center;color:var(--text-3);margin-bottom:16px">暂无别名</div>';
  }

  el.innerHTML = `
    ${contentHtml}
    <div style="display:flex;gap:12px;align-items:flex-end;flex-wrap:wrap;padding-top:12px;border-top:1px solid var(--border)">
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
    await loadAll();
  } catch (e) {
    toast('添加失败: ' + e.message, 'error');
  }
}

async function deleteAlias(alias, userId) {
  const name = store.currentPersona;
  try {
    await post(`/personas/${name}/biography/aliases`, { action: 'delete', alias, user_id: userId });
    toast('别名已删除');
    await loadAll();
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
