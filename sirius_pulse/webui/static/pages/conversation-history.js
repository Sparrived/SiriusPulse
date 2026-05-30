import { store } from '../store.js';
import { get } from '../app.js';
import { toast, $ } from '../components.js';

let messages = [];
let pinnedMessages = [];
let groups = [];
let activeGroup = '';
let currentOffset = 0;
const PAGE_SIZE = 100;

let pollTimer = null;
let isLive = true;

const TAG_COLORS = {
  sticker:    { bg: '#f5a62322', color: '#f5a623', border: '#f5a62344' },
  image:      { bg: '#4caf5022', color: '#4caf50', border: '#4caf5044' },
  emoji:      { bg: '#e8a87c22', color: '#e8a87c', border: '#e8a87c44' },
  voice:      { bg: '#85cdca22', color: '#85cdca', border: '#85cdca44' },
  video:      { bg: '#d8b4e222', color: '#d8b4e2', border: '#d8b4e244' },
  mention:    { bg: 'var(--accent)22', color: 'var(--accent)', border: 'var(--accent)44' },
  reply:      { bg: 'var(--text-2)22', color: 'var(--text-2)', border: 'var(--text-2)44' },
  skill:      { bg: '#ff980022', color: '#ff9800', border: '#ff980044' },
  pin:        { bg: '#e91e6322', color: '#e91e63', border: '#e91e6344' },
  unpin:      { bg: '#9e9e9e22', color: '#9e9e9e', border: '#9e9e9e44' },
  pinned:     { bg: '#e91e6322', color: '#e91e63', border: '#e91e6344' },
  file:       { bg: '#607d8b22', color: '#607d8b', border: '#607d8b44' },
  link:       { bg: '#2196f322', color: '#2196f3', border: '#2196f344' },
  recall:     { bg: '#9e9e9e22', color: '#9e9e9e', border: '#9e9e9e44' },
  forward:    { bg: '#9c27b022', color: '#9c27b0', border: '#9c27b044' },
  location:   { bg: '#79554822', color: '#795548', border: '#79554844' },
  contact:    { bg: '#00bcd422', color: '#00bcd4', border: '#00bcd444' },
  share:      { bg: '#ff572222', color: '#ff5722', border: '#ff572244' },
  sign:       { bg: '#8bc34a22', color: '#8bc34a', border: '#8bc34a44' },
  redpacket:  { bg: '#f4433622', color: '#f44336', border: '#f4433644' },
  gift:       { bg: '#ff980022', color: '#ff9800', border: '#ff980044' },
  poke:       { bg: '#9e9e9e22', color: '#9e9e9e', border: '#9e9e9e44' },
  shake:      { bg: '#9e9e9e22', color: '#9e9e9e', border: '#9e9e9e44' },
  diary:      { bg: '#e8a87c22', color: '#e8a87c', border: '#e8a87c44' },
  cross_group:{ bg: '#85cdca22', color: '#85cdca', border: '#85cdca44' },
  conversation:{ bg: '#d8b4e222', color: '#d8b4e2', border: '#d8b4e244' },
  skill_result:{ bg: '#ff980022', color: '#ff9800', border: '#ff980044' },
  glossary:   { bg: '#00bcd422', color: '#00bcd4', border: '#00bcd444' },
  biography:  { bg: '#79554822', color: '#795548', border: '#79554844' },
  memory:     { bg: '#607d8b22', color: '#607d8b', border: '#607d8b44' },
  scene:      { bg: '#9c27b022', color: '#9c27b0', border: '#9c27b044' },
  taboo:      { bg: '#f4433622', color: '#f44336', border: '#f4433644' },
  atmosphere: { bg: '#4caf5022', color: '#4caf50', border: '#4caf5044' },
  plugin:     { bg: '#2196f322', color: '#2196f3', border: '#2196f344' },
  topic:      { bg: '#ff572222', color: '#ff5722', border: '#ff572244' },
  reminder:   { bg: '#ff980022', color: '#ff9800', border: '#ff980044' },
};

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
          <button class="btn btn-sm" id="liveToggle" style="display:flex;align-items:center;gap:4px">
            <span id="liveDot" style="width:6px;height:6px;border-radius:50%;background:var(--success)"></span>
            <span id="liveLabel">实时</span>
          </button>
          <button class="btn btn-sm" id="refreshBtn">刷新</button>
        </div>
      </div>
      <div class="stat-grid" id="statsGrid"></div>
    </div>
    <div id="pinnedSection" style="display:none;margin-bottom:20px">
      <div class="card">
        <div class="card-header">
          <div class="card-title" style="display:flex;align-items:center;gap:8px">
            <span style="color:var(--accent)">📌</span> 钉住的消息
          </div>
          <div class="card-subtitle" id="pinnedCount"></div>
        </div>
        <div id="pinnedList" style="display:grid;gap:8px;padding:0 16px 16px"></div>
      </div>
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

  const liveToggleEl = $('liveToggle');
  if (liveToggleEl) {
    liveToggleEl.addEventListener('click', () => {
      isLive = !isLive;
      updateLiveIndicator();
      if (isLive) startPolling();
      else stopPolling();
    });
  }

  await loadMessages();
  updateLiveIndicator();
  startPolling();
}

async function loadMessages(silent = false) {
  const name = store.currentPersona;
  if (!name) return;

  const params = new URLSearchParams({
    limit: String(PAGE_SIZE),
    offset: String(currentOffset),
  });
  if (activeGroup) params.set('group_id', activeGroup);

  try {
    const data = await get(`/personas/${name}/conversations?${params}`);
    messages = data.messages || [];
    pinnedMessages = data.pinned_messages || [];
    groups = data.groups || [];
    const total = data.total || 0;

    updateGroupFilter();
    renderStats(total);
    renderPinnedMessages();
    renderMessages();
    renderPagination(total);
  } catch (e) {
    if (!silent) {
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

function renderPinnedMessages() {
  const section = $('pinnedSection');
  const list = $('pinnedList');
  const countEl = $('pinnedCount');

  if (!section || !list) return;

  if (!pinnedMessages.length) {
    section.style.display = 'none';
    return;
  }

  section.style.display = 'block';
  if (countEl) {
    countEl.textContent = `共 ${pinnedMessages.length} 条钉住消息`;
  }

  list.innerHTML = pinnedMessages.map(msg => {
    const carryProgress = msg.max_carry_count > 0
      ? Math.round((msg.current_carry_count / msg.max_carry_count) * 100)
      : 0;

    return `
      <div style="padding:12px 16px;background:var(--bg-2);border-radius:8px;border-left:3px solid var(--accent)">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
          <div style="display:flex;align-items:center;gap:8px">
            <span style="color:var(--accent);font-weight:600;font-size:13px">${escapeHtml(msg.speaker || '未知')}</span>
            ${msg.group_id ? `<span class="tag" style="font-size:10px;padding:2px 6px">${escapeHtml(msg.group_id)}</span>` : ''}
            ${msg.reason ? `<span class="tag" style="font-size:10px;padding:2px 6px;background:var(--warning)22;color:var(--warning)">${escapeHtml(msg.reason)}</span>` : ''}
          </div>
          <span style="font-size:11px;color:var(--text-3)">${formatTime(msg.pinned_at)}</span>
        </div>
        <div style="font-size:13px;color:var(--text-1);line-height:1.6;white-space:pre-wrap;margin-bottom:8px">${escapeHtml(truncate(msg.content))}</div>
        <div style="display:flex;align-items:center;gap:8px;font-size:11px;color:var(--text-3)">
          <span>携带次数: ${msg.current_carry_count} / ${msg.max_carry_count}</span>
          <div style="flex:1;height:4px;background:var(--bg-3);border-radius:2px;overflow:hidden">
            <div style="width:${carryProgress}%;height:100%;background:var(--accent);border-radius:2px;transition:width 0.3s"></div>
          </div>
          <span>${carryProgress}%</span>
        </div>
      </div>
    `;
  }).join('');
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

const SECTION_COLORS = {
  '角色': '#e91e63', '身份锚定': '#9c27b0', '背景故事': '#673ab7',
  '人格底色': '#3f51b5', '情绪反应': '#2196f3', '关系模式': '#00bcd4',
  '说话方式': '#009688', '回应习惯': '#4caf50', '场景行为': '#8bc34a',
  '场景定位': '#cddc39', '身份识别': '#ffc107', '输出规范': '#ff9800',
  '发言者情绪': '#ff5722', '互动指导': '#795548', '相关记忆': '#607d8b',
  '群体风格': '#e8a87c', '回复风格': '#85cdca', '跨群认知': '#d8b4e2',
  '人物速查': '#795548', '我的能力': '#00bcd4', '群成员区分': '#9e9e9e',
  '当前场景': '#9c27b0', '首次互动': '#ff5722', '触发原因': '#f44336',
  '语气': '#e91e63', '提醒': '#ff9800', '话题建议': '#4caf50',
  '话题': '#2196f3', '长度要求': '#607d8b', '群体兴趣': '#8bc34a',
  '关系': '#00bcd4', '历史日记': '#e8a87c', '其他群近期记录': '#85cdca',
  '近期对话记录': '#d8b4e2', '技能执行结果': '#ff9800', '当前时间': '#9e9e9e',
  '群规禁忌': '#f44336', '氛围趋势': '#4caf50', '插件能力': '#2196f3',
  '名词解释': '#00bcd4', '钉住的重要消息': '#e91e63', '最近消息': '#607d8b',
};

const SECTION_ENDINGS = ['结束', '完毕', '完', '末', '尾', '终止', '关闭'];

function parsePromptSections(prompt) {
  if (!prompt) return [];
  const sections = [];
  const regex = /【([^】]+)】/g;
  let match;
  let lastIndex = 0;
  let currentSection = null;

  while ((match = regex.exec(prompt)) !== null) {
    const tagContent = match[1];
    const tagStart = match.index;
    const tagEnd = regex.lastIndex;

    // 检查是否是结束标签
    const isEnd = SECTION_ENDINGS.some(e => tagContent.endsWith(e));

    if (isEnd && currentSection) {
      // 结束当前 section
      const contentBefore = prompt.slice(currentSection.contentStart, tagStart).trim();
      sections.push({
        type: 'section',
        label: currentSection.label,
        color: currentSection.color,
        content: contentBefore,
      });
      currentSection = null;
      lastIndex = tagEnd;
    } else if (!isEnd) {
      // 保存之前的文本
      if (tagStart > lastIndex) {
        const text = prompt.slice(lastIndex, tagStart).trim();
        if (text) sections.push({ type: 'text', content: text });
      }

      // 查找匹配的颜色
      let color = '#9e9e9e';
      for (const [key, c] of Object.entries(SECTION_COLORS)) {
        if (tagContent.includes(key)) { color = c; break; }
      }

      // 检查是否有对应结束标签
      const endPattern = new RegExp(`【${tagContent}(结束|完毕|完|末|尾|终止|关闭)】`);
      const hasEnd = endPattern.test(prompt.slice(tagEnd));

      if (hasEnd) {
        currentSection = { label: tagContent, color, contentStart: tagEnd };
      } else {
        // 单个标签，作为独立 section
        sections.push({
          type: 'section',
          label: tagContent,
          color,
          content: '',
        });
      }
      lastIndex = tagEnd;
    }
  }

  // 处理剩余文本
  if (lastIndex < prompt.length) {
    const remaining = prompt.slice(lastIndex).trim();
    if (remaining) {
      if (currentSection) {
        sections.push({
          type: 'section',
          label: currentSection.label,
          color: currentSection.color,
          content: remaining,
        });
      } else {
        sections.push({ type: 'text', content: remaining });
      }
    }
  }

  return sections;
}

function estimateTokens(text) {
  if (!text) return 0;
  return Math.ceil(text.length / 2);
}

let msgIdCounter = 0;

function renderMessageTags(tags) {
  if (!tags || !tags.length) return '';
  const badges = tags.map(t => {
    const c = TAG_COLORS[t.type] || TAG_COLORS.reply;
    return `<span class="tag" style="font-size:10px;padding:1px 6px;background:${c.bg};color:${c.color};border:1px solid ${c.border}">${escapeHtml(t.label)}</span>`;
  }).join('');
  return `<div style="display:flex;flex-wrap:wrap;gap:4px;margin-top:6px">${badges}</div>`;
}

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
    const tags = m.tags || [];

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
        ${renderMessageTags(tags)}
        ${hasPrompt ? renderPromptToggle(msgId, systemPrompt) : ''}
      </div>
    `;
  }).join('');

  bindPromptToggles();
}

function renderPromptToggle(msgId, systemPrompt) {
  const tokenCount = estimateTokens(systemPrompt);
  const charCount = systemPrompt.length;
  const sections = parsePromptSections(systemPrompt);
  const hasSections = sections.length > 1 || (sections.length === 1 && sections[0].type === 'section');

  const sectionBadges = sections
    .filter(s => s.type === 'section')
    .map(s => `<span class="tag" style="font-size:10px;padding:2px 6px;background:${s.color}22;color:${s.color};border:1px solid ${s.color}44">${s.label}</span>`)
    .join(' ');

  return `
    <div style="margin-top:10px">
      <button class="btn btn-sm prompt-toggle" data-target="${msgId}" style="font-size:11px;padding:4px 10px;display:flex;align-items:center;gap:6px">
        <span class="toggle-arrow" style="display:inline-block;transition:transform 0.2s">▸</span>
        <span>查看 LLM 输入上下文</span>
        <span style="color:var(--text-3);font-size:10px">${tokenCount} tokens · ${charCount} chars</span>
      </button>
      <div style="margin-top:6px;display:flex;flex-wrap:wrap;gap:4px">${sectionBadges}</div>
      <div id="${msgId}" class="prompt-detail" style="display:none;margin-top:8px;border:1px solid var(--border);border-radius:6px;overflow:hidden">
        ${hasSections ? renderStructuredSections(sections) : renderRawPrompt(systemPrompt)}
      </div>
    </div>
  `;
}

function renderStructuredSections(sections) {
  return sections.map((section, idx) => {
    if (section.type === 'section') {
      const sectionId = `section-${msgIdCounter}-${idx}`;
      return `
        <div style="border-bottom:1px solid var(--border)">
          <div class="section-header" data-target="${sectionId}"
               style="padding:8px 12px;cursor:pointer;display:flex;align-items:center;gap:8px;background:${section.color}08">
            <span class="section-arrow" style="display:inline-block;transition:transform 0.2s;font-size:11px;color:var(--text-3)">▸</span>
            <span style="font-size:12px;font-weight:600;color:${section.color}">${section.label}</span>
            <span style="font-size:10px;color:var(--text-3);margin-left:auto">${section.content.length} chars</span>
          </div>
          <div id="${sectionId}" class="section-body" style="display:none;padding:10px 12px;background:var(--bg-1);font-size:11px;color:var(--text-2);line-height:1.5;white-space:pre-wrap;max-height:250px;overflow-y:auto;font-family:monospace">${escapeHtml(section.content)}</div>
        </div>
      `;
    }
    return `
      <div style="padding:10px 12px;background:var(--bg-1);font-size:11px;color:var(--text-2);line-height:1.5;white-space:pre-wrap;max-height:200px;overflow-y:auto;font-family:monospace;border-bottom:1px solid var(--border)">${escapeHtml(section.content)}</div>
    `;
  }).join('');
}

function renderRawPrompt(systemPrompt) {
  return `<div style="padding:10px 12px;background:var(--bg-1);font-size:11px;color:var(--text-2);line-height:1.5;white-space:pre-wrap;max-height:400px;overflow-y:auto;font-family:monospace">${escapeHtml(systemPrompt)}</div>`;
}

function bindPromptToggles() {
  document.querySelectorAll('.prompt-toggle').forEach(btn => {
    btn.addEventListener('click', () => {
      const target = document.getElementById(btn.dataset.target);
      if (!target) return;
      const isOpen = target.style.display !== 'none';
      target.style.display = isOpen ? 'none' : 'block';
      const arrow = btn.querySelector('.toggle-arrow');
      if (arrow) arrow.style.transform = isOpen ? '' : 'rotate(90deg)';
    });
  });

  document.querySelectorAll('.section-header').forEach(header => {
    header.addEventListener('click', () => {
      const target = document.getElementById(header.dataset.target);
      if (!target) return;
      const isOpen = target.style.display !== 'none';
      target.style.display = isOpen ? 'none' : 'block';
      const arrow = header.querySelector('.section-arrow');
      if (arrow) arrow.style.transform = isOpen ? '' : 'rotate(90deg)';
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

function startPolling() {
  stopPolling();
  if (!isLive) return;
  pollTimer = setInterval(() => {
    if (currentOffset === 0) loadMessages(true);
  }, 5000);
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

function updateLiveIndicator() {
  const dot = $('liveDot');
  const label = $('liveLabel');
  if (dot) dot.style.background = isLive ? 'var(--success)' : 'var(--text-3)';
  if (label) label.textContent = isLive ? '实时' : '已暂停';
}
