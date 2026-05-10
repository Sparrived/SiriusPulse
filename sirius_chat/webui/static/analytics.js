// ── Token Tracker ─────────────────────────────────────
let _ttState = { range: 'all', page: 0, data: null };
let _ttAbort = null;

const _TASK_LABELS = {
  response_generate: '主模型调用',
  cognition_analyze: '认知分析',
  diary_generate: '日记生成',
  diary_consolidate: '日记合并',
  proactive_generate: '主动/提醒生成',
  persona_generate: '人格生成',
  sticker_preference_generate: '表情包偏好生成',
  sticker_tag_extract: '表情包标签提取',
};

function _translateTaskLabel(name) {
  return _TASK_LABELS[name] || name;
}

async function loadTokenTracker() {
  renderPersonaSelect();
  if (!currentPersona && personas.length > 0) {
    selectPersona(personas[0].name);
  }
  // Highlight active range button
  document.querySelectorAll('#page-token-tracker .btn[data-range]').forEach((b) => {
    b.classList.toggle('active', b.dataset.range === _ttState.range);
  });
  // Reset to overview tab on page load
  ttSwitchTab('overview');
  await ttLoadData();
}

function ttSetRange(range) {
  _ttState.range = range;
  _ttState.page = 0;
  document.querySelectorAll('#page-token-tracker .btn[data-range]').forEach((b) => {
    b.classList.toggle('active', b.dataset.range === range);
  });
  ttLoadData();
}

function ttChangePage(delta) {
  const records = (_ttState.data?.recent_with_breakdown || []);
  const maxPage = Math.max(0, Math.ceil(records.length / 10) - 1);
  _ttState.page = Math.max(0, Math.min(maxPage, _ttState.page + delta));
  ttRenderRecentTable();
}

async function ttLoadData() {
  if (!currentPersona) return;
  const name = currentPersona;

  // Cancel previous pending request
  if (_ttAbort) {
    _ttAbort.abort();
    _ttAbort = null;
  }
  const controller = new AbortController();
  _ttAbort = controller;

  // Compute time range
  let start = null, end = null;
  const now = Date.now() / 1000;
  if (_ttState.range === 'today') {
    const d = new Date();
    d.setHours(0, 0, 0, 0);
    start = d.getTime() / 1000;
    end = now;
  } else if (_ttState.range === '7d') {
    start = now - 7 * 86400;
    end = now;
  } else if (_ttState.range === '30d') {
    start = now - 30 * 86400;
    end = now;
  }

  const qs = start ? `?start=${Math.floor(start)}&end=${Math.floor(end)}` : '';
  try {
    const res = await get(`/personas/${name}/tokens${qs}`, controller.signal);
    if (_ttAbort !== controller) return; // stale request
    _ttState.data = res;

    // Summary stats
    const summary = res.summary || {};
    const statEls = document.querySelectorAll('#ttSummaryStats .stat-card .value');
    if (statEls.length >= 4) {
      animateNumber(statEls[0], summary.total_calls || 0, 500);
      animateNumber(statEls[1], summary.total_prompt_tokens || 0, 500);
      animateNumber(statEls[2], summary.total_completion_tokens || 0, 500);
      animateNumber(statEls[3], summary.total_tokens || 0, 500);
    }
    const avg = res.response_avg || {};
    const avgEl = $('ttAvgRound');
    const avgDetailEl = $('ttAvgRoundDetail');
    if (avgEl) animateNumber(avgEl, avg.avg_total_tokens || 0, 500);
    if (avgDetailEl) {
      const calls = avg.total_calls || 0;
      avgDetailEl.textContent = calls ? `${calls} 次回复 · ${(avg.avg_prompt_tokens || 0).toLocaleString()} + ${(avg.avg_completion_tokens || 0).toLocaleString()}` : '暂无回复记录';
    }

    // Render charts for the currently active tab only
    ttRenderActiveTab();
  } catch (e) {
    if (e.name === 'AbortError') return;
    console.error('ttLoadData', e);
    const els = ['ttTimeSeries', 'ttActiveHours', 'ttSectionBreakdown', 'ttTaskHierarchy', 'ttByModel', 'ttByGroup', 'ttByProvider', 'ttByTask'];
    els.forEach((id) => { const el = $(id); if (el) el.textContent = '加载失败'; });
  } finally {
    if (_ttAbort === controller) _ttAbort = null;
  }
}

// ── Tab switching ──────────────────────────────────────
let _ttActiveTab = 'overview';

function ttSwitchTab(tab) {
  _ttActiveTab = tab;
  document.querySelectorAll('#ttTabBar .tab-btn').forEach((b) => {
    b.classList.toggle('active', b.dataset.tab === tab);
  });
  document.querySelectorAll('#page-token-tracker .tab-panel').forEach((p) => {
    p.classList.toggle('active', p.dataset.tab === tab);
  });
  // Defer chart rendering so the container has layout
  requestAnimationFrame(() => ttRenderActiveTab());
}

function ttRenderActiveTab() {
  const res = _ttState.data;
  if (!res) return;
  switch (_ttActiveTab) {
    case 'overview':
      renderTimeSeries($('ttTimeSeries'), res.hourly || []);
      renderActiveHours($('ttActiveHours'), res.hourly_distribution || []);
      _renderExtraStats('tt', res);
      break;
    case 'module':
      renderSectionBars($('ttSectionBreakdown'), res.section_breakdown || {}, res.section_breakdown_by_task || {});
      renderTaskHierarchy('ttTaskHierarchy', res.by_task || []);
      break;
    case 'dimension':
      ttRenderDimensionList('ttByModel', res.by_model || []);
      ttRenderDimensionList('ttByGroup', res.by_group || []);
      ttRenderDimensionList('ttByProvider', res.by_provider || []);
      ttRenderDimensionList('ttByTask', res.by_task || []);
      break;
    case 'detail':
      ttRenderRecentTable();
      break;
  }
}

function ttRenderDimensionList(containerId, items) {
  const el = $(containerId);
  if (!el) return;
  let chart = echarts.getInstanceByDom(el);
  if (!items.length || typeof echarts === 'undefined') {
    if (chart) { chart.dispose(); window.removeEventListener('resize', el._barResize); }
    if (!items.length) {
      el.innerHTML = '<div style="color:var(--text-2)">暂无数据</div>';
    } else {
      // Fallback to text list if ECharts not loaded
      el.innerHTML = items.slice(0, 8).map((it) => `
        <div style="display:flex;justify-content:space-between;align-items:center;padding:6px 0;border-bottom:1px solid var(--border);font-size:13px">
          <span style="color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:140px" title="${it.name}">${it.name}</span>
          <span style="color:var(--text-2);font-family:ui-monospace,monospace">${it.total_tokens || 0}</span>
        </div>
      `).join('');
    }
    return;
  }

  const sorted = [...items].sort((a, b) => (b.total_tokens || 0) - (a.total_tokens || 0)).slice(0, 10);
  const data = sorted.map((it) => ({
    value: it.total_tokens || 0,
    name: _translateTaskLabel(it.name || '未知'),
    calls: it.calls || 0,
    prompt: it.prompt_tokens || 0,
    completion: it.completion_tokens || 0,
  }));

  if (!chart) {
    chart = echarts.init(el, 'dark');
    const onResize = () => chart.resize();
    window.removeEventListener('resize', el._barResize);
    window.addEventListener('resize', onResize);
    el._barResize = onResize;
  }

  const names = data.map((d) => d.name).reverse();
  const promptData = data.map((d) => d.prompt).reverse();
  const completionData = data.map((d) => d.completion).reverse();

  chart.setOption({
    backgroundColor: 'transparent',
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'shadow' },
      formatter: (params) => {
        const d0 = params[0] && params[0].data;
        const name = d0 ? d0.name : '';
        const promptVal = params.find((p) => p.seriesName === 'Prompt')?.value || 0;
        const completionVal = params.find((p) => p.seriesName === 'Completion')?.value || 0;
        const total = promptVal + completionVal;
        const calls = d0 ? d0.calls : 0;
        return `<b>${name}</b><br/>总 Tokens: <b>${total.toLocaleString()}</b><br/>调用: ${calls} 次<br/>Prompt: ${promptVal.toLocaleString()}<br/>Completion: ${completionVal.toLocaleString()}`;
      },
    },
    legend: { data: ['Prompt', 'Completion'], textStyle: { color: '#c9d1d9', fontSize: 11 }, top: 0 },
    grid: { top: 28, bottom: 8, left: 8, right: 48, containLabel: true },
    xAxis: { type: 'value', axisLabel: { fontSize: 11, color: '#8b949e' }, splitLine: { lineStyle: { color: '#30363d' } } },
    yAxis: {
      type: 'category',
      data: names,
      axisLabel: { fontSize: 11, color: '#c9d1d9' },
      axisLine: { show: false },
      axisTick: { show: false },
    },
    series: [
      {
        name: 'Prompt',
        type: 'bar',
        data: promptData.map((v, i) => ({ value: v, name: names[i], calls: data[i].calls })),
        barWidth: 10,
        itemStyle: { color: '#58a6ff', borderRadius: [2, 2, 2, 2] },
      },
      {
        name: 'Completion',
        type: 'bar',
        data: completionData.map((v, i) => ({ value: v, name: names[i], calls: data[i].calls })),
        barWidth: 10,
        itemStyle: { color: '#3fb950', borderRadius: [2, 2, 2, 2] },
        label: {
          show: true,
          position: 'right',
          fontSize: 11,
          color: '#c9d1d9',
          formatter: (p) => {
            const idx = p.dataIndex;
            const total = (promptData[idx] || 0) + (completionData[idx] || 0);
            return total.toLocaleString();
          },
        },
      },
    ],
  }, true);
}

function renderTaskHierarchy(containerId, items) {
  const el = $(containerId);
  if (!el) return;
  let chart = echarts.getInstanceByDom(el);
  if (!items.length || typeof echarts === 'undefined') {
    if (chart) { chart.dispose(); window.removeEventListener('resize', el._donutResize); }
    el.innerHTML = '<div style="color:var(--text-2)">暂无数据</div>';
    return;
  }

  const colors = ['#58a6ff', '#3fb950', '#d29922', '#f85149', '#a371f7', '#e3b341'];
  const data = items.map((it, i) => ({
    value: it.total_tokens || 0,
    name: _translateTaskLabel(it.name),
    calls: it.calls || 0,
    prompt: it.prompt_tokens || 0,
    completion: it.completion_tokens || 0,
    itemStyle: { color: colors[i % colors.length] },
  }));

  if (!chart) {
    chart = echarts.init(el, 'dark');
    const onResize = () => chart.resize();
    window.removeEventListener('resize', el._donutResize);
    window.addEventListener('resize', onResize);
    el._donutResize = onResize;
  }

  chart.setOption({
    backgroundColor: 'transparent',
    tooltip: {
      trigger: 'item',
      formatter: (p) => {
        const d = p.data;
        return `<b>${d.name}</b><br/>占比: <b>${p.percent}%</b><br/>Tokens: ${d.value.toLocaleString()}<br/>调用: ${d.calls} 次<br/>Prompt: ${d.prompt.toLocaleString()}<br/>Completion: ${d.completion.toLocaleString()}`;
      },
    },
    legend: {
      orient: 'vertical',
      right: 10,
      top: 'center',
      textStyle: { fontSize: 12, color: '#c9d1d9' },
      itemWidth: 12,
      itemHeight: 12,
    },
    series: [{
      type: 'pie',
      radius: ['40%', '70%'],
      center: ['40%', '50%'],
      avoidLabelOverlap: true,
      itemStyle: { borderRadius: 6, borderColor: '#0d1117', borderWidth: 2 },
      label: { show: false },
      emphasis: {
        label: { show: true, fontSize: 14, fontWeight: 'bold', color: '#e8eaf0' },
        itemStyle: { shadowBlur: 10, shadowOffsetX: 0, shadowColor: 'rgba(0,0,0,0.5)' },
      },
      data,
    }],
  }, true);
}

function renderCognitionEvents(events) {
  const el = $('cogEvents');
  if (!el) return;
  if (!events.length) {
    el.innerHTML = '<div style="color:var(--text-2);padding:12px">暂无认知事件数据</div>';
    return;
  }
  const rows = events.slice(0, 30).map((e) => {
    const ts = e.timestamp ? new Date(e.timestamp * 1000).toLocaleString('zh-CN', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '-';
    const emotionBadge = _emotionCn ? _emotionCn(e.basic_emotion) : (e.basic_emotion || '未知');
    const dir = (e.directed_score || 0).toFixed(2);
    const sar = (e.sarcasm_score || 0).toFixed(2);
    const ent = (e.entitlement_score || 0).toFixed(2);
    const gap = (e.turn_gap_readiness || 0).toFixed(2);
    const intent = e.social_intent || '-';
    const urgency = (e.urgency_score || 0).toFixed(0);
    const relevance = (e.relevance_score || 0).toFixed(2);
    const sig = e.directed_signals || {};
    const sigHtml = Object.entries(sig).slice(0, 4).map(([k, v]) => `<span style="font-size:10px;color:var(--text-3);background:var(--bg-2);border-radius:4px;padding:1px 5px;margin-right:4px">${k.replace('_score','')} ${(v*100).toFixed(0)}%</span>`).join('');
    return `
      <tr style="border-bottom:1px solid var(--border)">
        <td style="padding:8px 10px;font-size:12px;color:var(--text-2);white-space:nowrap">${ts}</td>
        <td style="padding:8px 10px;font-size:12px;color:var(--text)">${e.user_id || '-'}</td>
        <td style="padding:8px 10px;font-size:12px;color:var(--accent)">${emotionBadge}</td>
        <td style="padding:8px 10px;font-size:12px;color:var(--text);text-align:center">${dir}</td>
        <td style="padding:8px 10px;font-size:12px;color:var(--text);text-align:center">${sar}</td>
        <td style="padding:8px 10px;font-size:12px;color:var(--text);text-align:center">${ent}</td>
        <td style="padding:8px 10px;font-size:12px;color:var(--text);text-align:center">${gap}</td>
        <td style="padding:8px 10px;font-size:12px;color:var(--text-2)">${intent}</td>
        <td style="padding:8px 10px;font-size:12px;color:var(--text);text-align:center">${urgency}</td>
        <td style="padding:8px 10px;font-size:12px;color:var(--text);text-align:center">${relevance}</td>
        <td style="padding:8px 10px;font-size:12px">${sigHtml}</td>
      </tr>
    `;
  }).join('');
  el.innerHTML = `
    <table style="width:100%;border-collapse:collapse;font-size:12px">
      <thead>
        <tr style="border-bottom:1px solid var(--border);text-align:left">
          <th style="padding:8px 10px;font-size:11px;color:var(--text-3);font-weight:600;white-space:nowrap">时间</th>
          <th style="padding:8px 10px;font-size:11px;color:var(--text-3);font-weight:600">用户</th>
          <th style="padding:8px 10px;font-size:11px;color:var(--text-3);font-weight:600">情感</th>
          <th style="padding:8px 10px;font-size:11px;color:var(--text-3);font-weight:600;text-align:center">指向</th>
          <th style="padding:8px 10px;font-size:11px;color:var(--text-3);font-weight:600;text-align:center">讽刺</th>
          <th style="padding:8px 10px;font-size:11px;color:var(--text-3);font-weight:600;text-align:center">资格</th>
          <th style="padding:8px 10px;font-size:11px;color:var(--text-3);font-weight:600;text-align:center">间隙</th>
          <th style="padding:8px 10px;font-size:11px;color:var(--text-3);font-weight:600">意图</th>
          <th style="padding:8px 10px;font-size:11px;color:var(--text-3);font-weight:600;text-align:center">紧急</th>
          <th style="padding:8px 10px;font-size:11px;color:var(--text-3);font-weight:600;text-align:center">相关</th>
          <th style="padding:8px 10px;font-size:11px;color:var(--text-3);font-weight:600">信号</th>
        </tr>
      </thead>
      <tbody>${rows}</tbody>
    </table>
  `;
}

let _radarMode = 'avg';

function setRadarMode(mode) {
  _radarMode = mode;
  const avgBtn = $('radarBtnAvg');
  const lastBtn = $('radarBtnLast');
  if (avgBtn) avgBtn.classList.toggle('active', mode === 'avg');
  if (lastBtn) lastBtn.classList.toggle('active', mode === 'last');
  const res = _cogState && _cogState.data;
  if (res && res.events) {
    renderDirectedRadar(res.events);
  }
}

function renderDirectedRadar(events) {
  const el = $('cogDirectedRadar');
  if (!el) return;
  let chart = echarts.getInstanceByDom(el);
  if (!events.length || typeof echarts === 'undefined') {
    if (chart) { chart.dispose(); window.removeEventListener('resize', el._radarResize); }
    el.innerHTML = '<div style="color:var(--text-2);padding:12px">暂无指向性数据</div>';
    return;
  }
  const keys = ['mention_score','reference_score','name_match_score','second_person_score','question_score','imperative_score','topic_relevance_score','emotional_disclosure_score','attention_seeking_score','recency_score','turn_taking_score'];
  const labels = ['提及','引用','名称匹配','第二人称','问句','祈使','话题相关','情感表露','寻求关注','时效','轮次'];

  let data = [];
  let seriesName = '';

  if (_radarMode === 'last') {
    // 最近一次：从后往前找第一个有 directed_signals 的事件
    let lastEvent = null;
    for (let i = events.length - 1; i >= 0; i--) {
      const sig = events[i].directed_signals || {};
      if (sig && Object.keys(sig).length > 0) {
        lastEvent = events[i];
        break;
      }
    }
    if (!lastEvent) {
      el.innerHTML = '<div style="color:var(--text-2);padding:12px">暂无指向性数据</div>';
      return;
    }
    const sig = lastEvent.directed_signals || {};
    data = keys.map((k) => Math.round((sig[k] || 0) * 100) / 100);
    seriesName = '最近一次指向性';
  } else {
    // 平均值（默认）
    const sums = {};
    let count = 0;
    for (const e of events) {
      const sig = e.directed_signals || {};
      if (!sig || Object.keys(sig).length === 0) continue;
      for (const k of keys) {
        sums[k] = (sums[k] || 0) + (sig[k] || 0);
      }
      count++;
    }
    if (count === 0) {
      el.innerHTML = '<div style="color:var(--text-2);padding:12px">暂无指向性数据</div>';
      return;
    }
    data = keys.map((k) => Math.round((sums[k] / count) * 100) / 100);
    seriesName = '平均指向性信号';
  }

  if (!chart) {
    chart = echarts.init(el, 'dark');
    const onResize = () => chart.resize();
    window.removeEventListener('resize', el._radarResize);
    window.addEventListener('resize', onResize);
    el._radarResize = onResize;
  }
  chart.setOption({
    backgroundColor: 'transparent',
    tooltip: { trigger: 'item' },
    radar: {
      indicator: labels.map((name, i) => ({ name, max: 1 })),
      radius: '65%',
      axisName: { color: '#8b949e', fontSize: 11 },
      splitArea: { areaStyle: { color: ['rgba(88,166,255,0.02)', 'rgba(88,166,255,0.06)'] } },
      axisLine: { lineStyle: { color: 'rgba(139,148,158,0.2)' } },
      splitLine: { lineStyle: { color: 'rgba(139,148,158,0.15)' } },
    },
    series: [{
      type: 'radar',
      data: [{
        value: data,
        name: seriesName,
        areaStyle: { color: 'rgba(88,166,255,0.2)' },
        lineStyle: { color: '#58a6ff', width: 2 },
        itemStyle: { color: '#58a6ff' },
      }],
    }],
  }, true);
}

let _cogGroupFilter = '';

function cogToggleDropdown() {
  const list = $('cogDropdownList');
  const arrow = $('cogDropdownArrow');
  if (!list) return;
  const show = list.style.display === 'none';
  list.style.display = show ? 'block' : 'none';
  if (arrow) arrow.style.transform = show ? 'rotate(180deg)' : 'rotate(0deg)';
  if (show) {
    const close = (e) => {
      if (!e.target.closest('#cogGroupDropdown')) {
        list.style.display = 'none';
        if (arrow) arrow.style.transform = 'rotate(0deg)';
        document.removeEventListener('click', close);
      }
    };
    setTimeout(() => document.addEventListener('click', close), 0);
  }
}

function cogSelectGroup(gid) {
  _cogGroupFilter = gid;
  const label = $('cogDropdownLabel');
  if (label) label.textContent = gid || '全部群聊';
  const list = $('cogDropdownList');
  if (list) list.style.display = 'none';
  const arrow = $('cogDropdownArrow');
  if (arrow) arrow.style.transform = 'rotate(0deg)';
  loadCognition();
}

let _cogState = { data: null };
let _cogAbort = null;

async function loadCognition() {
  renderPersonaSelect();
  if (!currentPersona && personas.length > 0) {
    selectPersona(personas[0].name);
  }
  if (!currentPersona) return;

  // Cancel previous pending request
  if (_cogAbort) {
    _cogAbort.abort();
    _cogAbort = null;
  }
  const controller = new AbortController();
  _cogAbort = controller;

  try {
    const groupId = _cogGroupFilter;
    const qs = groupId ? `?group_id=${encodeURIComponent(groupId)}&limit=100` : '?limit=100';
    const res = await get(`/personas/${currentPersona}/cognition${qs}`, controller.signal);
    if (_cogAbort !== controller) return; // stale request
    _cogState.data = res;
    const events = res.events || [];

    // Build group dropdown from all events (use unfiltered request for group list)
    let allEvents = events;
    if (groupId) {
      try {
        const allRes = await get(`/personas/${currentPersona}/cognition?limit=200`);
        allEvents = allRes.events || [];
      } catch (e) { /* ignore */ }
    }
    const groups = Array.from(new Set(allEvents.map((e) => e.group_id).filter(Boolean))).sort();
    const listEl = $('cogDropdownList');
    const labelEl = $('cogDropdownLabel');
    if (listEl) {
      const items = [{ gid: '', label: '全部群聊' }].concat(groups.map((g) => ({ gid: g, label: g })));
      listEl.innerHTML = items.map((it) => {
        const active = it.gid === _cogGroupFilter;
        return `<div onclick="cogSelectGroup('${it.gid.replace(/'/g, "\\'")}')" class="diary-dropdown-item" style="padding:8px 12px;font-size:13px;cursor:pointer;color:${active ? 'var(--accent)' : 'var(--text)'};background:${active ? 'var(--surface-2)' : 'transparent'};border-radius:6px;margin:2px 4px"
          onmouseenter="this.style.background='var(--surface-2)'" onmouseleave="this.style.background='${active ? 'var(--surface-2)' : 'transparent'}'">${it.label}</div>`;
      }).join('');
    }
    if (labelEl) labelEl.textContent = _cogGroupFilter || '全部群聊';

    renderCognitionEvents(events);
    setRadarMode(_radarMode);
    renderEmotionDistribution($('cogEmotionDistribution'), res.emotion_distribution || {});
    renderEmotionTimeline($('cogEmotionTimeline'), events);
  } catch (e) {
    if (e.name === 'AbortError') return;
    console.error('loadCognition', e);
    const els = ['cogEvents', 'cogDirectedRadar', 'cogEmotionDistribution', 'cogEmotionTimeline'];
    els.forEach((id) => { const el = $(id); if (el) el.textContent = '加载失败'; });
  } finally {
    if (_cogAbort === controller) _cogAbort = null;
  }
}

let _diaryKeywordFilter = '';

function diarySetKeyword(kw) {
  _diaryKeywordFilter = kw;
  diaryRenderKeywordBar();
  diaryRenderEntries(_diaryEntriesCache);
}

function diaryClearKeyword() {
  _diaryKeywordFilter = '';
  diaryRenderKeywordBar();
  diaryRenderEntries(_diaryEntriesCache);
}

function diaryRenderKeywordBar() {
  const bar = $('diaryKeywordFilterBar');
  const active = $('diaryActiveKeyword');
  if (bar) bar.style.display = _diaryKeywordFilter ? 'flex' : 'none';
  if (active) active.textContent = _diaryKeywordFilter;
}

function diaryRenderEntries(entries) {
  const listEl = $('diaryList');
  if (!listEl) return;
  let filtered = entries || [];
  if (_diaryKeywordFilter) {
    filtered = filtered.filter((e) => (e.keywords || []).includes(_diaryKeywordFilter));
  }
  if (!filtered.length) {
    listEl.innerHTML = '<div style="color:var(--text-2);padding:12px">暂无日记</div>';
    return;
  }
  listEl.innerHTML = filtered.map((e) => {
    const ts = e.created_at ? new Date(e.created_at).toLocaleString('zh-CN', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '—';
    const kws = (e.keywords || []).slice(0, 8).map((k) => `<span style="background:var(--bg-2);border:1px solid var(--border);border-radius:8px;padding:2px 8px;font-size:11px;color:var(--text-2)">${k}</span>`).join('');
    return `
      <div style="background:var(--bg-2);border:1px solid var(--border);border-radius:8px;padding:12px">
        <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
          <span style="font-size:12px;color:var(--text-2)">${ts}</span>
          <span style="font-size:11px;color:var(--text-2);background:var(--bg);padding:2px 8px;border-radius:4px">${e.group_id || '—'}</span>
        </div>
        <div style="font-size:14px;font-weight:600;margin-bottom:6px;color:var(--text)">${e.summary || '无摘要'}</div>
        <div style="font-size:13px;color:var(--text);line-height:1.6;margin-bottom:8px;white-space:pre-wrap">${e.content || ''}</div>
        <div style="display:flex;flex-wrap:wrap;gap:4px">${kws}</div>
      </div>
    `;
  }).join('');
}

let _diaryEntriesCache = [];

let _diaryGroupFilter = '';

function diaryToggleDropdown() {
  const list = $('diaryDropdownList');
  const arrow = $('diaryDropdownArrow');
  if (!list) return;
  const open = list.style.display === 'block';
  list.style.display = open ? 'none' : 'block';
  if (arrow) arrow.style.transform = open ? 'rotate(0deg)' : 'rotate(180deg)';
  if (!open) {
    const close = (e) => {
      if (!list.contains(e.target) && !$('diaryGroupDropdown').contains(e.target)) {
        list.style.display = 'none';
        if (arrow) arrow.style.transform = 'rotate(0deg)';
        document.removeEventListener('click', close);
      }
    };
    setTimeout(() => document.addEventListener('click', close), 0);
  }
}

function diarySelectGroup(gid) {
  _diaryGroupFilter = gid;
  const label = $('diaryDropdownLabel');
  if (label) label.textContent = gid || '全部群聊';
  const list = $('diaryDropdownList');
  const arrow = $('diaryDropdownArrow');
  if (list) list.style.display = 'none';
  if (arrow) arrow.style.transform = 'rotate(0deg)';
  diaryLoadData();
}

async function diaryLoadData() {
  renderPersonaSelect();
  if (!currentPersona && personas.length > 0) {
    selectPersona(personas[0].name);
  }
  if (!currentPersona) return;
  try {
    const groupId = _diaryGroupFilter;
    const qs = groupId ? `?group_id=${encodeURIComponent(groupId)}` : '';
    const res = await get(`/personas/${currentPersona}/diary${qs}`);

    // Stats
    const stats = res.stats || {};
    const totalEl = $('diaryTotal');
    const groupsEl = $('diaryGroups');
    if (totalEl) totalEl.textContent = (stats.total || 0).toLocaleString();
    if (groupsEl) groupsEl.textContent = (stats.groups || 0).toLocaleString();

    // Keywords
    const kwContainer = $('diaryKeywords');
    const topKws = stats.top_keywords || [];
    if (kwContainer) {
      if (!topKws.length) {
        kwContainer.innerHTML = '<span style="color:var(--text-2)">暂无关键词</span>';
      } else {
        kwContainer.innerHTML = topKws.map(([kw, cnt]) => {
          const active = kw === _diaryKeywordFilter;
          return `
            <span onclick="diarySetKeyword('${kw.replace(/'/g, "\\'")}')" style="cursor:pointer;background:${active ? 'var(--accent)' : 'var(--bg-2)'};border:1px solid var(--border);border-radius:12px;padding:3px 10px;font-size:12px;color:${active ? '#fff' : 'var(--text)'};transition:.15s"
              onmouseenter="this.style.opacity='0.85'" onmouseleave="this.style.opacity='1'">${kw} <span style="opacity:0.7">${cnt}</span></span>
          `;
        }).join('');
      }
    }

    // Group filter dropdown list
    const listEl = $('diaryDropdownList');
    const labelEl = $('diaryDropdownLabel');
    if (listEl) {
      const groups = res.groups || [];
      const items = [{ gid: '', label: '全部群聊' }].concat(groups.map((g) => ({ gid: g, label: g })));
      listEl.innerHTML = items.map((it) => {
        const active = it.gid === _diaryGroupFilter;
        return `<div onclick="diarySelectGroup('${it.gid.replace(/'/g, "\\'")}')" class="diary-dropdown-item" style="padding:8px 12px;font-size:13px;cursor:pointer;color:${active ? 'var(--accent)' : 'var(--text)'};background:${active ? 'var(--surface-2)' : 'transparent'};border-radius:6px;margin:2px 4px"
          onmouseenter="this.style.background='var(--surface-2)'" onmouseleave="this.style.background='${active ? 'var(--surface-2)' : 'transparent'}'">${it.label}</div>`;
      }).join('');
    }
    if (labelEl) labelEl.textContent = _diaryGroupFilter || '全部群聊';

    // Entries
    _diaryEntriesCache = res.entries || [];
    diaryRenderKeywordBar();
    diaryRenderEntries(_diaryEntriesCache);
  } catch (e) {
    console.error('diaryLoadData', e);
    const listEl = $('diaryList');
    if (listEl) listEl.innerHTML = '<div style="color:var(--text-2);padding:12px">加载失败</div>';
  }
}

function ttRenderRecentTable() {
  const tbody = document.querySelector('#ttRecentTable tbody');
  const pgEl = $('ttPagination');
  if (!tbody) return;
  const records = (_ttState.data?.recent_with_breakdown || []);
  if (!records.length) {
    tbody.innerHTML = '<tr><td colspan="6" style="text-align:center;color:var(--text-2)">暂无记录</td></tr>';
    if (pgEl) pgEl.style.display = 'none';
    return;
  }

  const pageSize = 10;
  const totalPages = Math.max(1, Math.ceil(records.length / pageSize));
  const page = Math.min(_ttState.page, totalPages - 1);
  _ttState.page = page;
  const slice = records.slice(page * pageSize, (page + 1) * pageSize);

  const top3 = (bd) => {
    if (!bd || typeof bd !== 'object') return '—';
    const entries = Object.entries(bd)
      .filter(([k]) => !['total', 'system_prompt_total', 'user_message'].includes(k))
      .sort((a, b) => b[1] - a[1]);
    const nonzero = entries.filter(([, v]) => v > 0);
    if (!nonzero.length) return '—';
    return nonzero
      .slice(0, 3)
      .map(([k, v]) => `${k} ${v}`)
      .join(', ') || '—';
  };

  tbody.innerHTML = slice.map((r) => {
    const ts = r.timestamp ? new Date(r.timestamp * 1000).toLocaleString('zh-CN', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '—';
    return `
      <tr>
        <td>${ts}</td>
        <td>${r.task_name || '—'}</td>
        <td>${r.model || '—'}</td>
        <td class="mono">${r.prompt_tokens || 0}</td>
        <td class="mono">${r.completion_tokens || 0}</td>
        <td style="font-size:12px;color:var(--text-2);max-width:200px;overflow:hidden;text-overflow:ellipsis" title="${top3(r.breakdown).replace(/"/g, '&quot;')}">${top3(r.breakdown)}</td>
      </tr>
    `;
  }).join('');

  if (pgEl) {
    pgEl.style.display = totalPages > 1 ? 'flex' : 'none';
    const info = $('ttPageInfo');
    if (info) info.textContent = `第 ${page + 1} / ${totalPages} 页`;
    const prev = $('ttPrevPage');
    const next = $('ttNextPage');
    if (prev) prev.disabled = page <= 0;
    if (next) next.disabled = page >= totalPages - 1;
  }
}


// ── Users ─────────────────────────────────────────────
let _usersGroupFilter = '';

function loadUsers() {
  renderPersonaSelect();
  if (!currentPersona && personas.length > 0) {
    selectPersona(personas[0].name);
  }
  usersLoadData();
}

function usersToggleDropdown() {
  const list = $('usersDropdownList');
  const arrow = $('usersDropdownArrow');
  if (!list) return;
  const open = list.style.display === 'block';
  list.style.display = open ? 'none' : 'block';
  if (arrow) arrow.style.transform = open ? 'rotate(0deg)' : 'rotate(180deg)';
  if (!open) {
    const close = (e) => {
      if (!list.contains(e.target) && !$('usersGroupDropdown').contains(e.target)) {
        list.style.display = 'none';
        if (arrow) arrow.style.transform = 'rotate(0deg)';
        document.removeEventListener('click', close);
      }
    };
    setTimeout(() => document.addEventListener('click', close), 0);
  }
}

function usersSelectGroup(gid) {
  _usersGroupFilter = gid;
  const label = $('usersDropdownLabel');
  if (label) label.textContent = gid || '全部群聊';
  const list = $('usersDropdownList');
  const arrow = $('usersDropdownArrow');
  if (list) list.style.display = 'none';
  if (arrow) arrow.style.transform = 'rotate(0deg)';
  usersLoadData();
}

function usersBarColor(score) {
  if (score >= 0.7) return 'var(--success)';
  if (score >= 0.4) return 'var(--accent)';
  return 'var(--danger)';
}

function usersRenderList(users) {
  const listEl = $('usersList');
  if (!listEl) return;
  listEl.style.display = 'grid';
  listEl.style.gridTemplateColumns = 'repeat(auto-fill, minmax(300px, 1fr))';
  listEl.style.gap = '12px';
  if (!users.length) {
    listEl.innerHTML = '<div style="color:var(--text-2);padding:12px">暂无用户画像数据</div>';
    return;
  }
  listEl.innerHTML = users.map((u) => {
    const engagement = u.engagement_rate || 0;
    const count = u.interaction_count || 0;
    const familiarity = Math.min(1.0, Math.log1p(count) / Math.log1p(50));
    const displayName = (u.name || u.user_id || '未知用户');
    const userId = u.user_id || '';
    const interests = (u.interest_graph || []).map((n) => `
      <span style="background:var(--bg-2);border:1px solid var(--border);border-radius:4px;padding:1px 6px;font-size:10px;color:var(--text-2)">${n.topic || ''}</span>
    `).join('');
    const lastAt = u.last_interaction_at ? new Date(u.last_interaction_at).toLocaleDateString('zh-CN') : '-';

    const bar = (label, score) => `
      <div style="display:flex;align-items:center;gap:6px">
        <span style="font-size:10px;color:var(--text-3);width:44px;flex-shrink:0;text-align:right">${label}</span>
        <div style="flex:1;height:4px;background:var(--bg-2);border-radius:2px;overflow:hidden">
          <div style="width:${(score * 100).toFixed(0)}%;height:100%;background:${usersBarColor(score)};border-radius:2px;transition:width .3s"></div>
        </div>
        <span style="font-size:10px;color:var(--text-3);width:28px;text-align:right">${(score * 100).toFixed(0)}%</span>
      </div>
    `;

    return `
      <div style="background:var(--bg-2);border:1px solid var(--border);border-radius:8px;padding:10px 12px">
        <div style="display:flex;gap:10px;align-items:flex-start">
          <div style="width:32px;height:32px;border-radius:50%;background:var(--accent);display:flex;align-items:center;justify-content:center;font-size:14px;color:#fff;font-weight:700;flex-shrink:0;margin-top:2px">${(displayName[0] || '?').toUpperCase()}</div>
          <div style="flex:1;min-width:0">
            <div style="display:flex;justify-content:space-between;align-items:center;gap:8px">
              <div style="min-width:0">
                <div style="font-size:14px;font-weight:600;color:var(--text);white-space:nowrap;overflow:hidden;text-overflow:ellipsis">${displayName}</div>
                <div style="font-size:10px;color:var(--text-3);margin-top:1px">${userId} · ${count}次互动 · 最近${lastAt}</div>
              </div>
              <div style="text-align:right;flex-shrink:0">
                <div style="font-size:16px;font-weight:700;color:${usersBarColor(familiarity)}">${(familiarity * 100).toFixed(0)}%</div>
                <div style="font-size:9px;color:var(--text-3)">熟悉度</div>
              </div>
            </div>
            <div style="margin-top:6px;display:flex;flex-direction:column;gap:3px">
              ${bar('互动率', engagement)}
            </div>
            ${interests ? `<div style="display:flex;flex-wrap:wrap;gap:3px;margin-top:5px">${interests}</div>` : ''}
          </div>
        </div>
      </div>
    `;
  }).join('');
}

async function usersLoadData() {
  renderPersonaSelect();
  if (!currentPersona && personas.length > 0) {
    selectPersona(personas[0].name);
  }
  if (!currentPersona) return;
  try {
    const groupId = _usersGroupFilter;
    const qs = groupId ? `?group_id=${encodeURIComponent(groupId)}` : '';
    const res = await get(`/personas/${currentPersona}/users${qs}`);

    const users = res.users || [];
    const groups = res.groups || [];

    // Stats
    const totalEl = $('usersTotal');
    const groupsEl = $('usersGroups');
    if (totalEl) totalEl.textContent = users.length.toLocaleString();
    if (groupsEl) groupsEl.textContent = groups.length.toLocaleString();

    // Dropdown
    const listEl = $('usersDropdownList');
    const labelEl = $('usersDropdownLabel');
    if (listEl) {
      const items = [{ gid: '', label: '全部群聊' }].concat(groups.map((g) => ({ gid: g, label: g })));
      listEl.innerHTML = items.map((it) => {
        const active = it.gid === _usersGroupFilter;
        return `<div onclick="usersSelectGroup('${it.gid.replace(/'/g, "\\'")}')" class="diary-dropdown-item" style="padding:8px 12px;font-size:13px;cursor:pointer;color:${active ? 'var(--accent)' : 'var(--text)'};background:${active ? 'var(--surface-2)' : 'transparent'};border-radius:6px;margin:2px 4px"
          onmouseenter="this.style.background='var(--surface-2)'" onmouseleave="this.style.background='${active ? 'var(--surface-2)' : 'transparent'}'">${it.label}</div>`;
      }).join('');
    }
    if (labelEl) labelEl.textContent = _usersGroupFilter || '全部群聊';

    usersRenderList(users);
  } catch (e) {
    console.error('usersLoadData', e);
    const listEl = $('usersList');
    if (listEl) listEl.innerHTML = '<div style="color:var(--text-2);padding:12px">加载失败</div>';
  }
}

// ── Glossary ──────────────────────────────────────────
let _glossaryGroupFilter = '';
let _glossarySearchQuery = '';
let _glossaryEntriesCache = [];

function loadGlossary() {
  renderPersonaSelect();
  if (!currentPersona && personas.length > 0) {
    selectPersona(personas[0].name);
  }
  glossaryLoadData();
}

function glossaryToggleDropdown() {
  const list = $('glossaryDropdownList');
  const arrow = $('glossaryDropdownArrow');
  if (!list) return;
  const open = list.style.display === 'block';
  list.style.display = open ? 'none' : 'block';
  if (arrow) arrow.style.transform = open ? 'rotate(0deg)' : 'rotate(180deg)';
  if (!open) {
    const close = (e) => {
      if (!list.contains(e.target) && !$('glossaryGroupDropdown').contains(e.target)) {
        list.style.display = 'none';
        if (arrow) arrow.style.transform = 'rotate(0deg)';
        document.removeEventListener('click', close);
      }
    };
    setTimeout(() => document.addEventListener('click', close), 0);
  }
}

function glossarySelectGroup(gid) {
  _glossaryGroupFilter = gid;
  const label = $('glossaryDropdownLabel');
  if (label) label.textContent = gid || '全部群聊';
  const list = $('glossaryDropdownList');
  const arrow = $('glossaryDropdownArrow');
  if (list) list.style.display = 'none';
  if (arrow) arrow.style.transform = 'rotate(0deg)';
  glossaryLoadData();
}

function glossaryOnSearch(value) {
  _glossarySearchQuery = value.trim();
  glossaryRenderEntries();
}

function glossaryConfidenceTag(confidence) {
  if (confidence >= 0.8) return { text: '高', color: 'var(--success)' };
  if (confidence >= 0.6) return { text: '~', color: 'var(--accent)' };
  return { text: '?', color: 'var(--danger)' };
}

function glossaryRenderEntries() {
  const listEl = $('glossaryList');
  if (!listEl) return;

  let terms = _glossaryEntriesCache;

  if (_glossarySearchQuery) {
    const q = _glossarySearchQuery.toLowerCase();
    terms = terms.filter((t) =>
      (t.term || '').toLowerCase().includes(q) ||
      (t.definition || '').toLowerCase().includes(q)
    );
  }

  if (!terms.length) {
    listEl.innerHTML = '<div style="color:var(--text-2);padding:12px">暂无名词解释数据</div>';
    return;
  }

  listEl.innerHTML = terms.map((t) => {
    const tag = glossaryConfidenceTag(t.confidence || 0);
    const examples = (t.context_examples || []).slice(0, 3);
    const related = (t.related_terms || []).slice(0, 5);
    const domainColors = {
      tech: '#58a6ff', daily: '#a371f7', culture: '#e3b341', game: '#3fb950', custom: '#8b949e',
    };
    const domainColor = domainColors[t.domain] || domainColors.custom;

    return `
      <div style="background:var(--bg-2);border:1px solid var(--border);border-radius:8px;padding:12px 14px">
        <div style="display:flex;justify-content:space-between;align-items:flex-start;gap:8px">
          <div style="flex:1;min-width:0">
            <div style="display:flex;align-items:center;gap:8px;flex-wrap:wrap">
              <span style="font-size:15px;font-weight:600;color:var(--text)">${t.term || '未知'}</span>
              <span style="font-size:11px;color:${tag.color};font-weight:600">${tag.text}</span>
              <span style="font-size:10px;color:${domainColor};background:${domainColor}15;border:1px solid ${domainColor}40;border-radius:4px;padding:1px 6px">${t.domain || 'custom'}</span>
            </div>
            <div style="font-size:13px;color:var(--text-2);margin-top:4px;line-height:1.5">${t.definition || '待明确'}</div>
          </div>
          <div style="text-align:right;flex-shrink:0">
            <div style="font-size:11px;color:var(--text-3)">引用 ${t.usage_count || 0} 次</div>
            <div style="font-size:10px;color:var(--text-3);margin-top:2px">${t.source || 'inferred'}</div>
          </div>
        </div>
        ${examples.length ? `<div style="margin-top:8px;display:flex;flex-direction:column;gap:3px">
          ${examples.map((ex) => `<div style="font-size:11px;color:var(--text-3);padding-left:8px;border-left:2px solid var(--border)">${ex}</div>`).join('')}
        </div>` : ''}
        ${related.length ? `<div style="margin-top:6px;display:flex;flex-wrap:wrap;gap:4px">
          ${related.map((r) => `<span style="font-size:10px;color:var(--text-3);background:var(--bg-2);border:1px solid var(--border);border-radius:4px;padding:1px 6px">${r}</span>`).join('')}
        </div>` : ''}
      </div>
    `;
  }).join('');
}

async function glossaryLoadData() {
  renderPersonaSelect();
  if (!currentPersona && personas.length > 0) {
    selectPersona(personas[0].name);
  }
  if (!currentPersona) return;
  try {
    const groupId = _glossaryGroupFilter;
    const qs = groupId ? `?group_id=${encodeURIComponent(groupId)}` : '';
    const res = await get(`/personas/${currentPersona}/glossary${qs}`);

    _glossaryEntriesCache = res.terms || [];
    const groups = res.groups || [];

    // Stats
    const stats = res.stats || {};
    const totalEl = $('glossaryTotal');
    const groupsEl = $('glossaryGroups');
    if (totalEl) totalEl.textContent = (stats.total || 0).toLocaleString();
    if (groupsEl) groupsEl.textContent = groups.length.toLocaleString();

    // Dropdown
    const listEl = $('glossaryDropdownList');
    const labelEl = $('glossaryDropdownLabel');
    if (listEl) {
      const items = [{ gid: '', label: '全部群聊' }].concat(groups.map((g) => ({ gid: g, label: g })));
      listEl.innerHTML = items.map((it) => {
        const active = it.gid === _glossaryGroupFilter;
        return `<div onclick="glossarySelectGroup('${it.gid.replace(/'/g, "\\'")}')" class="diary-dropdown-item" style="padding:8px 12px;font-size:13px;cursor:pointer;color:${active ? 'var(--accent)' : 'var(--text)'};background:${active ? 'var(--surface-2)' : 'transparent'};border-radius:6px;margin:2px 4px"
          onmouseenter="this.style.background='var(--surface-2)'" onmouseleave="this.style.background='${active ? 'var(--surface-2)' : 'transparent'}'">${it.label}</div>`;
      }).join('');
    }
    if (labelEl) labelEl.textContent = _glossaryGroupFilter || '全部群聊';

    glossaryRenderEntries();
  } catch (e) {
    console.error('glossaryLoadData', e);
    const listEl = $('glossaryList');
    if (listEl) listEl.innerHTML = '<div style="color:var(--text-2);padding:12px">加载失败</div>';
  }
}

// ── Page Loader Registrations (analytics) ─────────────
registerPageLoader('token-tracker', { init: loadTokenTracker, refresh: ttLoadData });
registerPageLoader('cognition', { init: loadCognition });
registerPageLoader('diary', { init: diaryLoadData });
registerPageLoader('users', { init: loadUsers });
registerPageLoader('glossary', { init: loadGlossary });
