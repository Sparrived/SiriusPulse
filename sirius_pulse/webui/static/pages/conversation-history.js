import { store } from '../store.js';
import { get, del } from '../app.js';
import { confirmDanger, toast } from '../components.js';
import { createRealtimeRefresh } from './realtime.js';
import { createScopedPage } from '../page-context.js';

const scopedPage = createScopedPage();
const $ = scopedPage.$;

let messages = [];
let pinnedMessages = [];
let groups = [];
let activeGroup = '';
let activeSearch = '';
let activeSpeaker = '';
let currentOffset = 0;
let compressedMessageLookup = new Set();
const PAGE_SIZE = 100;

let isLive = true;
let isLoadingMessages = false;
let needsReloadAfterLoad = false;
const realtime = createRealtimeRefresh(() => loadMessages(true), {
  resources: ['conversations'],
  debounceMs: 500,
  shouldRefresh: () => isLive && currentOffset === 0,
});

export function dispose() {
  scopedPage.use(null, null);
  realtime.stop();
}

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
  compressed: { bg: '#cddc3922', color: '#cddc39', border: '#cddc3944' },
  scene:      { bg: '#9c27b022', color: '#9c27b0', border: '#9c27b044' },
  atmosphere: { bg: '#4caf5022', color: '#4caf50', border: '#4caf5044' },
  plugin:     { bg: '#2196f322', color: '#2196f3', border: '#2196f344' },
  topic:      { bg: '#ff572222', color: '#ff5722', border: '#ff572244' },
  reminder:   { bg: '#ff980022', color: '#ff9800', border: '#ff980044' },
};

export async function init(container, params = {}) {
  scopedPage.use(params?.ctx, container);
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
        <div style="display:flex;gap:12px;align-items:center;flex-wrap:wrap">
          <input type="text" id="msgSearch" placeholder="搜索消息内容..." style="width:180px;background:var(--surface-2);border:1px solid var(--border);border-radius:6px;padding:6px 10px;font-size:13px;color:var(--text-1)">
          <input type="text" id="speakerFilter" placeholder="发言人..." style="width:120px;background:var(--surface-2);border:1px solid var(--border);border-radius:6px;padding:6px 10px;font-size:13px;color:var(--text-1)">
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

  const msgSearchEl = $('msgSearch');
  if (msgSearchEl) {
    let debounceTimer;
    msgSearchEl.addEventListener('input', (e) => {
      clearTimeout(debounceTimer);
      debounceTimer = scopedPage.timeout(() => {
        activeSearch = e.target.value.trim();
        currentOffset = 0;
        loadMessages();
      }, 400);
    });
  }
  const speakerFilterEl = $('speakerFilter');
  if (speakerFilterEl) {
    let debounceTimer;
    speakerFilterEl.addEventListener('input', (e) => {
      clearTimeout(debounceTimer);
      debounceTimer = scopedPage.timeout(() => {
        activeSpeaker = e.target.value.trim();
        currentOffset = 0;
        loadMessages();
      }, 400);
    });
  }
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
      if (isLive && currentOffset === 0) loadMessages(true);
    });
  }

  await loadMessages();
  updateLiveIndicator();
  realtime.start();
}

async function loadMessages(silent = false) {
  const name = store.currentPersona;
  if (!name) return;

  if (isLoadingMessages) {
    needsReloadAfterLoad = true;
    return;
  }
  isLoadingMessages = true;

  const params = new URLSearchParams({
    limit: String(PAGE_SIZE),
    offset: String(currentOffset),
  });
  if (activeGroup) params.set('group_id', activeGroup);
  if (activeSearch) params.set('search', activeSearch);
  if (activeSpeaker) params.set('speaker', activeSpeaker);

  try {
    const data = await get(`/persona/conversations?${params}`);
    messages = data.messages || [];
    pinnedMessages = data.pinned_messages || [];
    groups = data.groups || [];
    rebuildCompressedMessageLookup();
    const total = data.total || 0;

    updateGroupFilter();
    renderStats(total);
    renderPinnedMessages();
    renderMessages();
    renderPagination(total);
  } catch (e) {
    if (e?.name === 'AbortError') return;
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
  } finally {
    isLoadingMessages = false;
    if (needsReloadAfterLoad && scopedPage.isActive()) {
      needsReloadAfterLoad = false;
      await loadMessages(true);
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
  const compressedCount = messages.filter(m => m.memory_compressed).length;

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
    <div class="stat-card">
      <div class="stat-label">本页已压缩</div>
      <div class="stat-value">${compressedCount}</div>
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

function escapeAttr(str) {
  return escapeHtml(str).replace(/"/g, '&quot;');
}

/** 将 conversation_chain 中可能的多模态 content 数组规范化为字符串 */
function normalizeChainContent(content) {
  if (Array.isArray(content)) {
    return content
      .filter(part => part && part.type === 'text')
      .map(part => part.text || '')
      .join('');
  }
  return content || '';
}

function truncate(str, max = 500) {
  if (!str) return '';
  return str.length > max ? str.slice(0, max) + '...' : str;
}

function buildConversationChain(message) {
  if (!message || message.role !== 'assistant') return [];

  const chain = Array.isArray(message.conversation_chain)
    ? message.conversation_chain.filter(m => m && typeof m === 'object')
    : [];
  const systemPrompt = message.system_prompt || '';

  if (!systemPrompt) return chain;

  const hasSystem = chain.some(m => m.role === 'system' && m.content);
  if (hasSystem) return chain;

  return [{ role: 'system', content: systemPrompt }, ...chain];
}

const SECTION_COLORS = {
  '角色': '#e91e63', '身份锚定': '#9c27b0', '背景故事': '#673ab7',
  '场景定位': '#cddc39', '身份识别': '#ffc107', '回复规范': '#ff9800',
  '发言者情绪': '#ff5722', '相关记忆': '#607d8b',
  '群体风格': '#e8a87c', '回复风格': '#85cdca', '跨群认知': '#d8b4e2',
  '人物速查': '#795548', '我的能力': '#00bcd4', '群成员区分': '#9e9e9e',
  '首次互动': '#ff5722', '触发原因': '#f44336',
  '语气': '#e91e63', '提醒': '#ff9800', '话题建议': '#4caf50',
  '话题': '#2196f3', '群体兴趣': '#8bc34a',
  '关系': '#00bcd4', '历史日记': '#e8a87c', '其他群近期记录': '#85cdca',
  '近期对话记录': '#d8b4e2', '技能执行结果': '#ff9800', '当前时间': '#9e9e9e',
  '氛围趋势': '#4caf50', '插件能力': '#2196f3',
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
      // 如果有当前section，先结束它（没有结束标签的情况）
      if (currentSection) {
        const contentBefore = prompt.slice(currentSection.contentStart, tagStart).trim();
        sections.push({
          type: 'section',
          label: currentSection.label,
          color: currentSection.color,
          content: contentBefore,
        });
        currentSection = null;
      } else {
        // 保存之前的文本（如果没有当前section）
        if (tagStart > lastIndex) {
          const text = prompt.slice(lastIndex, tagStart).trim();
          if (text) sections.push({ type: 'text', content: text });
        }
      }

      // 查找匹配的颜色
      let color = '#9e9e9e';
      for (const [key, c] of Object.entries(SECTION_COLORS)) {
        if (tagContent.includes(key)) { color = c; break; }
      }

      // 检查是否有对应结束标签
      const endPattern = new RegExp(`【${escapeRegex(tagContent)}(结束|完毕|完|末|尾|终止|关闭)】`);
      const hasEnd = endPattern.test(prompt.slice(tagEnd));

      if (hasEnd) {
        currentSection = { label: tagContent, color, contentStart: tagEnd };
      } else {
        // 没有结束标签，作为独立section，内容为空
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

  // 后处理：合并相邻的空section和text
  const merged = [];
  for (let i = 0; i < sections.length; i++) {
    const s = sections[i];
    if (s.type === 'section' && !s.content && i + 1 < sections.length) {
      const next = sections[i + 1];
      if (next.type === 'text') {
        // 将text内容合并到section中
        merged.push({ ...s, content: next.content });
        i++; // 跳过下一个
        continue;
      }
    }
    merged.push(s);
  }

  return merged;
}

// 转义正则表达式特殊字符
function escapeRegex(str) {
  return str.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
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

function messageCompressionKeys(message) {
  const groupId = message.group_id || activeGroup || '';
  const keys = [];
  if (message.entry_id) keys.push(`entry:${groupId}:${message.entry_id}`);
  if (message.platform_message_id) keys.push(`msg:${groupId}:${message.platform_message_id}`);
  const content = String(message.content || '').trim();
  if (message.user_id && content) keys.push(`content:${groupId}:${message.user_id}:${content.slice(0, 240)}`);
  return keys;
}

function historyItemCompressionKeys(item, defaultGroupId = '') {
  const groupId = item.group || defaultGroupId || activeGroup || '';
  const keys = [];
  if (item.msgId) keys.push(`msg:${groupId}:${item.msgId}`);
  const content = String(item.content || '').trim();
  if (item.userId && content) keys.push(`content:${groupId}:${item.userId}:${content.slice(0, 240)}`);
  return keys;
}

function rebuildCompressedMessageLookup() {
  compressedMessageLookup = new Set();
  messages.forEach(message => {
    if (!message?.memory_compressed) return;
    messageCompressionKeys(message).forEach(key => compressedMessageLookup.add(key));
  });
}

function isHistoryItemCompressed(item, defaultGroupId = '') {
  return historyItemCompressionKeys(item, defaultGroupId)
    .some(key => compressedMessageLookup.has(key));
}

function renderMemoryCompressionTags(message) {
  if (!message?.memory_compressed) return '';
  const refs = Array.isArray(message.memory_refs) ? message.memory_refs : [];
  const unitCount = refs.filter(ref => ref.kind === 'memory_unit').length;
  const diaryCount = refs.filter(ref => ref.kind === 'diary').length;
  const pieces = [
    unitCount ? `${unitCount} 记忆单元` : '',
    diaryCount ? `${diaryCount} 日记` : '',
  ].filter(Boolean);
  const title = refs.map(ref => ref.summary).filter(Boolean).join('\n');
  const suffix = pieces.length ? ` · ${pieces.join(' · ')}` : '';
  const c = TAG_COLORS.compressed;
  return `
    <div style="display:flex;flex-wrap:wrap;gap:4px;margin-top:6px">
      <span class="tag" title="${escapeAttr(title)}" style="font-size:10px;padding:1px 6px;background:${c.bg};color:${c.color};border:1px solid ${c.border}">已压缩进记忆${suffix}</span>
    </div>
  `;
}

function renderInjectedToolTags(toolNames) {
  if (!Array.isArray(toolNames) || !toolNames.length) return '';
  const badges = toolNames
    .filter(name => typeof name === 'string' && name.trim())
    .map(name => `<span class="tag" style="font-size:10px;padding:1px 6px;background:#2196f322;color:#2196f3;border:1px solid #2196f344">tool: ${escapeHtml(name.trim())}</span>`)
    .join('');
  return badges ? `<div style="display:flex;flex-wrap:wrap;gap:4px;margin-top:6px">${badges}</div>` : '';
}

function renderIntentScores(intentScores) {
  if (!intentScores || typeof intentScores !== 'object') return '';

  const intentLabel = {
    help_seeking: '求助',
    emotional: '情感',
    social: '社交',
    silent: '静默',
  };
  const chips = [];
  const socialIntent = intentScores.social_intent || '';
  if (socialIntent) {
    chips.push(`意图 ${escapeHtml(intentLabel[socialIntent] || socialIntent)}`);
  }

  const scoreItems = [
    ['directed_score', '指向'],
    ['urgency_score', '紧急'],
    ['relevance_score', '相关'],
    ['sarcasm_score', '讽刺'],
    ['entitlement_score', '资格'],
    ['turn_gap_readiness', '间隙'],
  ];
  scoreItems.forEach(([key, label]) => {
    const value = Number(intentScores[key]);
    if (Number.isFinite(value)) {
      chips.push(`${label} ${value.toFixed(2)}`);
    }
  });

  if (!chips.length) return '';
  const rendered = chips
    .map(text => `<span class="tag" style="font-size:10px;padding:1px 6px;background:#22c55e14;color:#86efac;border:1px solid #22c55e33">${text}</span>`)
    .join('');
  return `<div style="display:flex;flex-wrap:wrap;gap:4px;margin-top:6px">${rendered}</div>`;
}

function renderMessages() {
  const el = $('messageList');
  if (!el) return;

  // 保存当前滚动位置和内容高度（用于补偿新消息插入导致的偏移）
  const oldScrollTop = el.scrollTop;
  const oldScrollHeight = el.scrollHeight;

  // 保存当前展开的chain-detail状态
  const openChainEntryIds = new Set();
  const chainScrollPositions = new Map();
  el.querySelectorAll('.chain-detail').forEach(detail => {
    const entryId = detail.getAttribute('data-entry-id');
    if (detail.style.display !== 'none') {
      if (entryId) openChainEntryIds.add(entryId);
    }
    if (entryId && detail.scrollTop > 0) {
      chainScrollPositions.set(entryId, detail.scrollTop);
    }
  });

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
  el.innerHTML = messages.map((m, idx) => {
    const roleStyle = getRoleStyle(m.role);
    const speakerName = m.speaker_name || m.user_id || roleStyle.label;
    const content = m.content || '';
    const groupId = m.group_id || '';
    const conversationChain = buildConversationChain(m);
    const hasChain = m.role === 'assistant' && conversationChain.length > 0;
    const chainMsgId = `chain-${msgIdCounter++}`;
    const entryId = m.entry_id || m.timestamp || `idx-${idx}`;
    const tags = m.tags || [];
    const injectedToolNames = m.injected_tool_names || [];

    return `
      <div style="padding:12px 16px;background:var(--bg-2);border-radius:8px;border-left:3px solid ${roleStyle.color}">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
          <div style="display:flex;align-items:center;gap:8px">
            <span style="color:${roleStyle.color};font-weight:600;font-size:13px">${escapeHtml(speakerName)}</span>
            <span class="tag" style="font-size:10px;padding:2px 6px">${roleStyle.label}</span>
            ${groupId ? `<span class="tag" style="font-size:10px;padding:2px 6px">${escapeHtml(groupId)}</span>` : ''}
          </div>
          <div style="display:flex;align-items:center;gap:8px">
            <span style="font-size:11px;color:var(--text-3)">${formatTime(m.timestamp)}</span>
            <button class="btn btn-sm btn-danger conversation-delete" data-delete-index="${idx}" style="font-size:11px;padding:3px 8px">删除</button>
          </div>
        </div>
        <div style="font-size:13px;color:var(--text-1);line-height:1.6;white-space:pre-wrap">${escapeHtml(truncate(content))}</div>
        ${renderMessageTags(tags)}
        ${renderMemoryCompressionTags(m)}
        ${m.role === 'assistant' ? renderInjectedToolTags(injectedToolNames) : ''}
        ${renderIntentScores(m.intent_scores)}
        ${hasChain ? renderConversationChainToggle(chainMsgId, conversationChain, entryId, idx, openChainEntryIds.has(entryId), m) : ''}
      </div>
    `;
  }).join('');

  bindChainToggles();
  bindDeleteButtons();

  // 恢复chain-detail内部滚动位置
  if (chainScrollPositions.size > 0) {
    el.querySelectorAll('.chain-detail').forEach(detail => {
      const entryId = detail.getAttribute('data-entry-id');
      if (entryId && chainScrollPositions.has(entryId)) {
        detail.scrollTop = chainScrollPositions.get(entryId);
      }
    });
  }

  // 恢复滚动位置：新消息插入顶部会导致内容下移，需补偿高度差
  const newScrollHeight = el.scrollHeight;
  const heightDiff = newScrollHeight - oldScrollHeight;
  el.scrollTop = oldScrollTop + heightDiff;
}

const CHAIN_ROLE_STYLES = {
  system:    { color: '#9e9e9e', label: 'SYSTEM', bg: '#9e9e9e08' },
  user:      { color: 'var(--accent)', label: 'USER', bg: 'var(--accent)08' },
  assistant: { color: 'var(--success)', label: 'ASSISTANT', bg: 'var(--success)08' },
};

function renderConversationChainToggle(chainMsgId, chain, entryId = '', messageIndex = 0, isOpen = false, parentMessage = null) {
  const msgCount = chain.length;
  const totalChars = chain.reduce((sum, m) => sum + (m.content || '').length, 0);
  const totalTokens = Math.ceil(totalChars / 2);

  const displayStyle = isOpen ? 'display:block' : 'display:none';
  const arrowTransform = isOpen ? 'transform:rotate(90deg)' : '';
  const chainHtml = isOpen
    ? renderChainMessages(chain, parentMessage)
    : '<div style="padding:12px;color:var(--text-3);font-size:12px">点击后加载消息链详情</div>';

  // 统计各角色消息数
  const roleCounts = {};
  chain.forEach(m => {
    const r = m.role || 'unknown';
    roleCounts[r] = (roleCounts[r] || 0) + 1;
  });
  const roleSummary = Object.entries(roleCounts)
    .map(([r, c]) => `${r}×${c}`)
    .join(' ');

  return `
    <div style="margin-top:10px">
      <button class="btn btn-sm chain-toggle" data-target="${chainMsgId}" data-chain-index="${messageIndex}" style="font-size:11px;padding:4px 10px;display:flex;align-items:center;gap:6px;background:var(--accent)11;border:1px solid var(--accent)33">
        <span class="toggle-arrow" style="display:inline-block;transition:transform 0.2s;${arrowTransform}">▸</span>
        <span>查看 LLM 消息链</span>
        <span style="color:var(--text-3);font-size:10px">${msgCount} 条消息 · ${totalTokens} tokens · ${roleSummary}</span>
      </button>
      <div id="${chainMsgId}" class="chain-detail" data-entry-id="${entryId}" data-loaded="${isOpen ? 'true' : 'false'}" style="${displayStyle};margin-top:8px;border:1px solid var(--border);border-radius:6px;max-height:600px;overflow-y:auto">
        ${chainHtml}
      </div>
    </div>
  `;
}

function renderChainMessages(chain, parentMessage = null) {
  return chain.map((msg, idx) => {
    const role = msg.role || 'unknown';
    const style = CHAIN_ROLE_STYLES[role] || CHAIN_ROLE_STYLES.system;
    const content = normalizeChainContent(msg.content);
    const isSystem = role === 'system';
    const isUser = role === 'user';

    // system 消息做结构化解析
    if (isSystem && content.length > 100) {
      return renderChainSystemMessage(content, style, idx);
    }

    // user 消息：解析 XML 格式的多条消息
    if (isUser) {
      return renderChainUserMessage(content, style, idx, parentMessage);
    }

    // assistant 消息：直接展示
    return `
      <div style="border-bottom:1px solid var(--border);background:${style.bg}">
        <div style="padding:6px 12px;display:flex;align-items:center;gap:6px;background:${style.color}11">
          <span style="font-size:11px;font-weight:600;color:${style.color}">#${idx + 1} ${style.label}</span>
          <span style="font-size:10px;color:var(--text-3);margin-left:auto">${estimateTokens(content)} tokens</span>
        </div>
        <div style="padding:8px 12px;font-size:11px;color:var(--text-2);line-height:1.5;white-space:pre-wrap;max-height:300px;overflow-y:auto;font-family:monospace">${escapeHtml(content)}</div>
      </div>
    `;
  }).join('');
}

const SPEAKER_COLORS = [
  '#e91e63', '#9c27b0', '#673ab7', '#3f51b5', '#2196f3',
  '#00bcd4', '#009688', '#4caf50', '#ff9800', '#ff5722',
  '#795548', '#607d8b',
];

function getSpeakerColor(speaker) {
  let hash = 0;
  for (let i = 0; i < speaker.length; i++) {
    hash = ((hash << 5) - hash) + speaker.charCodeAt(i);
    hash |= 0;
  }
  return SPEAKER_COLORS[Math.abs(hash) % SPEAKER_COLORS.length];
}

function parseXmlMessages(content) {
  const parser = new DOMParser();
  const doc = parser.parseFromString(content, 'text/xml');
  if (doc.querySelector('parsererror')) {
    return null;
  }
  const items = [];
  doc.querySelectorAll('message').forEach(el => {
    items.push({
      type: 'message',
      speaker: el.getAttribute('speaker') || '',
      userId: el.getAttribute('user_id') || '',
      time: el.getAttribute('time') || '',
      group: el.getAttribute('group') || '',
      msgId: el.getAttribute('msg_id') || '',
      content: el.textContent || '',
    });
  });
  doc.querySelectorAll('image').forEach(el => {
    items.push({
      type: 'image',
      speaker: el.getAttribute('speaker') || '',
      userId: el.getAttribute('user_id') || '',
      caption: el.getAttribute('caption') || '',
      sticker: el.getAttribute('type') === 'sticker',
      src: el.getAttribute('src') || '',
    });
  });
  return items;
}

function extractXmlBlock(content, tagName) {
  if (!content || !tagName) return null;
  const pattern = new RegExp(`<${escapeRegex(tagName)}(?:\\s[^>]*)?>[\\s\\S]*?<\\/${escapeRegex(tagName)}>`, 'i');
  const match = pattern.exec(content);
  if (!match) return null;
  const start = match.index;
  const end = start + match[0].length;
  return {
    block: match[0],
    rest: (content.slice(0, start) + content.slice(end)).trim(),
  };
}

function extractXmlBlocks(content, tagNames) {
  let rest = content || '';
  const blocks = [];
  while (rest) {
    let best = null;
    for (const tagName of tagNames) {
      const extracted = extractXmlBlock(rest, tagName);
      if (!extracted) continue;
      const index = rest.indexOf(extracted.block);
      if (!best || index < best.index) {
        best = { ...extracted, tagName, index };
      }
    }
    if (!best) break;
    blocks.push({ tagName: best.tagName, block: best.block });
    rest = best.rest;
  }
  return { blocks, rest };
}

function extractBracketBlock(content, tagName) {
  if (!content || !tagName) return null;
  const escaped = escapeRegex(tagName);
  const pattern = new RegExp(`【${escaped}】[\\s\\S]*?【${escaped}结束】`, 'i');
  const match = pattern.exec(content);
  if (!match) return null;
  const start = match.index;
  const end = start + match[0].length;
  return {
    block: match[0],
    rest: (content.slice(0, start) + content.slice(end)).trim(),
  };
}

function renderInjectedHistorySection(block, parentMessage = null, label = 'Injected conversation history') {
  const parsed = parseXmlMessages(block) || [];
  const messageCount = parsed.filter(item => item.type === 'message').length;
  const imageCount = parsed.filter(item => item.type === 'image').length;
  const defaultGroupId = parentMessage?.group_id || '';
  const compressedCount = parsed.filter(item => item.type === 'message' && isHistoryItemCompressed(item, defaultGroupId)).length;
  const summary = [
    `${messageCount} messages`,
    compressedCount ? `${compressedCount} compressed` : '',
    imageCount ? `${imageCount} images` : '',
    `${estimateTokens(block)} tokens`,
  ].filter(Boolean).join(' · ');

  const body = parsed.length ? parsed.map(item => {
    if (item.type === 'image') {
      return `
        <div style="padding:6px 12px;display:flex;align-items:center;gap:6px;border-bottom:1px solid var(--border)">
          <span style="font-size:10px;font-weight:600;color:${getSpeakerColor(item.speaker)}">${escapeHtml(item.speaker || 'unknown')}</span>
          <span style="font-size:10px;color:var(--text-3)">${item.sticker ? 'sticker' : 'image'} ${escapeHtml(item.caption || '')}</span>
        </div>
      `;
    }
    const compressed = isHistoryItemCompressed(item, defaultGroupId);
    const speakerColor = getSpeakerColor(item.speaker || 'unknown');
    return `
      <div style="padding:6px 12px;border-bottom:1px solid var(--border)">
        <div style="display:flex;align-items:center;gap:6px;margin-bottom:3px">
          <span style="font-size:10px;font-weight:600;color:${speakerColor}">${escapeHtml(item.speaker || 'unknown')}</span>
          ${item.userId ? `<span style="font-size:9px;color:var(--text-3)">${escapeHtml(item.userId)}</span>` : ''}
          ${compressed ? `<span class="tag" style="font-size:9px;padding:1px 5px;background:${TAG_COLORS.compressed.bg};color:${TAG_COLORS.compressed.color};border:1px solid ${TAG_COLORS.compressed.border}">已压缩</span>` : ''}
          ${item.time ? `<span style="font-size:9px;color:var(--text-3);margin-left:auto">${escapeHtml(item.time)}</span>` : ''}
        </div>
        <div style="font-size:11px;color:var(--text-2);line-height:1.5;white-space:pre-wrap">${escapeHtml(item.content)}</div>
      </div>
    `;
  }).join('') : `
    <div style="padding:8px 12px;font-size:11px;color:var(--text-2);line-height:1.5;white-space:pre-wrap;font-family:monospace">${escapeHtml(truncate(block, 1200))}</div>
  `;

  return `
    <div style="border-bottom:1px solid var(--border);background:var(--accent)06">
      <div style="padding:6px 12px;display:flex;align-items:center;gap:6px;background:var(--accent)12">
        <span style="font-size:11px;font-weight:600;color:var(--accent)">${escapeHtml(label)}</span>
        <span style="font-size:10px;color:var(--text-3);margin-left:auto">${summary}</span>
      </div>
      <div style="max-height:260px;overflow-y:auto">${body}</div>
    </div>
  `;
}

function renderChainUserMessage(content, style, idx, parentMessage = null) {
  const extractedHistory = extractXmlBlocks(content, ['conversation_history', 'cross_group_history']);
  if (extractedHistory.blocks.length) {
    const historyHtml = extractedHistory.blocks.map(({ tagName, block }) => (
      renderInjectedHistorySection(
        block,
        parentMessage,
        tagName === 'cross_group_history' ? 'Cross-group history evidence' : 'Conversation history evidence',
      )
    )).join('');
    const rest = extractedHistory.rest.trim();
    const restHtml = rest
      ? `<div style="padding:8px 12px;font-size:11px;color:var(--text-2);line-height:1.5;white-space:pre-wrap;max-height:220px;overflow-y:auto;font-family:monospace">${escapeHtml(rest)}</div>`
      : '';
    return `
      <div style="border-bottom:1px solid var(--border);background:${style.bg}">
        <div style="padding:6px 12px;display:flex;align-items:center;gap:6px;background:${style.color}11">
          <span style="font-size:11px;font-weight:600;color:${style.color}">#${idx + 1} ${style.label}</span>
          <span style="font-size:10px;color:var(--text-3);margin-left:auto">${estimateTokens(content)} tokens</span>
        </div>
        ${historyHtml}
        ${restHtml}
      </div>
    `;
  }

  const parsed = parseXmlMessages(content);

  // 非 XML 格式：直接按纯文本展示
  if (!parsed || parsed.length === 0) {
    return `
      <div style="border-bottom:1px solid var(--border);background:${style.bg}">
        <div style="padding:6px 12px;display:flex;align-items:center;gap:6px;background:${style.color}11">
          <span style="font-size:11px;font-weight:600;color:${style.color}">#${idx + 1} ${style.label}</span>
          <span style="font-size:10px;color:var(--text-3);margin-left:auto">${estimateTokens(content)} tokens</span>
        </div>
        <div style="padding:8px 12px;font-size:11px;color:var(--text-2);line-height:1.5;white-space:pre-wrap;max-height:300px;overflow-y:auto;font-family:monospace">${escapeHtml(content)}</div>
      </div>
    `;
  }

  const innerHtml = parsed.map(item => {
    if (item.type === 'image') {
      if (item.sticker) {
        return `
          <div style="padding:6px 12px;display:flex;align-items:center;gap:6px;border-bottom:1px solid var(--border)">
            <span style="font-size:10px;font-weight:600;color:${getSpeakerColor(item.speaker)}">${escapeHtml(item.speaker)}</span>
            <span style="font-size:10px;color:var(--text-3)">🎞 表情包</span>
          </div>
        `;
      }
      return `
        <div style="padding:6px 12px;display:flex;align-items:center;gap:6px;border-bottom:1px solid var(--border)">
          <span style="font-size:10px;font-weight:600;color:${getSpeakerColor(item.speaker)}">${escapeHtml(item.speaker)}</span>
          <span style="font-size:10px;color:var(--text-3)">🖼 ${escapeHtml(item.caption || '图片')}</span>
        </div>
      `;
    }
    const speakerColor = getSpeakerColor(item.speaker);
    const compressed = isHistoryItemCompressed(item, parentMessage?.group_id || '');
    return `
      <div style="padding:6px 12px;border-bottom:1px solid var(--border)">
        <div style="display:flex;align-items:center;gap:6px;margin-bottom:3px">
          <span style="font-size:10px;font-weight:600;color:${speakerColor}">${escapeHtml(item.speaker)}</span>
          ${item.userId ? `<span style="font-size:9px;color:var(--text-3)">${escapeHtml(item.userId)}</span>` : ''}
          ${compressed ? `<span class="tag" style="font-size:9px;padding:1px 5px;background:${TAG_COLORS.compressed.bg};color:${TAG_COLORS.compressed.color};border:1px solid ${TAG_COLORS.compressed.border}">已压缩</span>` : ''}
          ${item.time ? `<span style="font-size:9px;color:var(--text-3);margin-left:auto">${escapeHtml(item.time)}</span>` : ''}
        </div>
        <div style="font-size:11px;color:var(--text-2);line-height:1.5;white-space:pre-wrap">${escapeHtml(item.content)}</div>
      </div>
    `;
  }).join('');

  return `
    <div style="border-bottom:1px solid var(--border);background:${style.bg}">
      <div style="padding:6px 12px;display:flex;align-items:center;gap:6px;background:${style.color}11">
        <span style="font-size:11px;font-weight:600;color:${style.color}">#${idx + 1} ${style.label}</span>
        <span style="font-size:10px;color:var(--text-3);margin-left:auto">${parsed.length} 条消息 · ${estimateTokens(content)} tokens</span>
      </div>
      ${innerHtml}
    </div>
  `;
}

function renderChainSystemMessage(content, style, idx) {
  const sections = parsePromptSections(content);
  const hasSections = sections.length > 1 || (sections.length === 1 && sections[0].type === 'section');
  const sectionIdPrefix = `chain-sys-${msgIdCounter}-${idx}`;

  const promptHtml = hasSections ? sections.map((section, sIdx) => {
    if (section.type === 'section') {
      const sid = `${sectionIdPrefix}-${sIdx}`;
      return `
        <div style="border-bottom:1px solid var(--border)">
          <div class="chain-section-header" data-target="${sid}"
               style="padding:6px 12px;cursor:pointer;display:flex;align-items:center;gap:6px;background:${section.color}08">
            <span class="chain-section-arrow" style="display:inline-block;transition:transform 0.2s;font-size:10px;color:var(--text-3)">▸</span>
            <span style="font-size:11px;font-weight:600;color:${section.color}">${section.label}</span>
            <span style="font-size:10px;color:var(--text-3);margin-left:auto">${section.content.length} chars</span>
          </div>
          <div id="${sid}" class="chain-section-body" style="display:none;padding:6px 12px;background:var(--bg-1);font-size:10px;color:var(--text-2);line-height:1.4;white-space:pre-wrap;max-height:200px;overflow-y:auto;font-family:monospace">${escapeHtml(section.content)}</div>
        </div>
      `;
    }
    return `<div style="padding:6px 12px;font-size:10px;color:var(--text-2);line-height:1.4;white-space:pre-wrap;max-height:150px;overflow-y:auto;font-family:monospace;border-bottom:1px solid var(--border)">${escapeHtml(truncate(section.content, 300))}</div>`;
  }).join('') : (
    content
      ? `<div style="padding:6px 12px;font-size:10px;color:var(--text-2);line-height:1.4;white-space:pre-wrap;max-height:150px;overflow-y:auto;font-family:monospace;border-bottom:1px solid var(--border)">${escapeHtml(truncate(content, 500))}</div>`
      : ''
  );

  return `
    <div style="border-bottom:1px solid var(--border);background:${style.bg}">
      <div style="padding:6px 12px;display:flex;align-items:center;gap:6px;background:${style.color}11">
        <span style="font-size:11px;font-weight:600;color:${style.color}">#${idx + 1} ${style.label}</span>
        <span style="font-size:10px;color:var(--text-3);margin-left:auto">${estimateTokens(content)} tokens · ${content.length} chars</span>
      </div>
      ${promptHtml}
    </div>
  `;
}


function conversationEntryKey(message) {
  const entryId = String(message.entry_id || '').trim();
  if (entryId) return `id:${entryId}`;
  const timestamp = message.timestamp || '';
  const role = message.role || '';
  const userId = message.user_id || '';
  const content = String(message.content || '').slice(0, 120);
  return `fallback:${timestamp}:${role}:${userId}:${content}`;
}

function buildConversationDeletePath(message) {
  const params = new URLSearchParams();
  const key = conversationEntryKey(message);
  if (key) params.set('key', key);
  if (message.group_id) params.set('group_id', message.group_id);
  return `/persona/conversations?${params.toString()}`;
}

async function deleteConversationMessage(message) {
  const label = String(message.content || message.entry_id || message.timestamp || '\u8be5\u6d88\u606f').slice(0, 40);
  if (!confirmDanger(`确定删除「${label}」吗？此操作不可撤销。`)) return;
  try {
    await del(buildConversationDeletePath(message));
    toast('\u5bf9\u8bdd\u5185\u5bb9\u5df2\u5220\u9664', 'success');
    await loadMessages(true);
  } catch (error) {
    if (error?.name === 'AbortError') return;
    toast('\u5220\u9664\u5931\u8d25: ' + error.message, 'error');
  }
}

function bindDeleteButtons() {
  scopedPage.$$('.conversation-delete').forEach(btn => {
    btn.addEventListener('click', (event) => {
      event.stopPropagation();
      const idx = Number(btn.dataset.deleteIndex);
      const message = messages[idx];
      if (message) deleteConversationMessage(message);
    });
  });
}

function renderChainDetailForButton(btn, target) {
  if (target.dataset.loaded === 'true') return;
  const idx = Number(btn.dataset.chainIndex);
  const message = messages[idx];
  const chain = buildConversationChain(message);
  target.innerHTML = renderChainMessages(chain, message);
  target.dataset.loaded = 'true';
  bindChainSectionToggles(target);
}

function bindChainDetailScroll(detail) {
  detail.addEventListener('wheel', (e) => {
    const { scrollTop, scrollHeight, clientHeight } = detail;
    const atTop = e.deltaY < 0 && scrollTop === 0;
    const atBottom = e.deltaY > 0 && scrollTop + clientHeight >= scrollHeight;
    if (!atTop && !atBottom) {
      e.stopPropagation();
    }
  }, { passive: true });
}

function bindChainSectionToggles(root) {
  root.querySelectorAll('.chain-section-header').forEach(header => {
    header.addEventListener('click', () => {
      const target = $(header.dataset.target);
      if (!target) return;
      const isOpen = target.style.display !== 'none';
      target.style.display = isOpen ? 'none' : 'block';
      const arrow = header.querySelector('.chain-section-arrow');
      if (arrow) arrow.style.transform = isOpen ? '' : 'rotate(90deg)';
    });
  });
}

function bindChainToggles() {
  scopedPage.$$('.chain-toggle').forEach(btn => {
    btn.addEventListener('click', () => {
      const target = $(btn.dataset.target);
      if (!target) return;
      const isOpen = target.style.display !== 'none';
      if (!isOpen) renderChainDetailForButton(btn, target);
      target.style.display = isOpen ? 'none' : 'block';
      const arrow = btn.querySelector('.toggle-arrow');
      if (arrow) arrow.style.transform = isOpen ? '' : 'rotate(90deg)';
    });
  });

  scopedPage.$$('.chain-detail').forEach(detail => {
    bindChainDetailScroll(detail);
    bindChainSectionToggles(detail);
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

function updateLiveIndicator() {
  const dot = $('liveDot');
  const label = $('liveLabel');
  if (dot) dot.style.background = isLive ? 'var(--success)' : 'var(--text-3)';
  if (label) label.textContent = isLive ? '事件实时' : '已暂停';
}
