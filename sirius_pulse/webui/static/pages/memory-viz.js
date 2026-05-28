import { store } from '../store.js';
import { get } from '../app.js';
import { toast, $ } from '../components.js';
import { setChartOption, getChart, disposeChart } from '../charts.js';

const ROLE_COLORS = {
  human: '#58a6ff',
  assistant: '#3fb950',
  system: '#a371f7',
};

let charts = [];
let cachedData = null;

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
        <div class="card-title">基础记忆时间线</div>
        <div style="display:flex;gap:8px;align-items:center">
          <select id="timelineGroup" class="btn btn-sm" style="display:none;padding:2px 8px"></select>
          <select id="timelineRange" class="btn btn-sm" style="padding:2px 8px">
            <option value="7">近 7 天</option>
            <option value="30" selected>近 30 天</option>
            <option value="90">近 90 天</option>
            <option value="0">全部</option>
          </select>
          <button class="btn btn-sm" id="refreshViz">刷新</button>
        </div>
      </div>
      <div id="timelineChart" class="chart-container" style="min-height:360px;padding:16px"></div>
    </div>
    <div class="card" style="margin-bottom:20px">
      <div class="card-header">
        <div class="card-title">日记语义聚类</div>
      </div>
      <div id="clusterChart" class="chart-container" style="min-height:400px;padding:16px"></div>
    </div>
    <div class="card">
      <div class="card-header">
        <div class="card-title">用户-话题二部图</div>
      </div>
      <div id="graphChart" class="chart-container" style="min-height:400px;padding:16px"></div>
    </div>
  `;

  $('refreshViz').addEventListener('click', () => loadViz());
  $('timelineRange').addEventListener('change', () => {
    if (cachedData) {
      renderTimeline(cachedData.basic_timeline || {});
    }
  });
  $('timelineGroup').addEventListener('change', () => {
    if (cachedData) {
      renderTimeline(cachedData.basic_timeline || {});
    }
  });
  charts = [];
  await loadViz();
}

async function loadViz() {
  const name = store.currentPersona;
  try {
    const data = await get(`/personas/${name}/memory-viz`);
    cachedData = data;

    // 填充群聊选择器
    const groups = data.groups || [];
    const groupSelect = $('timelineGroup');
    if (groups.length > 1) {
      groupSelect.style.display = '';
      const prev = groupSelect.value;
      groupSelect.innerHTML = '<option value="">全部群聊</option>' +
        groups.map(g => `<option value="${g}">${g}</option>`).join('');
      if (prev && groups.includes(prev)) groupSelect.value = prev;
    } else {
      groupSelect.style.display = 'none';
      groupSelect.innerHTML = '';
    }

    renderTimeline(data.basic_timeline || {});
    renderCluster(data.diary_entries || []);
    renderGraph(data);
  } catch (e) {
    toast('加载可视化数据失败: ' + e.message, 'error');
  }
}

function renderTimeline(timeline) {
  const container = $('timelineChart');
  const buckets = timeline.buckets || {};
  const allDays = timeline.days || [];
  const recent = timeline.recent || [];

  if (!allDays.length && !recent.length) {
    container.innerHTML = '<div style="text-align:center;color:var(--text-3);padding:80px 0">暂无记忆时间线数据</div>';
    return;
  }

  // 读取筛选条件
  const daysRange = parseInt($('timelineRange')?.value || '30', 10);
  const groupSel = $('timelineGroup')?.value || '';

  // 根据时间范围过滤天数
  let filteredDays = allDays;
  if (daysRange > 0) {
    const cutoff = new Date();
    cutoff.setDate(cutoff.getDate() - daysRange);
    const cutoffStr = cutoff.toISOString().slice(0, 10);
    filteredDays = allDays.filter(d => d >= cutoffStr);
  }

  // 按天聚合：{human: N, assistant: N, system: N}
  const dailyData = {};
  for (const day of filteredDays) {
    const dayGroups = buckets[day] || {};
    const agg = { human: 0, assistant: 0, system: 0 };
    for (const [gid, counts] of Object.entries(dayGroups)) {
      if (groupSel && gid !== groupSel) continue;
      for (const role of ['human', 'assistant', 'system']) {
        agg[role] += counts[role] || 0;
      }
    }
    if (agg.human + agg.assistant + agg.system > 0) {
      dailyData[day] = agg;
    }
  }

  const days = Object.keys(dailyData).sort();
  if (!days.length) {
    container.innerHTML = '<div style="text-align:center;color:var(--text-3);padding:80px 0">当前筛选条件下暂无数据</div>';
    return;
  }

  const roles = [
    { key: 'human', name: '用户消息', color: ROLE_COLORS.human },
    { key: 'assistant', name: 'AI 回复', color: ROLE_COLORS.assistant },
    { key: 'system', name: '系统消息', color: ROLE_COLORS.system },
  ];

  const series = roles.map(r => ({
    name: r.name,
    type: 'bar',
    stack: 'total',
    barMaxWidth: 28,
    itemStyle: { color: r.color, borderRadius: r.key === 'system' ? [3, 3, 0, 0] : 0 },
    emphasis: { focus: 'series' },
    data: days.map(d => dailyData[d][r.key] || 0),
  }));

  const chart = getChart(container);
  if (chart) charts.push(chart);

  setChartOption(container, {
    backgroundColor: 'transparent',
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      formatter: (params) => {
        const day = params[0]?.axisValue || '';
        const lines = params
          .filter(p => p.value > 0)
          .map(p => `${p.marker} ${p.seriesName}: <b>${p.value}</b>`)
          .join('<br/>');
        if (!lines) return '';
        // 查找当天的最近消息用于预览
        const dayBuckets = buckets[day];
        let preview = '';
        if (dayBuckets) {
          const dayMsgs = recent
            .filter(r => r.timestamp?.startsWith(day) && (!groupSel || r.group_id === groupSel))
            .slice(0, 3);
          if (dayMsgs.length) {
            preview = '<div style="margin-top:6px;padding-top:6px;border-top:1px solid #30363d;font-size:11px;color:#8b949e">';
            preview += dayMsgs
              .map(m => `${m.speaker_name || m.role}: ${(m.content || '').slice(0, 40)}`)
              .join('<br/>');
            preview += '</div>';
          }
        }
        return `<b style="font-size:13px">${day}</b><br/>${lines}${preview}`;
      },
    },
    legend: {
      data: roles.map(r => r.name),
      textStyle: { color: '#8b949e', fontSize: 11 },
      top: 0,
    },
    grid: { left: 10, right: 10, bottom: 10, top: 36, containLabel: true },
    xAxis: {
      type: 'category',
      data: days,
      axisLabel: {
        fontSize: 10,
        color: '#8b949e',
        rotate: days.length > 20 ? 45 : 0,
        formatter: (v) => v.slice(5),
      },
      axisLine: { lineStyle: { color: '#30363d' } },
    },
    yAxis: {
      type: 'value',
      minInterval: 1,
      axisLabel: { fontSize: 10, color: '#8b949e' },
      splitLine: { lineStyle: { color: '#21262d' } },
    },
    series,
  });
}

function renderCluster(entries) {
  const container = $('clusterChart');
  const withEmbedding = entries.filter(e => e.embedding && e.embedding.length >= 3);

  if (!withEmbedding.length) {
    container.innerHTML = '<div style="text-align:center;color:var(--text-3);padding:80px 0">暂无日记嵌入数据，无法渲染语义聚类图</div>';
    return;
  }

  const hasGL = typeof echarts !== 'undefined' && echarts.graphic && container.style;

  if (!hasGL || !window.echartsGLLoaded) {
    renderCluster2D(container, withEmbedding);
    return;
  }

  const keywords = [...new Set(withEmbedding.flatMap(e => e.keywords || []).slice(0, 10))];
  const keywordColorMap = {};
  const palette = ['#58a6ff', '#3fb950', '#a371f7', '#e3b341', '#f78166', '#d2a8ff', '#79c0ff', '#ffa657', '#ff7b72', '#56d4dd'];
  keywords.forEach((k, i) => { keywordColorMap[k] = palette[i % palette.length]; });

  const seriesData = withEmbedding.map(e => {
    const kw = (e.keywords || []).find(k => keywordColorMap[k]) || '';
    return {
      value: [e.embedding[0], e.embedding[1], e.embedding[2]],
      itemStyle: { color: keywordColorMap[kw] || '#8b949e' },
      raw: e,
    };
  });

  const chart = getChart(container);
  if (chart) charts.push(chart);

  setChartOption(container, {
    backgroundColor: 'transparent',
    tooltip: {
      formatter: (p) => {
        const raw = p.data.raw;
        if (!raw) return '';
        return `<b>${raw.summary || ''}</b><br/>${(raw.content || '').slice(0, 120)}`;
      },
    },
    xAxis3D: { type: 'value', axisLine: { lineStyle: { color: '#30363d' } } },
    yAxis3D: { type: 'value', axisLine: { lineStyle: { color: '#30363d' } } },
    zAxis3D: { type: 'value', axisLine: { lineStyle: { color: '#30363d' } } },
    grid3D: {
      boxWidth: 200,
      boxHeight: 150,
      boxDepth: 150,
      viewControl: { autoRotate: true, autoRotateSpeed: 6 },
    },
    series: [{
      type: 'scatter3D',
      data: seriesData,
      symbolSize: 8,
    }],
  });
}

function renderCluster2D(container, entries) {
  const keywords = [...new Set(entries.flatMap(e => e.keywords || []).slice(0, 10))];
  const keywordColorMap = {};
  const palette = ['#58a6ff', '#3fb950', '#a371f7', '#e3b341', '#f78166', '#d2a8ff', '#79c0ff', '#ffa657', '#ff7b72', '#56d4dd'];
  keywords.forEach((k, i) => { keywordColorMap[k] = palette[i % palette.length]; });

  const chart = getChart(container);
  if (chart) charts.push(chart);

  setChartOption(container, {
    backgroundColor: 'transparent',
    tooltip: {
      formatter: (p) => {
        const raw = p.data.raw;
        if (!raw) return '';
        return `<b>${raw.summary || ''}</b><br/>${(raw.content || '').slice(0, 120)}`;
      },
    },
    legend: {
      data: keywords,
      textStyle: { color: '#8b949e', fontSize: 11 },
      top: 0,
      type: 'scroll',
    },
    grid: { left: 10, right: 10, bottom: 10, top: 36, containLabel: true },
    xAxis: {
      type: 'value',
      axisLabel: { fontSize: 10, color: '#8b949e' },
      splitLine: { lineStyle: { color: '#21262d' } },
    },
    yAxis: {
      type: 'value',
      axisLabel: { fontSize: 10, color: '#8b949e' },
      splitLine: { lineStyle: { color: '#21262d' } },
    },
    series: keywords.map(kw => ({
      name: kw,
      type: 'scatter',
      symbolSize: 10,
      itemStyle: { color: keywordColorMap[kw] },
      data: entries.filter(e => (e.keywords || []).includes(kw)).map(e => ({
        value: [e.embedding[0], e.embedding[1]],
        raw: e,
      })),
    })),
  });
}

function renderGraph(data) {
  const container = $('graphChart');
  const userNodes = data.user_nodes || [];
  const topicNodes = data.topic_nodes || [];
  const links = data.user_topic_links || [];

  if (!userNodes.length && !topicNodes.length) {
    container.innerHTML = '<div style="text-align:center;color:var(--text-3);padding:80px 0">暂无用户-话题数据</div>';
    return;
  }

  const topicConnCount = {};
  links.forEach(l => {
    const tId = (l.target || '').replace(/^t_/, '');
    topicConnCount[tId] = (topicConnCount[tId] || 0) + 1;
  });

  const validTopicIds = new Set(Object.entries(topicConnCount).filter(([, c]) => c >= 2).map(([id]) => id));
  const validLinks = links.filter(l => {
    const tId = (l.target || '').replace(/^t_/, '');
    return validTopicIds.has(tId);
  });

  const usedUserIds = new Set(validLinks.map(l => (l.source || '').replace(/^u_/, '')));
  const usedTopicIds = new Set(validLinks.map(l => (l.target || '').replace(/^t_/, '')));

  function userColor(engagement) {
    if (engagement > 0.5) return '#3fb950';
    if (engagement > 0.2) return '#58a6ff';
    if (engagement > 0) return '#e3b341';
    return '#8b949e';
  }

  const nodes = [
    ...userNodes.filter(u => usedUserIds.has(u.user_id || u.id)).map(u => ({
      id: 'u_' + (u.user_id || u.id),
      name: u.name || u.user_id || u.id,
      symbolSize: 12 + (u.engagement || 0) * 20,
      itemStyle: { color: userColor(u.engagement || 0) },
      category: 0,
    })),
    ...topicNodes.filter(t => usedTopicIds.has(t.id || t.topic_id)).map(t => ({
      id: 't_' + (t.id || t.topic_id),
      name: t.name || t.topic || t.id,
      symbolSize: 14,
      itemStyle: { color: '#a371f7' },
      category: 1,
    })),
  ];

  const graphLinks = validLinks.map(l => ({
    source: l.source,
    target: l.target,
    lineStyle: { color: '#30363d', width: 1 },
  }));

  const chart = getChart(container);
  if (chart) charts.push(chart);

  setChartOption(container, {
    backgroundColor: 'transparent',
    tooltip: {
      formatter: (p) => {
        if (p.dataType === 'node') {
          return `<b>${p.name}</b><br/>类别: ${p.category === 0 ? '用户' : '话题'}`;
        }
        return '';
      },
    },
    legend: {
      data: ['用户', '话题'],
      textStyle: { color: '#8b949e', fontSize: 11 },
      top: 0,
    },
    series: [{
      type: 'graph',
      layout: 'force',
      roam: true,
      draggable: true,
      force: { repulsion: 120, edgeLength: [60, 160], gravity: 0.1 },
      categories: [
        { name: '用户' },
        { name: '话题' },
      ],
      data: nodes,
      links: graphLinks,
      label: { show: true, fontSize: 11, color: '#c9d1d9' },
      lineStyle: { opacity: 0.4, curveness: 0.1 },
      emphasis: { focus: 'adjacency', lineStyle: { width: 3 } },
    }],
  });
}
