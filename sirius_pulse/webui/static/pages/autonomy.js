import { get } from '../app.js';
import { toast } from '../components.js';
import { createScopedPage } from '../page-context.js';
import { store } from '../store.js';
import { createRealtimeRefresh } from './realtime.js';

const scopedPage = createScopedPage();
const $ = scopedPage.$;

const KIND_LABELS = {
  reading: '读东西',
  building: '动手做',
  image: '画画',
  note: '记下来',
  musing: '想事情',
  share: '去分享',
};

const RESOLUTION_LABELS = {
  do: '想弄明白',
  tell: '想说给人听',
};

const STATUS_LABELS = {
  nascent: '刚冒出来',
  active: '惦记着',
  resolved: '已了结',
  dropped: '放下了',
};

// 心跳轮询是兜底：实时推送若不可用（例如没连上 WebSocket），页面仍然会更新。
const POLL_MS = 15000;

const realtime = createRealtimeRefresh(() => load(true), {
  resources: ['autonomy'],
  debounceMs: 400,
});

export function dispose() {
  scopedPage.use(null, null);
  realtime.stop();
}

export async function init(container, params = {}) {
  scopedPage.use(params?.ctx, container);
  if (!store.currentPersona) {
    container.innerHTML = `
      <div class="card">
        <div class="card-header"><div class="card-title">自主行为</div></div>
        <div class="autonomy-empty">
          <div class="autonomy-empty-title">请先选择人格</div>
          <div class="autonomy-empty-detail">在顶部导航栏中选择要查看的人格</div>
        </div>
      </div>
    `;
    return;
  }

  $('autonomyRefresh')?.addEventListener('click', () => load(false));

  realtime.start();
  scopedPage.on(window, 'sirius:event', onLiveEvent);

  await load(false);
  scopedPage.interval(() => load(true), POLL_MS);
}

async function load(silent) {
  const connection = $('autonomyConnection');
  if (connection) {
    connection.textContent = silent ? '更新中' : '读取中';
    connection.className = 'autonomy-connection is-loading';
  }
  try {
    const data = await get('/persona/autonomy');
    if (!$('autonomyPage')) return;
    render(data);
    if (connection) {
      connection.textContent = '已同步';
      connection.className = 'autonomy-connection is-live';
    }
  } catch (error) {
    if (error?.name === 'AbortError' || !$('autonomyPage')) return;
    if (connection) {
      connection.textContent = '读取失败';
      connection.className = 'autonomy-connection is-error';
    }
    if (!silent) toast('自主记录加载失败', 'error');
  }
}

function render(data) {
  const summary = data?.summary || {};
  const intentions = Array.isArray(data?.intentions) ? data.intentions : [];
  const episodes = Array.isArray(data?.episodes) ? data.episodes : [];
  const shared = episodes.filter((item) => item.kind === 'share').length;

  setText('autonomyOpen', String(summary.intentions_open ?? 0));
  setText('autonomyEpisodeTotal', String(summary.episodes_total ?? 0));
  setText('autonomyShared', String(shared));
  setText('autonomyLastAt', shortTime(summary.last_episode_at));

  const newest = episodes[0];
  setText('autonomyLastKind', newest ? kindLabel(newest.kind) : '还没有过');

  setText('autonomyIntentCount', `${intentions.length} 条`);
  setText('autonomyEpisodeCount', `${episodes.length} 条`);
  renderIntentions(intentions);
  renderEpisodes(episodes);
  renderFootnote(data?.paths);
}

function renderIntentions(items) {
  const box = $('autonomyIntentions');
  if (!box) return;
  if (!items.length) {
    box.innerHTML = emptyState(
      '她此刻没有惦记着什么',
      '下一轮心跳时，她可能会在群聊里遇到什么，然后自己决定要不要放在心上。'
    );
    return;
  }
  box.innerHTML = items.map(intentionCard).join('');
}

function intentionCard(item) {
  const resolution = String(item.resolution || 'do');
  const status = String(item.status || '');
  const spent = Number(item.attempts || 0) >= 3;
  const audience = String(item.audience_label || item.audience || '').trim();
  const urgency = Number(item.urgency || 0);
  const attempts = Number(item.attempts || 0);

  const badges = [
    `<span class="tag autonomy-tag-resolution">${escapeHtml(
      RESOLUTION_LABELS[resolution] || resolution
    )}</span>`,
    `<span class="tag">${escapeHtml(KIND_LABELS[item.kind] || item.kind || '念头')}</span>`,
  ];
  if (status && STATUS_LABELS[status]) {
    badges.push(
      `<span class="tag ${status === 'resolved' ? 'tag-success' : ''}">${escapeHtml(
        STATUS_LABELS[status]
      )}</span>`
    );
  }
  if (resolution === 'tell' && audience) {
    badges.push(`<span class="tag tag-accent">说给 ${escapeHtml(audience)}</span>`);
  }

  // 「为什么会去干这件事」——why/seed 就是回答，必须显式呈现，否则自主看起来像随机。
  const why = String(item.why || '').trim();
  const refs = Array.isArray(item.refs) ? item.refs.filter(Boolean) : [];
  const outcome = String(item.outcome || '').trim();

  return `
    <article class="autonomy-item${spent ? ' is-spent' : ''}">
      <div class="autonomy-item-head">
        <div class="autonomy-item-what">${escapeHtml(item.what || '（没有写下内容）')}</div>
        <div class="autonomy-urgency" title="紧迫度">${Math.round(urgency * 100)}%</div>
      </div>
      <div class="autonomy-item-tags">${badges.join('')}</div>
      ${why ? `<div class="autonomy-item-why">因为：${escapeHtml(why)}</div>` : ''}
      ${refs.length ? `<div class="autonomy-item-refs">${refs.map(refTag).join('')}</div>` : ''}
      ${outcome ? `<div class="autonomy-item-outcome">结果：${escapeHtml(outcome)}</div>` : ''}
      <div class="autonomy-item-meta">
        <span>${escapeHtml(shortTime(item.created_at))}</span>
        ${attempts ? `<span>试过 ${attempts} 次${spent ? '（已达上限，不再重复）' : ''}</span>` : ''}
      </div>
    </article>
  `;
}

function renderEpisodes(items) {
  const box = $('autonomyEpisodes');
  if (!box) return;
  if (!items.length) {
    box.innerHTML = emptyState(
      '她还没有自己做过什么',
      '这里只记录真的发生了的事。她不会每轮心跳都动——大多数时候，什么都不做才是对的。'
    );
    return;
  }
  box.innerHTML = items.map(episodeRow).join('');
}

function episodeRow(item) {
  const audience = String(item.audience || '').trim();
  const seed = String(item.seed || '').trim();
  const outcome = String(item.outcome || '').trim();
  const refs = Array.isArray(item.refs) ? item.refs.filter(Boolean) : [];

  return `
    <article class="autonomy-episode">
      <div class="autonomy-episode-rail">
        <span class="autonomy-episode-dot autonomy-kind-${escapeHtml(item.kind || 'musing')}"></span>
      </div>
      <div class="autonomy-episode-body">
        <div class="autonomy-episode-head">
          <span class="autonomy-episode-kind">${escapeHtml(kindLabel(item.kind))}</span>
          <span class="autonomy-episode-time">${escapeHtml(shortTime(item.ended_at || item.started_at))}</span>
        </div>
        ${outcome ? `<div class="autonomy-episode-outcome">${escapeHtml(truncate(outcome, 400))}</div>` : ''}
        ${seed ? `<div class="autonomy-episode-seed">起因：${escapeHtml(truncate(seed, 200))}</div>` : ''}
        ${audience ? `<div class="autonomy-episode-to">说给了 ${escapeHtml(audience)}</div>` : ''}
        ${refs.length ? `<div class="autonomy-item-refs">${refs.map(refTag).join('')}</div>` : ''}
      </div>
    </article>
  `;
}

function onLiveEvent(event) {
  const detail = event?.detail || {};
  if (detail.type !== 'agent_turn_updated') return;
  const data = detail.data || {};
  // 只有她自己发起的回合才属于这一页；普通回复不进这里。
  if (data.origin !== 'self_initiated') return;
  if (!$('autonomyPage')) return;

  const episode = data.episode || {};
  renderLive(episode);
  load(true);
}

function renderLive(episode) {
  const card = $('autonomyLiveCard');
  const body = $('autonomyLive');
  if (!card || !body) return;
  card.hidden = false;
  const audience = String(episode.audience || '').trim();
  const outcome = String(episode.outcome || '').trim();
  body.innerHTML = `
    <div class="autonomy-live-head">
      <span class="autonomy-episode-kind">${escapeHtml(kindLabel(episode.kind))}</span>
      <span class="autonomy-episode-time">${escapeHtml(shortTime(episode.ended_at))}</span>
    </div>
    ${outcome ? `<div class="autonomy-live-outcome">${escapeHtml(truncate(outcome, 300))}</div>` : ''}
    ${audience ? `<div class="autonomy-episode-to">说给了 ${escapeHtml(audience)}</div>` : ''}
  `;
}

function renderFootnote(paths) {
  const box = $('autonomyFootnote');
  if (!box || !paths) return;
  box.innerHTML =
    '记录文件：' +
    `<code>${escapeHtml(paths.intentions || '')}</code> · ` +
    `<code>${escapeHtml(paths.episodes || '')}</code>`;
}

function refTag(ref) {
  const text = String(ref || '');
  if (/^https?:\/\//i.test(text)) {
    return `<a class="autonomy-ref" href="${escapeHtml(text)}" target="_blank" rel="noopener noreferrer">${escapeHtml(
      truncate(text, 60)
    )}</a>`;
  }
  return `<span class="autonomy-ref">${escapeHtml(truncate(text, 60))}</span>`;
}

function emptyState(title, detail) {
  return `
    <div class="autonomy-empty">
      <div class="autonomy-empty-title">${escapeHtml(title)}</div>
      <div class="autonomy-empty-detail">${escapeHtml(detail)}</div>
    </div>
  `;
}

function kindLabel(kind) {
  const key = String(kind || '');
  if (KIND_LABELS[key]) return KIND_LABELS[key];
  // kind 是自由标签，不是枚举：没见过的就照原样显示，别藏起来。
  return key || '做点什么';
}

function setText(id, text) {
  const el = $(id);
  if (el) el.textContent = text;
}

function truncate(value, max) {
  const text = String(value || '');
  return text.length > max ? `${text.slice(0, max)}…` : text;
}

function shortTime(value) {
  const text = String(value || '');
  if (!text) return '—';
  const date = new Date(text);
  if (Number.isNaN(date.getTime())) return text;
  const pad = (n) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(
    date.getHours()
  )}:${pad(date.getMinutes())}`;
}

function escapeHtml(value) {
  return String(value ?? '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}
