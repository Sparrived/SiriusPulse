const instances = new Map();

export function getChart(container) {
  if (!container || typeof echarts === 'undefined') return null;
  let chart = instances.get(container);
  if (!chart) {
    chart = echarts.init(container, null, { renderer: 'canvas' });
    instances.set(container, chart);
    const ro = new ResizeObserver(() => chart.resize());
    ro.observe(container);
    container._resizeObserver = ro;
  }
  return chart;
}

export function disposeChart(container) {
  const chart = instances.get(container);
  if (chart) {
    chart.dispose();
    container._resizeObserver?.disconnect();
    instances.delete(container);
  }
}

export function setChartOption(container, option, notMerge = true) {
  const chart = getChart(container);
  if (chart) chart.setOption(option, notMerge);
  return chart;
}

export function renderBarChart(container, { labels, data, colors, horizontal = false, stacked = false }) {
  if (!container) return;
  const series = data.map((s, i) => ({
    name: s.name,
    type: 'bar',
    data: s.values,
    barWidth: horizontal ? 10 : '60%',
    itemStyle: { color: colors?.[i] || undefined, borderRadius: horizontal ? [0, 3, 3, 0] : [3, 3, 0, 0] },
    stack: stacked ? 'total' : undefined,
  }));
  setChartOption(container, {
    backgroundColor: 'transparent',
    tooltip: { trigger: 'axis', axisPointer: { type: 'shadow' } },
    legend: data.length > 1 ? { data: data.map(s => s.name), textStyle: { color: '#8b949e', fontSize: 11 }, top: 0 } : undefined,
    grid: { left: 10, right: 10, bottom: 10, top: data.length > 1 ? 32 : 10, containLabel: true },
    xAxis: { type: horizontal ? 'value' : 'category', data: horizontal ? undefined : labels, axisLabel: { fontSize: 10, color: '#8b949e' }, splitLine: { lineStyle: { color: '#21262d' } }, axisLine: { lineStyle: { color: '#30363d' } } },
    yAxis: { type: horizontal ? 'category' : 'value', data: horizontal ? labels : undefined, axisLabel: { fontSize: 10, color: '#8b949e' }, splitLine: { lineStyle: { color: '#21262d' } }, axisLine: { lineStyle: { color: '#30363d' } } },
    series,
  });
}

export function renderLineChart(container, { labels, series, areaStyle = true }) {
  if (!container) return;
  setChartOption(container, {
    backgroundColor: 'transparent',
    tooltip: { trigger: 'axis' },
    legend: { data: series.map(s => s.name), textStyle: { color: '#8b949e', fontSize: 11 }, top: 0 },
    grid: { left: 10, right: 10, bottom: 10, top: 32, containLabel: true },
    xAxis: { type: 'category', data: labels, boundaryGap: false, axisLabel: { fontSize: 10, color: '#8b949e', rotate: 30 }, axisLine: { lineStyle: { color: '#30363d' } } },
    yAxis: { type: 'value', axisLabel: { fontSize: 10, color: '#8b949e' }, splitLine: { lineStyle: { color: '#21262d' } } },
    series: series.map(s => ({ ...s, type: 'line', smooth: true, showSymbol: false, lineStyle: { width: 2, ...s.lineStyle }, areaStyle: areaStyle ? { opacity: 0.12 } : undefined })),
  });
}

export function renderPieChart(container, { data, center = ['40%', '50%'], radius = ['40%', '70%'] }) {
  if (!container) return;
  setChartOption(container, {
    backgroundColor: 'transparent',
    tooltip: { trigger: 'item', formatter: p => `<b>${p.name}</b><br/>${p.percent}%` },
    legend: { orient: 'vertical', right: 10, top: 'center', textStyle: { fontSize: 12, color: '#c9d1d9' }, itemWidth: 12, itemHeight: 12 },
    series: [{ type: 'pie', radius, center, avoidLabelOverlap: true, itemStyle: { borderRadius: 6, borderColor: '#0d1117', borderWidth: 2 }, label: { show: false }, emphasis: { label: { show: true, fontSize: 14, fontWeight: 'bold' } }, data }],
  });
}

export function renderRadarChart(container, { indicators, data, color = '#4c9aff' }) {
  if (!container) return;
  setChartOption(container, {
    backgroundColor: 'transparent',
    tooltip: { trigger: 'item' },
    radar: { indicator: indicators, radius: '65%', axisName: { color: '#8b949e', fontSize: 11 }, splitArea: { areaStyle: { color: ['rgba(76,154,255,0.02)', 'rgba(76,154,255,0.06)'] } }, axisLine: { lineStyle: { color: 'rgba(139,148,158,0.2)' } }, splitLine: { lineStyle: { color: 'rgba(139,148,158,0.15)' } } },
    series: [{ type: 'radar', data: [{ value: data, areaStyle: { color: color + '33' }, lineStyle: { color, width: 2 }, itemStyle: { color } }] }],
  });
}
