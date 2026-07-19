import { store } from '../store.js';
import { get } from '../app.js';
import { toast } from '../components.js';
import {
  renderBarChart,
  renderLineChart,
  renderPieChart,
  renderRadarChart,
  disposeChart,
} from '../charts.js';
import { createRealtimeRefresh } from './realtime.js';
import { createScopedPage } from '../page-context.js';

const scopedPage = createScopedPage();
const $ = scopedPage.$;

const EMOTION_CN = {
  JOY:'喜悦',CONTENTMENT:'满足',RELIEF:'释然',EXCITEMENT:'兴奋',
  SADNESS:'悲伤',GRIEF:'悲痛',ANGER:'愤怒',IRRITATION:'恼怒',
  ANXIETY:'焦虑',LONELINESS:'孤独',FEAR:'恐惧',DISGUST:'厌恶',
  SURPRISE:'惊讶',TRUST:'信任',ANTICIPATION:'期待',LOVE:'喜爱',
  GRATITUDE:'感激',HOPE:'希望',NEUTRAL:'中性',CURIOSITY:'好奇',
  CONFUSION:'困惑',unknown:'未知','':'未知',
};

const INTENT_CN = {
  help_seeking:'求助',emotional:'情感表达',social:'社交',
  silent:'沉默',plugin_command:'插件指令',unknown:'未知','':'未知',
};

const STRATEGY_CN = {
  immediate:'立即回复',delayed:'延迟回复',silent:'静默',
  plugin:'插件触发',unknown:'未知','':'未知',
};

const HEAT_CN = {
  cold:'冷清',warm:'温和',hot:'活跃',overheated:'过热',
  unknown:'未知','':'未知',
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

const STRATEGY_COLORS = ['#52c41a','#faad14','#8c8c8c','#1890ff','#722ed1'];
const HEAT_COLORS = ['#d9d9d9','#95de64','#ffc53d','#ff4d4f'];
const realtime = createRealtimeRefresh(() => refreshRealtime(true), {
  resources: ['cognition'],
  debounceMs: 600,
});

export function dispose() {
  scopedPage.use(null, null);
  realtime.stop();
}

export async function init(container, params = {}) {
  scopedPage.use(params?.ctx, container);
  const name = store.currentPersona;
  if (!name) {
    container.innerHTML = `
      <div class="card">
        <div class="card-header">
          <div class="card-title">认知分析</div>
        </div>
        <div style="padding:40px;text-align:center;color:var(--text-3)">
          <div style="font-size:48px;margin-bottom:16px">✦</div>
          <div style="font-size:16px;margin-bottom:8px">请先选择人格</div>
          <div style="font-size:13px">在顶部导航栏中选择要查看的人格</div>
        </div>
      </div>
    `;
    return;
  }

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

    <div style="margin-top:32px;border-top:1px solid var(--border-1);padding-top:24px">
      <div class="card">
        <div class="card-header"><div class="card-title">深度分析</div></div>
        <div class="stat-grid" id="analysisStats"></div>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-top:20px">
        <div class="card">
          <div class="card-header"><div class="card-title">社交意图分布</div></div>
          <div data-chart="intent-dist" style="min-height:300px"></div>
        </div>
        <div class="card">
          <div class="card-header"><div class="card-title">回复策略分布</div></div>
          <div data-chart="strategy-dist" style="min-height:300px"></div>
        </div>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-top:20px">
        <div class="card">
          <div class="card-header"><div class="card-title">活跃时段分布</div></div>
          <div data-chart="hourly-dist" style="min-height:300px"></div>
        </div>
        <div class="card">
          <div class="card-header"><div class="card-title">群聊热度分布</div></div>
          <div data-chart="heat-dist" style="min-height:300px"></div>
        </div>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr 1fr;gap:20px;margin-top:20px">
        <div class="card">
          <div class="card-header"><div class="card-title">定向分数分布</div></div>
          <div data-chart="score-directed" style="min-height:250px"></div>
        </div>
        <div class="card">
          <div class="card-header"><div class="card-title">讽刺分数分布</div></div>
          <div data-chart="score-sarcasm" style="min-height:250px"></div>
        </div>
        <div class="card">
          <div class="card-header"><div class="card-title">资格感分数分布</div></div>
          <div data-chart="score-entitlement" style="min-height:250px"></div>
        </div>
      </div>
      <div class="card" style="margin-top:20px">
        <div class="card-header"><div class="card-title">决策时间线（分数 vs 阈值）</div></div>
        <div data-chart="decision-timeline" style="min-height:300px"></div>
      </div>
      <div style="display:grid;grid-template-columns:1fr 1fr;gap:20px;margin-top:20px">
        <div class="card">
          <div class="card-header"><div class="card-title">决策原因 TOP 15</div></div>
          <div id="reasonTable"></div>
        </div>
        <div class="card">
          <div class="card-header"><div class="card-title">群组对比</div></div>
          <div id="groupSummaryTable"></div>
        </div>
      </div>
      <div class="card" style="margin-top:20px">
        <div class="card-header"><div class="card-title">决策事件</div></div>
        <div id="decisionTable"></div>
      </div>
    </div>
  `;

  await refreshRealtime(false);
  realtime.start();
}

async function refreshRealtime(silent = true) {
  await Promise.all([loadData(silent), loadAnalysis(silent)]);
}

async function loadData(silent = false) {
  const name = store.currentPersona;
  if (!name) {
    toast('请先选择一个人格', 'error');
    return;
  }
  try {
    const res = await get(`/persona/cognition?limit=100`);
    renderStats(res.events || [], res.emotion_distribution || {});
    renderEmotionDistribution(res.emotion_distribution || {});
    renderTimeline(res.events || []);
    renderRadarChart_(res.events || []);
    renderEventsTable(res.events || []);
  } catch (e) {
    if (e?.name === 'AbortError') return;
    if (!silent) toast('加载认知数据失败', 'error');
    else console.warn('cognition realtime refresh failed:', e);
  }
}

async function loadAnalysis(silent = false) {
  const name = store.currentPersona;
  if (!name) return;
  try {
    const res = await get(`/persona/cognition/analysis`);
    if (!res.has_data) return;
    renderAnalysisStats(res);
    renderIntentDistribution(res.intent_distribution || {});
    renderStrategyDistribution(res.strategy_distribution || {});
    renderHourlyDistribution(res.hourly_distribution || {});
    renderHeatDistribution(res.decision_summary?.heat_distribution || {});
    renderScoreHistogram('score-directed', '定向', res.score_histograms?.directed);
    renderScoreHistogram('score-sarcasm', '讽刺', res.score_histograms?.sarcasm);
    renderScoreHistogram('score-entitlement', '资格感', res.score_histograms?.entitlement);
    renderDecisionTimeline(res.decision_timeline || []);
    renderReasonTable(res.decision_summary?.reason_distribution || {});
    renderGroupSummaryTable(res.group_summary || []);
    renderDecisionTable(res.decision_timeline || []);
  } catch (e) {
    if (e?.name === 'AbortError') return;
    console.warn('加载深度分析数据失败:', e);
  }
}

function renderStats(events, dist) {
  const el = $('cogStats');
  if (!el) return;
  el.innerHTML = `
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

function renderAnalysisStats(res) {
  const el = $('analysisStats');
  if (!el) return;
  const ds = res.decision_summary || {};
  const strategyDist = res.strategy_distribution || {};
  const totalDecisions = Object.values(strategyDist).reduce((a, b) => a + b, 0);
  const silentCount = strategyDist.silent || 0;
  const silentRate = totalDecisions > 0 ? ((silentCount / totalDecisions) * 100).toFixed(1) : '0.0';
  el.innerHTML = `
    <div class="stat-card">
      <div class="stat-label">决策事件总数</div>
      <div class="stat-value">${ds.total || 0}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">平均复合分数</div>
      <div class="stat-value">${(ds.avg_score || 0).toFixed(3)}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">平均动态阈值</div>
      <div class="stat-value">${(ds.avg_threshold || 0).toFixed(3)}</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">静默率</div>
      <div class="stat-value">${silentRate}%</div>
    </div>
    <div class="stat-card">
      <div class="stat-label">平均消息速率</div>
      <div class="stat-value">${(ds.avg_msg_rate || 0).toFixed(2)}/min</div>
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
  const el = scopedPage.query('[data-chart="emotion-dist"]');
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
  const el = scopedPage.query('[data-chart="timeline"]');
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
  const el = scopedPage.query('[data-chart="radar"]');
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
  if (!el) return;
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

// ── 深度分析：意图分布饼图 ──────────────────────────────────────

function renderIntentDistribution(dist) {
  const el = scopedPage.query('[data-chart="intent-dist"]');
  const entries = Object.entries(dist);
  if (!entries.length) return;
  renderPieChart(el, {
    data: entries.map(([k, v]) => ({
      name: INTENT_CN[k] || k || '未知',
      value: v,
    })),
  });
}

// ── 深度分析：策略分布饼图 ──────────────────────────────────────

function renderStrategyDistribution(dist) {
  const el = scopedPage.query('[data-chart="strategy-dist"]');
  if (!el) return;
  const entries = Object.entries(dist);
  if (!entries.length) {
    el.innerHTML = '<div style="color:var(--text-3);padding:48px;text-align:center">暂无决策数据</div>';
    return;
  }
  renderPieChart(el, {
    data: entries.map(([k, v]) => ({
      name: STRATEGY_CN[k] || k || '未知',
      value: v,
    })),
  });
}

// ── 深度分析：时段分布 ──────────────────────────────────────────

function renderHourlyDistribution(dist) {
  const el = scopedPage.query('[data-chart="hourly-dist"]');
  const hours = Array.from({ length: 24 }, (_, i) => i);
  const labels = hours.map(h => `${String(h).padStart(2, '0')}:00`);
  const values = hours.map(h => dist[h] || 0);
  if (values.every(v => v === 0)) return;
  renderBarChart(el, {
    labels,
    data: [{ name: '认知事件数', values }],
    colors: ['#1890ff'],
  });
}

// ── 深度分析：热度分布 ──────────────────────────────────────────

function renderHeatDistribution(dist) {
  const el = scopedPage.query('[data-chart="heat-dist"]');
  if (!el) return;
  const entries = Object.entries(dist);
  if (!entries.length) {
    el.innerHTML = '<div style="color:var(--text-3);padding:48px;text-align:center">暂无决策数据</div>';
    return;
  }
  const order = ['cold', 'warm', 'hot', 'overheated'];
  const sorted = entries.sort((a, b) => order.indexOf(a[0]) - order.indexOf(b[0]));
  renderPieChart(el, {
    data: sorted.map(([k, v]) => ({
      name: HEAT_CN[k] || k,
      value: v,
    })),
  });
}

// ── 深度分析：分数直方图 ────────────────────────────────────────

function renderScoreHistogram(chartId, label, histogram) {
  const el = scopedPage.query(`[data-chart="${chartId}"]`);
  if (!histogram || !histogram.labels || !histogram.labels.length) return;
  renderBarChart(el, {
    labels: histogram.labels,
    data: [{ name: label + ' 分数', values: histogram.counts }],
    colors: ['#722ed1'],
  });
}

// ── 深度分析：决策时间线 ────────────────────────────────────────

function renderDecisionTimeline(timeline) {
  const el = scopedPage.query('[data-chart="decision-timeline"]');
  if (!timeline.length) return;
  const sorted = [...timeline].sort((a, b) => a.timestamp - b.timestamp);
  const labels = sorted.map(e => {
    const d = new Date(e.timestamp * 1000);
    return `${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')} ${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
  });
  renderLineChart(el, {
    labels,
    series: [
      { name: '复合分数 (score)', data: sorted.map(e => e.score || 0) },
      { name: '动态阈值 (threshold)', data: sorted.map(e => e.threshold || 0) },
    ],
    areaStyle: false,
  });
}

// ── 深度分析：决策原因表格 ──────────────────────────────────────

function renderReasonTable(dist) {
  const el = $('reasonTable');
  if (!el) return;
  const entries = Object.entries(dist);
  if (!entries.length) {
    el.innerHTML = '<div style="color:var(--text-3);padding:24px;text-align:center">暂无决策数据</div>';
    return;
  }
  el.innerHTML = `
    <table class="table">
      <thead>
        <tr><th>原因</th><th>次数</th></tr>
      </thead>
      <tbody>
        ${entries.map(([reason, count]) => `
          <tr><td style="font-size:13px">${reason}</td><td>${count}</td></tr>
        `).join('')}
      </tbody>
    </table>
  `;
}

// ── 深度分析：群组对比表格 ──────────────────────────────────────

function renderGroupSummaryTable(groups) {
  const el = $('groupSummaryTable');
  if (!el) return;
  if (!groups.length) {
    el.innerHTML = '<div style="color:var(--text-3);padding:24px;text-align:center">暂无群组数据</div>';
    return;
  }
  el.innerHTML = `
    <table class="table">
      <thead>
        <tr>
          <th>群组 ID</th>
          <th>事件数</th>
          <th>活跃用户</th>
          <th>平均效价</th>
          <th>平均唤醒度</th>
        </tr>
      </thead>
      <tbody>
        ${groups.map(g => `
          <tr>
            <td>${g.group_id || '—'}</td>
            <td>${g.event_count}</td>
            <td>${g.unique_users}</td>
            <td style="color:${(g.avg_valence || 0) >= 0 ? '#52c41a' : '#ff4d4f'}">${(g.avg_valence || 0).toFixed(3)}</td>
            <td>${(g.avg_arousal || 0).toFixed(3)}</td>
          </tr>
        `).join('')}
      </tbody>
    </table>
  `;
}

// ── 深度分析：决策事件表格 ──────────────────────────────────────

function renderDecisionTable(timeline) {
  const el = $('decisionTable');
  if (!el) return;
  if (!timeline.length) {
    el.innerHTML = '<div style="color:var(--text-3);padding:24px;text-align:center">暂无决策事件</div>';
    return;
  }
  el.innerHTML = `
    <table class="table">
      <thead>
        <tr>
          <th>时间</th>
          <th>策略</th>
          <th>分数</th>
          <th>阈值</th>
          <th>热度</th>
          <th>消息速率</th>
          <th>表达力</th>
          <th>灵敏度</th>
          <th>原因</th>
        </tr>
      </thead>
      <tbody>
        ${timeline.map(d => `
          <tr>
            <td>${formatTs(d.timestamp)}</td>
            <td>${STRATEGY_CN[d.strategy] || d.strategy}</td>
            <td>${(d.score || 0).toFixed(3)}</td>
            <td>${(d.threshold || 0).toFixed(3)}</td>
            <td>${HEAT_CN[d.heat_level] || d.heat_level}</td>
            <td>${(d.msg_rate || 0).toFixed(2)}</td>
            <td>${(d.expressiveness || 0).toFixed(2)}</td>
            <td>${(d.sensitivity || 0).toFixed(2)}</td>
            <td style="font-size:12px;max-width:200px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${d.reason || '—'}</td>
          </tr>
        `).join('')}
      </tbody>
    </table>
  `;
}
