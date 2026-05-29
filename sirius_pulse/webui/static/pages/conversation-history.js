import { store } from '../store.js';
import { get } from '../app.js';
import { toast, $ } from '../components.js';

let messages = [];
let groups = [];
let activeGroup = '';
let currentOffset = 0;
const PAGE_SIZE = 100;

export async function init(container) {
  const name = store.currentPersona;
  if (!name) {
    container.innerHTML = `
      <div class="card">
        <div style="padding:60px;text-align:center;color:var(--text-3)">
          <div style="font-size:48px;margin-bottom:16px">◧</div>
          <div style="font-size:16px;margin-bottom:8px">请先选择人格</div>
        </div>
      </div>
    `;
    return;
  }

  container.innerHTML = `
    <div class="card" style="margin-bottom:20px">
      <div class="card-header">
        <div>
          <div class="card-title">历史对话分析</div>
          <div class="card-subtitle">查看 ${name} 的完整对话记录</div>
        </div>
        <div style="display:flex;gap:12px;align-items:center">
          <select id="groupFilter" class="btn btn-sm">
            <option value="">全部群组</option>
          </select>
          <button class="btn btn-sm" id="refreshBtn">刷新</button>
        </div>
      </div>
      <div class="stat-grid" id="statsGrid"></div>
    </div>
    <div id="messageList" style="display:grid;gap:8px">
      <div class="card">
        <div style="padding:40px;text-align:center;color:var(--text-3)">加载中...</div>
      </div>
    </div>
    <div id="pagination" style="display:flex;justify-content:center;gap:12px;margin-top:20px"></div>
  `;

  const groupFilterEl = $('groupFilter');
  if (groupFilterEl) {
    groupFilterEl.addEventListener('change', (e) => {
      activeGroup = e.target.value;
      currentOffset = 0;
      loadMessages();
    });
  }
  const refreshBtnEl = $('refreshBtn');
  if (refreshBtnEl) {
    refreshBtnEl.addEventListener('click', () => {
      currentOffset = 0;
      loadMessages();
    });
  }

  await loadMessages();
}

async function loadMessages() {
  const name = store.currentPersona;
  const params = new URLSearchParams({
    limit: String(PAGE_SIZE),
    offset: String(currentOffset),
  });
  if (activeGroup) params.set('group_id', activeGroup);

  try {
    const data = await get(`/personas/${name}/conversations?${params}`);
    messages = data.messages || [];
    groups = data.groups || [];
    const total = data.total || 0;

    updateGroupFilter();
    renderStats(total);
    renderMessages();
    renderPagination(total);
  } catch (e) {
    toast('加载对话历史失败: ' + e.message, 'error');
    const msgList = $('messageList');
    if (msgList) {
      msgList.innerHTML = `
        <div class="card">
          <div style="padding:40px;text-align:center;color:var(--danger)">加载失败: ${e.message}</div>
        </div>
      `;
    }
  }
}

function updateGroupFilter() {
  const sel = $('groupFilter');
  if (!sel) return;
  const current = sel.value;
  sel.innerHTML = `<option value="">全部群组</option>` +
    groups.map(g => `<option value="${g}"${g === current ? ' selected' : ''}>${g}</option>`).join('');
}

function renderStats(total) {
  const humanCount = messages.filter(m => m.role === 'human').length;
  const assistantCount = messages.filter(m => m.role === 'assistant').length;
  const systemCount = messages.filter(m => m.role === 'system').length;
  const uniqueUsers = new Set(messages.filter(m => m.user_id).map(m => m.user_id)).size;

  const statsGrid = $('statsGrid');
  if (!statsGrid) return;
  statsGrid.innerHTML = `
    <div class="stat-card">
      <div class="stat-label">总消息数</div>
      <div class="stat-value">${total.toLocaleString()}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">用户消息</div>
      <div class="stat-value">${humanCount}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">AI 回复</div>
      <div class="stat-value">${assistantCount}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">系统消息</div>
      <div class="stat-value">${systemCount}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">参与用户</div>
      <div class="stat-value">${uniqueUsers}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">群组数</div>
      <div class="stat-value">${groups.length}</div>
    </div>
  `;
}

function formatTime(ts) {
  if (!ts) return '';
  try {
    const date = new Date(ts);
    return date.toLocaleString('zh-CN');
  } catch {
    return ts;
  }
}

function getRoleStyle(role) {
  switch (role) {
    case 'human':
      return { color: 'var(--accent)', label: '用户' };
    case 'assistant':
      return { color: 'var(--success)', label: 'AI' };
    case 'system':
      return { color: 'var(--text-3)', label: '系统' };
    default:
      return { color: 'var(--text-2)', label: role };
  }
}

function escapeHtml(str) {
  if (!str) return '';
  const div = document.createElement('div');
  div.textContent = str;
  return div.innerHTML;
}

function truncate(str, max = 500) {
  if (!str) return '';
  return str.length > max ? str.slice(0, max) + '...' : str;
}

let msgIdCounter = 0;

function renderMessages() {
  const el = $('messageList');
  if (!el) return;

  if (!messages.length) {
    el.innerHTML = `
      <div class="card">
        <div style="padding:60px;text-align:center;color:var(--text-3)">
          <div style="font-size:48px;margin-bottom:16px">◧</div>
          <div style="font-size:16px;margin-bottom:8px">暂无对话记录</div>
        </div>
      </div>
    `;
    return;
  }

  msgIdCounter = 0;
  el.innerHTML = messages.map(m => {
    const roleStyle = getRoleStyle(m.role);
    const speakerName = m.speaker_name || m.user_id || roleStyle.label;
    const content = m.content || '';
    const groupId = m.group_id || '';
    const systemPrompt = m.system_prompt || '';
    const hasPrompt = m.role === 'assistant' && systemPrompt;
    const msgId = `msg-${msgIdCounter++}`;

    return `
      <div style="padding:12px 16px;background:var(--bg-2);border-radius:8px;border-left:3px solid ${roleStyle.color}">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
          <div style="display:flex;align-items:center;gap:8px">
            <span style="color:${roleStyle.color};font-weight:600;font-size:13px">${escapeHtml(speakerName)}</span>
            <span class="tag" style="font-size:10px;padding:2px 6px">${roleStyle.label}</span>
            ${groupId ? `<span class="tag" style="font-size:10px;padding:2px 6px">${escapeHtml(groupId)}</span>` : ''}
          </div>
          <span style="font-size:11px;color:var(--text-3)">${formatTime(m.timestamp)}</span>
        </div>
        <div style="font-size:13px;color:var(--text-1);line-height:1.6;white-space:pre-wrap">${escapeHtml(truncate(content))}</div>
        ${hasPrompt ? `
          <div style="margin-top:10px">
            <button class="btn btn-sm prompt-toggle" data-target="${msgId}" style="font-size:11px;padding:4px 8px">▸ 查看系统提示词</button>
            <div id="${msgId}" style="display:none;margin-top:8px;padding:10px;background:var(--bg-1);border-radius:6px;font-size:12px;color:var(--text-2);line-height:1.5;white-space:pre-wrap;max-height:300px;overflow-y:auto">${escapeHtml(systemPrompt)}</div>
          </div>
        ` : ''}
      </div>
    `;
  }).join('');

  el.querySelectorAll('.prompt-toggle').forEach(btn => {
    btn.addEventListener('click', () => {
      const target = document.getElementById(btn.dataset.target);
      const isOpen = target.style.display !== 'none';
      target.style.display = isOpen ? 'none' : 'block';
      btn.textContent = isOpen ? '▸ 查看系统提示词' : '▾ 隐藏系统提示词';
    });
  });
}

function renderPagination(total) {
  const el = $('pagination');
  if (!el) return;
  const totalPages = Math.ceil(total / PAGE_SIZE);
  const currentPage = Math.floor(currentOffset / PAGE_SIZE) + 1;

  if (totalPages <= 1) {
    el.innerHTML = '';
    return;
  }

  el.innerHTML = `
    <button class="btn btn-sm" id="prevPage" ${currentOffset === 0 ? 'disabled' : ''}>上一页</button>
    <span style="display:flex;align-items:center;font-size:13px;color:var(--text-2)">第 ${currentPage} / ${totalPages} 页</span>
    <button class="btn btn-sm" id="nextPage" ${currentOffset + PAGE_SIZE >= total ? 'disabled' : ''}>下一页</button>
  `;

  const prevBtn = $('prevPage');
  if (prevBtn) {
    prevBtn.addEventListener('click', () => {
      if (currentOffset > 0) {
        currentOffset = Math.max(0, currentOffset - PAGE_SIZE);
        loadMessages();
      }
    });
  }

  const nextBtn = $('nextPage');
  if (nextBtn) {
    nextBtn.addEventListener('click', () => {
      if (currentOffset + PAGE_SIZE < total) {
        currentOffset += PAGE_SIZE;
        loadMessages();
      }
    });
  }
}
