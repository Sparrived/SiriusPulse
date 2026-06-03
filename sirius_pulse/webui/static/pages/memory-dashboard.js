import { store } from '../store.js';
import { get } from '../app.js';
import { toast, $, animateNumber } from '../components.js';
import { setChartOption, getChart } from '../charts.js';
import { renderNeuralNav, removeNeuralNav, makeClickableStat, makeClickableTopic } from './memory-nav.js';

let cachedData = null;

export async function init(container) {
  const name = store.currentPersona;
  if (!name) {
    container.innerHTML = '<div class="bio-empty">请先选择人格</div>';
    return;
  }

  renderNeuralNav('memory-dashboard');
  const btn = $('bioRefreshBtn');
  if (btn) btn.addEventListener('click', () => loadDashboard());
  await loadDashboard();
}

async function loadDashboard() {
  const name = store.currentPersona;
  if (!name) return;
  try {
    const data = await get(`/personas/${name}/memory/dashboard`);
    cachedData = data;
    if (!data.has_data) {
      const bioStats = $('bioStats');
      if (bioStats) bioStats.innerHTML = '<div class="bio-empty" style="grid-column:1/-1">暂无记忆系统数据，请先启动人格并进行对话</div>';
      return;
    }
    renderStats(data);
    renderConfidenceChart(data.confidence_distribution || {});
    renderPredicateChart(data.top_predicates || []);
    renderTopics(data.top_topics || []);
    renderLayers(data);
  } catch (e) {
    toast('加载记忆仪表盘失败: ' + e.message, 'error');
  }
}

function renderStats(data) {
  const evo = data.evolution_stats || {};
  const sit = data.situation_stats || {};
  const diary = data.diary_stats || {};
  const prov = data.provenance_stats || {};

  animateNumber($('statEvolution'), evo.active_records || 0);
  const evoSub = $('statEvolutionSub');
  if (evoSub) evoSub.textContent = `${evo.active_records || 0} 活跃 / ${evo.total_records || 0} 总计`;

  animateNumber($('statSituation'), sit.today_count || 0);
  const sitSub = $('statSituationSub');
  if (sitSub) sitSub.textContent = `今日 ${sit.today_count || 0} / ${sit.total_situations || 0} 总计`;

  animateNumber($('statDiary'), diary.total_entries || 0);
  animateNumber($('statSlices'), diary.total_slices || 0);
  animateNumber($('statUsers'), data.user_count || 0);
  animateNumber($('statClaims'), prov.total_claims || 0);
  animateNumber($('statEvidence'), prov.total_evidence || 0);

  const statCards = document.querySelectorAll('.bio-stat-card');
  if (statCards[0]) makeClickableStat(statCards[0], 'evolution-chain');
  if (statCards[1]) makeClickableStat(statCards[1], 'situation-timeline');
  if (statCards[2]) makeClickableStat(statCards[2], 'diary-slices');
  if (statCards[3]) makeClickableStat(statCards[3], 'diary-slices');
  if (statCards[4]) makeClickableStat(statCards[4], 'biography-view');
  if (statCards[5]) makeClickableStat(statCards[5], 'memory-claims');
  if (statCards[6]) makeClickableStat(statCards[6], 'memory-claims');
}

function renderConfidenceChart(dist) {
  const container = $('bioConfChart');
  if (!container) return;

  const labels = Object.keys(dist);
  const values = Object.values(dist);
  if (!labels.length) {
    container.innerHTML = '<div class="bio-empty">暂无置信度数据</div>';
    return;
  }

  const chart = getChart(container);
  setChartOption(container, {
    backgroundColor: 'transparent',
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    grid: { left: 10, right: 30, top: 10, bottom: 10, containLabel: true },
    xAxis: {
      type: 'value',
      axisLabel: { fontSize: 10, color: '#8b949e' },
      splitLine: { lineStyle: { color: '#21262d' } },
    },
    yAxis: {
      type: 'category',
      data: labels,
      axisLabel: { fontSize: 11, color: '#8b949e' },
      axisLine: { lineStyle: { color: '#30363d' } },
    },
    series: [{
      type: 'bar',
      data: values,
      barMaxWidth: 20,
      itemStyle: {
        borderRadius: [0, 4, 4, 0],
        color: {
          type: 'linear',
          x: 0, y: 0, x2: 1, y2: 0,
          colorStops: [
            { offset: 0, color: '#00ffc8' },
            { offset: 1, color: '#00e5ff' },
          ],
        },
      },
    }],
  });
}

function renderPredicateChart(predicates) {
  const container = $('bioPredChart');
  if (!container) return;

  if (!predicates.length) {
    container.innerHTML = '<div class="bio-empty">暂无谓语数据</div>';
    return;
  }

  const top10 = predicates.slice(0, 10);
  const palette = ['#00ffc8', '#00e5ff', '#7b61ff', '#ff6b35', '#ffd23e', '#3fb950', '#f78166', '#58a6ff', '#d2a8ff', '#56d4dd'];

  const chart = getChart(container);
  setChartOption(container, {
    backgroundColor: 'transparent',
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    grid: { left: 10, right: 10, top: 10, bottom: 10, containLabel: true },
    xAxis: {
      type: 'category',
      data: top10.map(p => p[0]),
      axisLabel: { fontSize: 10, color: '#8b949e', rotate: 30 },
      axisLine: { lineStyle: { color: '#30363d' } },
    },
    yAxis: {
      type: 'value',
      minInterval: 1,
      axisLabel: { fontSize: 10, color: '#8b949e' },
      splitLine: { lineStyle: { color: '#21262d' } },
    },
    series: [{
      type: 'bar',
      data: top10.map((p, i) => ({
        value: p[1],
        itemStyle: { color: palette[i % palette.length], borderRadius: [4, 4, 0, 0] },
      })),
      barMaxWidth: 28,
    }],
  });
}

function renderTopics(topics) {
  const container = $('bioTopics');
  if (!container) return;

  if (!topics.length) {
    container.innerHTML = '<div class="bio-empty" style="padding:20px">暂无话题数据</div>';
    return;
  }

  container.innerHTML = topics.map(([topic, count]) =>
    `<div class="bio-topic-pill" data-topic="${topic}"><span>${topic}</span><span class="bio-topic-count">×${count}</span></div>`
  ).join('');

  const pills = container.querySelectorAll('.bio-topic-pill');
  pills.forEach((pill, i) => {
    setTimeout(() => pill.classList.add('visible'), i * 60);
    makeClickableTopic(pill, pill.dataset.topic);
  });
}

function renderLayers(data) {
  const evo = data.evolution_stats || {};
  const sit = data.situation_stats || {};
  const diary = data.diary_stats || {};
  const prov = data.provenance_stats || {};

  const layers = [
    { name: 'Layer 0', label: '感知层', status: evo.total_records > 0 ? 'ok' : 'idle' },
    { name: 'Ledger', label: '证据账本', status: prov.total_claims > 0 ? 'ok' : 'idle' },
    { name: 'Layer 1', label: '回复层', status: diary.total_entries > 0 ? 'ok' : 'idle' },
    { name: 'Layer 2', label: '暂冷压缩', status: sit.total_situations > 0 ? 'ok' : 'idle' },
    { name: 'Layer 3', label: '冷寂总结', status: diary.total_entries > 0 ? 'ok' : 'idle' },
    { name: 'Layer 4', label: '后台精炼', status: evo.superseded_records > 0 ? 'ok' : 'idle' },
  ];

  const bioLayers = $('bioLayers');
  if (bioLayers) bioLayers.innerHTML = layers.map(l =>
    `<div class="bio-layer"><div class="bio-layer-dot ${l.status}"></div><div class="bio-layer-name">${l.name}</div><div class="bio-layer-label">${l.label}</div></div>`
  ).join('');
}
