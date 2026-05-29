import { store } from '../store.js';
import { get } from '../app.js';
import { toast, $ } from '../components.js';
import {
  renderBarChart,
  renderLineChart,
  renderRadarChart,
  disposeChart,
} from '../charts.js';

const EMOTION_CN = {
  JOY:'喜悦',CONTENTMENT:'满足',RELIEF:'释然',EXCITEMENT:'兴奋',
  SADNESS:'悲伤',GRIEF:'悲痛',ANGER:'愤怒',IRRITATION:'恼怒',
  ANXIETY:'焦虑',LONELINESS:'孤独',FEAR:'恐惧',DISGUST:'厌恶',
  SURPRISE:'惊讶',TRUST:'信任',ANTICIPATION:'期待',LOVE:'喜爱',
  GRATITUDE:'感激',HOPE:'希望',NEUTRAL:'中性',CURIOSITY:'好奇',
  CONFUSION:'困惑',unknown:'未知','':'未知',
};

const RADAR_KEYS = [
  'mention_score','reference_score','name_match_score','second_person_score',
  'question_score','imperative_score','topic_relevance_score',
  'emotional_disclosure_score','attention_seeking_score',
  'recency_score','turn_taking_score',
];
const RADAR_LABELS = [
  '提及','引用','名称匹配','第二人称','问句','祈使',
  '话题相关','情感表露','寻求关注','时效','轮次',
];

export async function init(container) {
  container.innerHTML = `
    <div class="card">
      <div class="card-header"><div class="card-title">认知分析</div></div>
      <div class="stat-grid" id="cogStats"></div>
    </div>
    <div style="display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-top:20px">
      <div class="card">
        <div class="card-header"><div class="card-title">情绪分布</div></div>
        <div data-chart="emotion-dist" style="min-height:300px"></div>
      </div>
      <div class="card">
        <div class="card-header"><div class="card-title">定向信号雷达</div></div>
        <div data-chart="radar" style="min-height:300px"></div>
      </div>
    </div>
    <div class="card" style="margin-top:20px">
      <div class="card-header"><div class="card-title">情绪时间线</div></div>
      <div data-chart="timeline" style="min-height:300px"></div>
    </div>
    <div class="card" style="margin-top:20px">
      <div class="card-header"><div class="card-title">认知事件</div></div>
      <div id="cogTable"></div>
    </div>
  `;

  await loadData();
}

async function loadData() {
  const name = store.currentPersona;
  if (!name) {
    toast('请先选择一个人格', 'error');
    return;
  }
  try {
    const res = await get(`/personas/${name}/cognition?limit=100`);
    renderStats(res.events || [], res.emotion_distribution || {});
    renderEmotionDistribution(res.emotion_distribution || {});
    renderTimeline(res.events || []);
    renderRadarChart_(res.events || []);
    renderEventsTable(res.events || []);
  } catch (e) {
    toast('加载认知数据失败', 'error');
  }
}

function renderStats(events, dist) {
  $('cogStats').innerHTML = `
    <div class="stat-card">
      <div class="stat-label">事件总数</div>
      <div class="stat-value">${events.length}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">情绪类型</div>
      <div class="stat-value">${Object.keys(dist).length}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">最新事件</div>
      <div class="stat-value" style="font-size:16px">${events.length ? formatTs(events[0].timestamp) : '—'}</div>
    </div>
  `;
}

function formatTs(ts) {
  if (!ts) return '—';
  return new Date(ts * 1000).toLocaleString('zh-CN');
}

function translateEmotion(em) {
  return EMOTION_CN[em] || EMOTION_CN[em?.toUpperCase()] || em || '未知';
}

function renderEmotionDistribution(dist) {
  const el = document.querySelector('[data-chart="emotion-dist"]');
  const entries = Object.entries(dist);
  if (!entries.length) return;
  renderBarChart(el, {
    labels: entries.map(([k]) => translateEmotion(k)),
    data: [{ name: '次数', values: entries.map(([, v]) => v) }],
    colors: ['#4c9aff'],
    horizontal: true,
  });
}

function renderTimeline(events) {
  const el = document.querySelector('[data-chart="timeline"]');
  if (!events.length) return;
  const sorted = [...events].sort((a, b) => a.timestamp - b.timestamp);
  const labels = sorted.map(e => {
    const d = new Date(e.timestamp * 1000);
    return `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
  });
  renderLineChart(el, {
    labels,
    series: [
      { name: '效价 (valence)', data: sorted.map(e => e.valence || 0) },
      { name: '唤醒度 (arousal)', data: sorted.map(e => e.arousal || 0) },
      { name: '强度 (intensity)', data: sorted.map(e => e.intensity || 0) },
    ],
    areaStyle: false,
  });
}

function renderRadarChart_(events) {
  const el = document.querySelector('[data-chart="radar"]');
  if (!events.length) return;

  const sums = RADAR_KEYS.map(() => 0);
  let count = 0;
  for (const ev of events) {
    const sig = ev.directed_signals;
    if (!sig || typeof sig !== 'object') continue;
    RADAR_KEYS.forEach((k, i) => { sums[i] += (sig[k] || 0); });
    count++;
  }
  if (!count) return;

  const avgValues = sums.map(s => Math.round((s / count) * 100) / 100);
  renderRadarChart(el, {
    indicators: RADAR_LABELS.map((label, i) => ({
      name: label,
      max: Math.max(avgValues[i] * 2, 1),
    })),
    data: avgValues,
    color: '#7c4dff',
  });
}

function renderEventsTable(events) {
  const el = $('cogTable');
  if (!events.length) {
    el.innerHTML = '<div style="color:var(--text-3);padding:24px;text-align:center">暂无认知事件</div>';
    return;
  }

  el.innerHTML = `
    <table class="table">
      <thead>
        <tr>
          <th>时间</th>
          <th>用户</th>
          <th>情绪</th>
          <th>定向分数</th>
          <th>讽刺分数</th>
          <th>社交意图</th>
          <th>紧急度</th>
          <th>相关度</th>
        </tr>
      </thead>
      <tbody>
        ${events.map(e => `
          <tr>
            <td>${formatTs(e.timestamp)}</td>
            <td>${e.user_id || '—'}</td>
            <td>${translateEmotion(e.basic_emotion)}</td>
            <td>${(e.directed_score || 0).toFixed(2)}</td>
            <td>${(e.sarcasm_score || 0).toFixed(2)}</td>
            <td>${e.social_intent || '—'}</td>
            <td>${(e.urgency_score || 0).toFixed(2)}</td>
            <td>${(e.relevance_score || 0).toFixed(2)}</td>
          </tr>
        `).join('')}
      </tbody>
    </table>
  `;
}
