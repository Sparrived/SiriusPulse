const instances = new Map();

export function getChart(container) {
  if (!container || typeof echarts === 'undefined') return null;
  let chart = instances.get(container);
  if (!chart) {
    chart = echarts.init(container, null, { renderer: 'canvas' });
    instances.set(container, chart);
    const ro = new ResizeObserver(() => {
      if (container.offsetWidth > 0 && container.offsetHeight > 0) chart.resize();
    });
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
  let sortedLabels = labels;
  let sortedData = data;
  if (horizontal && labels.length > 1) {
    const indices = labels.map((_, i) => i);
    indices.sort((a, b) => {
      const sumA = data.reduce((s, srs) => s + (srs.values[a] || 0), 0);
      const sumB = data.reduce((s, srs) => s + (srs.values[b] || 0), 0);
      return sumA - sumB;
    });
    sortedLabels = indices.map(i => labels[i]);
    sortedData = data.map(s => ({ ...s, values: indices.map(i => s.values[i]) }));
  }
  const series = sortedData.map((s, i) => ({
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
    legend: sortedData.length > 1 ? { data: sortedData.map(s => s.name), textStyle: { color: '#8b949e', fontSize: 11 }, top: 0 } : undefined,
    grid: { left: 10, right: 10, bottom: 10, top: sortedData.length > 1 ? 32 : 10, containLabel: true },
    xAxis: { type: horizontal ? 'value' : 'category', data: horizontal ? undefined : sortedLabels, axisLabel: { fontSize: 10, color: '#8b949e' }, splitLine: { lineStyle: { color: '#21262d' } }, axisLine: { lineStyle: { color: '#30363d' } } },
    yAxis: { type: horizontal ? 'category' : 'value', data: horizontal ? sortedLabels : undefined, axisLabel: { fontSize: 10, color: '#8b949e' }, splitLine: { lineStyle: { color: '#21262d' } }, axisLine: { lineStyle: { color: '#30363d' } } },
    series,
  });
}

export function renderLineChart(container, { labels, series, areaStyle = true, dualAxis = false, colors }) {
  if (!container) return;
  const yAxis = dualAxis
    ? [
        { type: 'value', position: 'left', axisLabel: { fontSize: 10, color: '#8b949e' }, splitLine: { lineStyle: { color: '#21262d' } } },
        { type: 'value', position: 'right', axisLabel: { fontSize: 10, color: '#8b949e' }, splitLine: { show: false } },
      ]
    : { type: 'value', axisLabel: { fontSize: 10, color: '#8b949e' }, splitLine: { lineStyle: { color: '#21262d' } } };
  const gridRight = dualAxis ? 48 : 10;
  setChartOption(container, {
    backgroundColor: 'transparent',
    tooltip: { trigger: 'axis' },
    legend: { data: series.map(s => s.name), textStyle: { color: '#8b949e', fontSize: 11 }, top: 0 },
    grid: { left: 10, right: gridRight, bottom: 10, top: 32, containLabel: true },
    xAxis: { type: 'category', data: labels, boundaryGap: false, axisLabel: { fontSize: 10, color: '#8b949e', rotate: 30 }, axisLine: { lineStyle: { color: '#30363d' } } },
    yAxis,
    series: series.map((s, i) => ({
      ...s,
      type: 'line',
      smooth: true,
      showSymbol: false,
      yAxisIndex: dualAxis ? i : 0,
      lineStyle: { width: 2, color: colors?.[i], ...s.lineStyle },
      itemStyle: colors?.[i] ? { color: colors[i] } : undefined,
      areaStyle: areaStyle ? { opacity: 0.12 } : undefined,
    })),
  });
}

export function renderPieChart(container, { data, center = ['35%', '50%'], radius = ['35%', '60%'] }) {
  if (!container) return;
  setChartOption(container, {
    backgroundColor: 'transparent',
    tooltip: { trigger: 'item', formatter: p => `<b>${p.name}</b><br/>${p.value.toLocaleString()} (${p.percent}%)` },
    legend: {
      orient: 'vertical',
      right: 10,
      top: 'center',
      textStyle: { fontSize: 11, color: '#c9d1d9' },
      itemWidth: 10,
      itemHeight: 10,
      formatter: name => {
        const item = data.find(d => d.name === name);
        return item ? `${name}  ${item.value.toLocaleString()}` : name;
      },
    },
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

// 模块名称映射（英文 key → 中文标签）
const SECTION_LABELS = {
  persona: '人格设定', identity: '身份识别', output_constraint: '输出约束',
  emotion: '情感上下文', empathy: '共情策略', relationship: '互动指导',
  memory: '记忆引用', interests: '用户兴趣', group_style: '群体风格',
  participants: '近期参与者', cross_group: '跨群认知', skills: '可用技能',
  glossary: '名词解释', output_format: '输出格式', diary: '日记记忆',
  history_xml: '对话历史', cross_group_xml: '跨群历史',
  system_prompt_total: '系统指令', user_message: '用户消息',
};

// 桑基图大类分组
const SECTION_GROUPS = [
  { name: '人格与身份', keys: ['persona', 'identity'], color: '#58a6ff' },
  { name: '情感与关系', keys: ['emotion', 'empathy', 'relationship'], color: '#3fb950' },
  { name: '记忆与历史', keys: ['memory', 'diary', 'history_xml', 'cross_group_xml'], color: '#d29922' },
  { name: '环境与风格', keys: ['group_style', 'participants', 'cross_group', 'interests'], color: '#f85149' },
  { name: '功能与格式', keys: ['skills', 'glossary', 'output_format', 'output_constraint'], color: '#a371f7' },
  { name: '输入组成', keys: ['system_prompt_total', 'user_message'], color: '#e3b341' },
];

// 任务标签映射
const SANKEY_TASK_LABELS = {
  response_generate: '主模型调用',
  cognition_analyze: '认知分析',
  diary_generate: '日记生成',
  diary_consolidate: '日记合并',
  persona_generate: '人格生成',
};

const TASK_COLORS = ['#58a6ff', '#3fb950', '#d29922', '#f85149', '#a371f7', '#e3b341'];

/**
 * 渲染模块 Token 分布桑基图
 * @param {HTMLElement} container - 图表容器
 * @param {Object} breakdown - 按模块分类的 token 数据 {persona: 100, identity: 50, ...}
 * @param {Object} [breakdownByTask] - 按任务×模块的交叉数据 {response_generate: {persona: 80, ...}, ...}
 */
export function renderSankeyChart(container, breakdown, breakdownByTask) {
  if (!container) return;
  const rawEntries = Object.entries(breakdown).filter(([k]) => k !== 'total');

  if (!rawEntries.length || typeof echarts === 'undefined') {
    disposeChart(container);
    container.innerHTML = '<div style="color:var(--text-2);padding:12px">暂无模块分布数据</div>';
    return;
  }

  const nodes = [{ name: '总输入', itemStyle: { color: '#ffffff' } }];
  const links = [];
  const hasTaskBreakdown = breakdownByTask && Object.keys(breakdownByTask).length > 1;

  if (hasTaskBreakdown) {
    // 4层桑基图：总输入 → 任务 → 大类 → 子模块
    Object.keys(breakdownByTask).forEach((taskName, ti) => {
      const taskLabel = SANKEY_TASK_LABELS[taskName] || taskName;
      const taskColor = TASK_COLORS[ti % TASK_COLORS.length];
      const taskData = breakdownByTask[taskName];
      let taskSum = 0;

      SECTION_GROUPS.forEach((g) => {
        let groupSum = 0;
        g.keys.forEach((key) => {
          const val = taskData[key] || 0;
          if (val) {
            nodes.push({ name: SECTION_LABELS[key] || key, itemStyle: { color: g.color } });
            links.push({ source: g.name, target: SECTION_LABELS[key] || key, value: val });
            groupSum += val;
          }
        });
        if (groupSum) {
          nodes.push({ name: g.name, itemStyle: { color: g.color } });
          links.push({ source: taskLabel, target: g.name, value: groupSum });
          taskSum += groupSum;
        }
      });

      if (taskSum) {
        nodes.push({ name: taskLabel, itemStyle: { color: taskColor } });
        links.push({ source: '总输入', target: taskLabel, value: taskSum });
      }
    });
  } else {
    // 3层桑基图（聚合视图）：总输入 → 大类 → 子模块
    SECTION_GROUPS.forEach((g) => {
      let groupSum = 0;
      g.keys.forEach((key) => {
        const val = breakdown[key] || 0;
        if (val) {
          nodes.push({ name: SECTION_LABELS[key] || key, itemStyle: { color: g.color } });
          links.push({ source: g.name, target: SECTION_LABELS[key] || key, value: val });
          groupSum += val;
        }
      });
      if (groupSum) {
        nodes.push({ name: g.name, itemStyle: { color: g.color } });
        links.push({ source: '总输入', target: g.name, value: groupSum });
      }
    });
  }

  if (!links.length) {
    disposeChart(container);
    container.innerHTML = '<div style="color:var(--text-2);padding:12px">暂无模块分布数据</div>';
    return;
  }

  // 去重节点：同名节点只保留一个（ECharts 按 name 聚合）
  const nodeMap = new Map();
  nodes.forEach((n) => { if (!nodeMap.has(n.name)) nodeMap.set(n.name, n); });
  const uniqueNodes = Array.from(nodeMap.values());

  // 桑基图数据结构变化大，每次都重建实例避免增量更新内部状态错乱
  disposeChart(container);
  const chart = echarts.init(container, 'dark');
  instances.set(container, chart);
  const ro = new ResizeObserver(() => {
    if (container.offsetWidth > 0 && container.offsetHeight > 0) chart.resize();
  });
  ro.observe(container);
  container._resizeObserver = ro;

  chart.setOption({
    backgroundColor: 'transparent',
    tooltip: {
      trigger: 'item',
      formatter: (params) => {
        if (params.dataType === 'edge') {
          return `${params.data.source} → ${params.data.target}<br/><b>${params.data.value.toLocaleString()} tokens</b>`;
        }
        const nodeValue = params.value ?? 0;
        return `<b>${params.name}</b><br/>总计: ${nodeValue.toLocaleString()} tokens`;
      },
    },
    series: [{
      type: 'sankey',
      layout: 'none',
      emphasis: { focus: 'adjacency' },
      data: uniqueNodes,
      links: links,
      top: 10, bottom: 10, left: 10, right: hasTaskBreakdown ? 140 : 110,
      nodeWidth: hasTaskBreakdown ? 22 : 28,
      nodeGap: 10,
      layoutIterations: 32,
      lineStyle: { color: 'gradient', curveness: 0.5, opacity: 0.55 },
      label: {
        color: '#e8eaf0',
        fontSize: 11,
        formatter: (p) => p.name,
      },
      itemStyle: { borderWidth: 1, borderColor: '#0d1117' },
    }],
  });
}
