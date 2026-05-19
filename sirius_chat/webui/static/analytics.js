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

// ── 记忆可视化 ───────────────────────────────────────
let _mvChartBasic = null;
let _mvChartDiary = null;
let _mvChartUser = null;
let _mvGroupFilter = '';
let _mvDataCache = null;

function mvToggleDropdown() {
  const list = $('mvDropdownList');
  const arrow = $('mvDropdownArrow');
  if (!list) return;
  const open = list.style.display === 'block';
  list.style.display = open ? 'none' : 'block';
  if (arrow) arrow.style.transform = open ? 'rotate(0deg)' : 'rotate(180deg)';
  if (!open) {
    const close = (e) => {
      if (!list.contains(e.target) && !$('mvGroupDropdown').contains(e.target)) {
        list.style.display = 'none';
        if (arrow) arrow.style.transform = 'rotate(0deg)';
        document.removeEventListener('click', close);
      }
    };
    setTimeout(() => document.addEventListener('click', close), 0);
  }
}

function mvSelectGroup(gid) {
  _mvGroupFilter = gid;
  const label = $('mvDropdownLabel');
  if (label) label.textContent = gid || '全部群聊';
  const list = $('mvDropdownList');
  const arrow = $('mvDropdownArrow');
  if (list) list.style.display = 'none';
  if (arrow) arrow.style.transform = 'rotate(0deg)';
  loadMemoryViz();
}

async function loadMemoryViz() {
  renderPersonaSelect();
  if (!currentPersona && personas.length > 0) {
    selectPersona(personas[0].name);
  }
  if (!currentPersona) return;

  [_mvChartBasic, _mvChartDiary, _mvChartUser].forEach((c) => { if (c) c.dispose(); });
  _mvChartBasic = null;
  _mvChartDiary = null;
  _mvChartUser = null;

  try {
    const qs = _mvGroupFilter ? `?group_id=${encodeURIComponent(_mvGroupFilter)}` : '';
    const res = await get(`/personas/${currentPersona}/memory-viz${qs}`);
    _mvDataCache = res;

    // 填充群组下拉
    const listEl = $('mvDropdownList');
    const labelEl = $('mvDropdownLabel');
    if (listEl) {
      const groups = res.groups || [];
      const items = [{ gid: '', label: '全部群聊' }].concat(groups.map((g) => ({ gid: g, label: g })));
      listEl.innerHTML = items.map((it) => {
        const active = it.gid === _mvGroupFilter;
        return `<div onclick="mvSelectGroup('${it.gid.replace(/'/g, "\\'")}')" style="padding:8px 12px;font-size:13px;cursor:pointer;color:${active ? 'var(--accent)' : 'var(--text)'};background:${active ? 'var(--surface-2)' : 'transparent'};border-radius:6px;margin:2px 4px"
          onmouseenter="this.style.background='var(--surface-2)'" onmouseleave="this.style.background='${active ? 'var(--surface-2)' : 'transparent'}'">${it.label}</div>`;
      }).join('');
    }
    if (labelEl) labelEl.textContent = _mvGroupFilter || '全部群聊';

    mvRenderTimeline(res.basic_timeline || {});
    mvRenderDiaryCluster(res.diary_entries || [], res.diary_top_keywords || []);
    mvRenderBipartite(res.user_nodes || [], res.topic_nodes || [], res.user_topic_links || []);
  } catch (e) {
    console.error('loadMemoryViz', e);
    ['basicTimelineChart', 'diaryClusterChart', 'userNetworkChart'].forEach((id) => {
      const el = $(id);
      if (el) el.innerHTML = '<div style="color:var(--text-2);padding:40px;text-align:center">加载失败</div>';
    });
  }
}

// ── 基础记忆时间线：散点图，按群分组，支持缩放 ───────
const MV_ROLE_COLORS = { human: '#58a6ff', assistant: '#3fb950', system: '#d2a8ff' };
const MV_ROLE_LABELS = { human: '用户', assistant: '助手', system: '系统' };

function mvRenderTimeline(timeline) {
  const el = $('basicTimelineChart');
  if (!el) return;

  const recent = timeline.recent || [];
  if (!recent.length) {
    el.innerHTML = '<div style="color:var(--text-2);padding:40px;text-align:center">暂无基础记忆数据</div>';
    return;
  }

  if (!_mvChartBasic) {
    el.innerHTML = '';
    _mvChartBasic = echarts.init(el, 'dark');
    const onResize = () => _mvChartBasic.resize();
    window.removeEventListener('resize', el._mvResizeBasic);
    window.addEventListener('resize', onResize);
    el._mvResizeBasic = onResize;
  }

  const groups = Array.from(new Set(recent.map((e) => e.group_id).filter(Boolean)));
  const isMultiGroup = groups.length > 1;
  const roles = ['human', 'assistant', 'system'];
  const roleOrder = { human: 0, assistant: 1, system: 2 };

  // 按群分组，每群一个 scatter 系列
  const series = [];
  for (const gid of groups) {
    const gEntries = recent.filter((e) => e.group_id === gid);
    series.push({
      name: gid,
      type: 'scatter',
      symbolSize: (val) => Math.min(18, 6 + (val[3] || '').length / 8),
      data: gEntries.map((e) => ({
        value: [
          e.timestamp ? new Date(e.timestamp).getTime() : 0,
          isMultiGroup ? gid : (roleOrder[e.role] ?? 1),
          e.speaker_name || '—',
          e.content || '',
          e.role,
        ],
      })),
      itemStyle: {
        color: (params) => MV_ROLE_COLORS[params.value[4]] || '#8b949e',
        opacity: 0.85,
      },
    });
  }

  _mvChartBasic.setOption({
    backgroundColor: 'transparent',
    tooltip: {
      trigger: 'item',
      formatter: (params) => {
        const v = params.value;
        const ts = v[0] ? new Date(v[0]).toLocaleString('zh-CN') : '—';
        const role = MV_ROLE_LABELS[v[4]] || v[4];
        const content = (v[3] || '').slice(0, 150);
        return `<b>${v[2]}</b> <span style="color:${MV_ROLE_COLORS[v[4]] || '#8b949e'}">(${role})</span><br/>时间: ${ts}<br/><div style="margin-top:4px;max-width:320px;white-space:pre-wrap;font-size:12px;color:#c9d1d9">${content}</div>`;
      },
    },
    legend: {
      data: isMultiGroup ? groups : ['用户', '助手', '系统'],
      textStyle: { color: '#c9d1d9', fontSize: 11 },
      top: 0,
      type: 'scroll',
    },
    grid: { top: 36, bottom: 24, left: 12, right: 24, containLabel: true },
    xAxis: {
      type: 'time',
      axisLabel: { fontSize: 11, color: '#8b949e', formatter: '{MM}-{dd}' },
      splitLine: { lineStyle: { color: '#30363d' } },
    },
    yAxis: isMultiGroup ? {
      type: 'category',
      data: groups,
      axisLabel: { fontSize: 11, color: '#c9d1d9', width: 120, overflow: 'truncate' },
      axisLine: { show: false },
      axisTick: { show: false },
    } : {
      type: 'category',
      data: roles.map((r) => MV_ROLE_LABELS[r]),
      axisLabel: { fontSize: 11, color: '#c9d1d9' },
      axisLine: { show: false },
      axisTick: { show: false },
      splitLine: { show: false },
    },
    dataZoom: [
      { type: 'inside', xAxisIndex: 0 },
      { type: 'slider', xAxisIndex: 0, height: 18, bottom: 0, borderColor: '#30363d', fillerColor: 'rgba(88,166,255,0.15)', handleStyle: { color: '#58a6ff' }, textStyle: { color: '#8b949e', fontSize: 10 } },
    ],
    series,
  }, true);
}

// ── 3D 语义聚类：球面投影 + 随机正交基降维 ───────────
function mvSphereProject(vectors, nComponents) {
  if (!vectors || !vectors.length) return [];
  const dim = vectors[0].length;
  const n = vectors.length;
  const k = Math.min(nComponents, dim, n);

  // Step 1: 球面归一化 — 消除向量长度差异，保留方向信息
  const sphere = vectors.map((v) => {
    let norm = 0;
    for (let j = 0; j < dim; j++) norm += v[j] * v[j];
    norm = Math.sqrt(norm) || 1;
    return v.map((x) => x / norm);
  });

  // Step 2: 中心化
  const means = new Float64Array(dim);
  for (const v of sphere) for (let j = 0; j < dim; j++) means[j] += v[j];
  for (let j = 0; j < dim; j++) means[j] /= n;
  const centered = sphere.map((v) => v.map((x, j) => x - means[j]));

  // Step 3: 生成随机正交基（避免 PCA 第一主成分压倒性主导）
  // 使用 Gram-Schmidt 正交化生成 k 个随机正交方向
  function randomUnitVector(size) {
    const v = new Float64Array(size);
    for (let i = 0; i < size; i++) v[i] = Math.random() * 2 - 1;
    let norm = 0;
    for (let i = 0; i < size; i++) norm += v[i] * v[i];
    norm = Math.sqrt(norm) || 1;
    for (let i = 0; i < size; i++) v[i] /= norm;
    return v;
  }

  function dot(a, b) {
    let s = 0;
    for (let i = 0; i < a.length; i++) s += a[i] * b[i];
    return s;
  }

  const bases = [];
  for (let b = 0; b < k; b++) {
    let vec = randomUnitVector(dim);
    // 对已有基做 Gram-Schmidt 正交化
    for (let prev = 0; prev < bases.length; prev++) {
      const proj = dot(vec, bases[prev]);
      for (let i = 0; i < dim; i++) vec[i] -= proj * bases[prev][i];
    }
    // 重新归一化
    let norm = 0;
    for (let i = 0; i < dim; i++) norm += vec[i] * vec[i];
    norm = Math.sqrt(norm) || 1;
    for (let i = 0; i < dim; i++) vec[i] /= norm;
    bases.push(vec);
  }

  // Step 4: 投影到正交基
  let pts = centered.map((row) => {
    const pt = [];
    for (let c = 0; c < k; c++) {
      let v = 0;
      for (let j = 0; j < dim; j++) v += row[j] * bases[c][j];
      pt.push(v);
    }
    return pt;
  });

  // Step 5: 归一化到 [-1, 1]
  for (let c = 0; c < k; c++) {
    const vals = pts.map((p) => p[c]);
    const min = Math.min(...vals);
    const max = Math.max(...vals);
    const range = max - min || 1;
    pts.forEach((p) => { p[c] = ((p[c] - min) / range) * 2 - 1; });
  }

  return pts;
}

function mvRenderDiaryCluster(entries, topKeywords) {
  const el = $('diaryClusterChart');
  const kwEl = $('diaryKeywordsCloud');
  if (!el) return;

  if (kwEl && topKeywords.length) {
    const maxCnt = Math.max(...topKeywords.map(([, c]) => c));
    kwEl.innerHTML = topKeywords.map(([kw, cnt]) => {
      const size = 11 + Math.round((cnt / maxCnt) * 9);
      const opacity = 0.5 + (cnt / maxCnt) * 0.5;
      return `<span style="background:var(--bg-2);border:1px solid var(--border);border-radius:12px;padding:3px 10px;font-size:${size}px;color:var(--text);opacity:${opacity}">${kw} <span style="opacity:0.6;font-size:${Math.max(10, size - 2)}px">${cnt}</span></span>`;
    }).join('');
  }

  if (!entries.length) {
    el.innerHTML = '<div style="color:var(--text-2);padding:40px;text-align:center">暂无日记数据</div>';
    return;
  }

  const withEmb = entries.filter((e) => e.embedding && e.embedding.length >= 3);
  const withoutEmb = entries.filter((e) => !e.embedding || e.embedding.length < 3);

  let points3d = [];
  if (withEmb.length >= 3) {
    points3d = mvSphereProject(withEmb.map((e) => e.embedding), 3);
  } else if (withEmb.length > 0) {
    points3d = withEmb.map((e) => {
      const p = e.embedding;
      return [p[0] || 0, p[1] || 0, p[2] || 0];
    });
  }

  const allKws = topKeywords.map(([k]) => k);
  const palette = ['#58a6ff', '#3fb950', '#d2a8ff', '#f0883e', '#f778ba', '#79c0ff', '#56d364', '#d29922', '#ff7b72', '#bc8cff'];
  const kwColorMap = {};
  allKws.forEach((kw, i) => { kwColorMap[kw] = palette[i % palette.length]; });

  const scatter3dData = withEmb.map((e, i) => {
    const pt = points3d[i] || [0, 0, 0];
    const primaryKw = (e.keywords || []).find((k) => allKws.includes(k)) || '';
    return {
      value: [pt[0], pt[1], pt[2]],
      name: (e.summary || e.content.slice(0, 40)).replace(/"/g, ''),
      itemStyle: { color: kwColorMap[primaryKw] || '#8b949e' },
      _meta: {
        summary: e.summary || '',
        content: (e.content || '').slice(0, 200),
        group_id: e.group_id || '',
        created_at: e.created_at || '',
        keywords: (e.keywords || []).join(', '),
      },
    };
  });

  if (!_mvChartDiary) {
    el.innerHTML = '';
    _mvChartDiary = echarts.init(el, 'dark');
    const onResize = () => _mvChartDiary.resize();
    window.removeEventListener('resize', el._mvResizeDiary);
    window.addEventListener('resize', onResize);
    el._mvResizeDiary = onResize;
  }

  _mvChartDiary.setOption({
    backgroundColor: 'transparent',
    tooltip: {
      backgroundColor: 'rgba(22,27,34,0.95)',
      borderColor: '#30363d',
      borderWidth: 1,
      padding: [10, 14],
      textStyle: { color: '#e6edf3', fontSize: 12 },
      extraCssText: 'max-width:360px;word-break:break-all;white-space:normal;line-height:1.6;box-shadow:0 4px 12px rgba(0,0,0,0.4);',
      formatter: (p) => {
        const m = p.data._meta || {};
        const title = (p.data.name || '').slice(0, 60);
        const ts = m.created_at ? new Date(m.created_at).toLocaleString('zh-CN') : '—';
        const content = (m.content || '').slice(0, 180);
        return `<div style="max-width:340px">
          <div style="font-weight:600;color:#e6edf3;margin-bottom:6px;font-size:13px;word-break:break-all">${title}</div>
          <div style="color:#8b949e;font-size:11px;margin-bottom:4px">群: ${m.group_id || '—'} · ${ts}</div>
          <div style="color:#58a6ff;font-size:11px;margin-bottom:6px">${m.keywords || '—'}</div>
          <div style="color:#c9d1d9;font-size:12px;white-space:pre-wrap;word-break:break-all;line-height:1.5">${content}</div>
        </div>`;
      },
    },
    xAxis3D: {
      type: 'value',
      min: -1.2, max: 1.2,
      axisLine: { lineStyle: { color: '#484f58' } },
      axisLabel: { show: false },
      splitLine: { show: false },
    },
    yAxis3D: {
      type: 'value',
      min: -1.2, max: 1.2,
      axisLine: { lineStyle: { color: '#484f58' } },
      axisLabel: { show: false },
      splitLine: { show: false },
    },
    zAxis3D: {
      type: 'value',
      min: -1.2, max: 1.2,
      axisLine: { lineStyle: { color: '#484f58' } },
      axisLabel: { show: false },
      splitLine: { show: false },
    },
    grid3D: {
      boxWidth: 140,
      boxHeight: 120,
      boxDepth: 120,
      viewControl: {
        autoRotate: false,
        distance: 280,
        alpha: 30,
        beta: 40,
      },
      light: {
        main: { intensity: 1.2, shadow: false },
        ambient: { intensity: 0.5 },
      },
      environment: '#0d1117',
    },
    series: [{
      type: 'scatter3D',
      symbolSize: 14,
      data: scatter3dData,
      emphasis: {
        itemStyle: { borderColor: '#fff', borderWidth: 2 },
        label: {
          show: true,
          formatter: (p) => (p.data.name || '').slice(0, 20),
          fontSize: 11,
          color: '#e6edf3',
          distance: 8,
        },
      },
    }],
  }, true);

  if (withoutEmb.length) {
    const info = document.createElement('div');
    info.style.cssText = 'font-size:11px;color:var(--text-2);text-align:center;margin-top:4px';
    info.textContent = `${withoutEmb.length} 条日记缺少 embedding 向量，未显示`;
    el.parentElement.appendChild(info);
  }
}

// ── 用户-话题二部图：力导向 + 过滤低频话题 ────────────
function mvRenderBipartite(userNodes, topicNodes, links) {
  const el = $('userNetworkChart');
  if (!el) return;

  if (!userNodes.length && !topicNodes.length) {
    el.innerHTML = '<div style="color:var(--text-2);padding:40px;text-align:center">暂无用户画像数据</div>';
    return;
  }

  if (!_mvChartUser) {
    el.innerHTML = '';
    _mvChartUser = echarts.init(el, 'dark');
    const onResize = () => _mvChartUser.resize();
    window.removeEventListener('resize', el._mvResizeUser);
    window.addEventListener('resize', onResize);
    el._mvResizeUser = onResize;
  }

  // 统计话题关注度，过滤只被 1 个用户关注的话题
  const topicUsage = {};
  links.forEach((l) => { topicUsage[l.topic] = (topicUsage[l.topic] || 0) + 1; });
  const filteredTopics = topicNodes.filter((t) => (topicUsage[t.id] || 0) >= 2);
  const keptTopicIds = new Set(filteredTopics.map((t) => t.id));
  const filteredLinks = links.filter((l) => keptTopicIds.has(l.topic));

  const maxCount = Math.max(...userNodes.map((n) => n.interaction_count || 0), 1);
  const maxTopicUsage = Math.max(...Object.values(topicUsage), 1);

  const graphNodes = [];

  // 用户节点
  userNodes.forEach((n) => {
    const engRate = n.engagement_rate || 0;
    const size = 24 + Math.round(((n.interaction_count || 0) / maxCount) * 46);
    graphNodes.push({
      id: `u_${n.user_id}`,
      name: n.name || n.user_id,
      symbolSize: size,
      value: n.interaction_count || 0,
      category: 0,
      itemStyle: {
        color: engRate > 0.5 ? '#3fb950' : engRate > 0.2 ? '#58a6ff' : engRate > 0 ? '#d29922' : '#8b949e',
        borderColor: '#21262d',
        borderWidth: 2,
      },
      label: { show: true, fontSize: 12, color: '#c9d1d9' },
      tooltip: {
        formatter: () =>
          `<b>${n.name || n.user_id}</b><br/>`
          + `互动率: ${(engRate * 100).toFixed(1)}%<br/>`
          + `互动次数: ${n.interaction_count || 0}`,
      },
    });
  });

  // 话题节点
  filteredTopics.forEach((t) => {
    const usage = topicUsage[t.id] || 0;
    const size = 18 + Math.round((usage / maxTopicUsage) * 32);
    graphNodes.push({
      id: `t_${t.id}`,
      name: t.name,
      symbolSize: size,
      value: usage,
      category: 1,
      itemStyle: {
        color: '#d2a8ff',
        borderColor: '#21262d',
        borderWidth: 2,
      },
      label: { show: true, fontSize: 11, color: '#c9d1d9' },
      tooltip: {
        formatter: () => `<b>${t.name}</b><br/>被 ${usage} 个用户关注`,
      },
    });
  });

  const graphEdges = filteredLinks.map((l) => ({
    source: `u_${l.user_id}`,
    target: `t_${l.topic}`,
    lineStyle: { color: '#484f58', width: 1.2, opacity: 0.45 },
  }));

  _mvChartUser.setOption({
    backgroundColor: 'transparent',
    tooltip: { trigger: 'item' },
    legend: {
      data: ['用户', '兴趣话题'],
      textStyle: { color: '#c9d1d9', fontSize: 11 },
      top: 0,
    },
    series: [{
      type: 'graph',
      layout: 'force',
      roam: true,
      draggable: true,
      force: {
        repulsion: 350,
        edgeLength: [60, 180],
        gravity: 0.08,
      },
      categories: [
        { name: '用户', itemStyle: { color: '#58a6ff' } },
        { name: '兴趣话题', itemStyle: { color: '#d2a8ff' } },
      ],
      label: { show: true, fontSize: 12, color: '#c9d1d9' },
      lineStyle: { opacity: 0.45, curveness: 0.05 },
      emphasis: {
        focus: 'adjacency',
        lineStyle: { width: 3, opacity: 0.9 },
      },
      data: graphNodes,
      links: graphEdges,
    }],
  }, true);
}

// ═══════════════════════════════════════════════════════════
// Biography 人物传记
// ═══════════════════════════════════════════════════════════

let bioData = { cards: [], alias_index: {} };

async function bioFetch() {
  if (!currentPersona) {
    document.getElementById('bioCardList').innerHTML = '<div style="color:var(--text-2);padding:12px">请选择人格</div>';
    return;
  }
  try {
    bioData = await get(pApi('/biography'));
    bioRender();
  } catch (e) {
    document.getElementById('bioCardList').innerHTML = `<div style="color:var(--danger);padding:12px">加载失败：${e.message}</div>`;
  }
}

function bioRefresh() { bioFetch(); }

function bioRender() {
  const cards = bioData.cards || [];
  const aliasIndex = bioData.alias_index || {};
  const totalAliases = Object.keys(aliasIndex).length;
  const lastUpdate = cards.length > 0
    ? cards.reduce((latest, c) => c.last_updated_at > latest ? c.last_updated_at : latest, '').slice(0, 19).replace('T', ' ')
    : '—';

  document.getElementById('bioTotal').textContent = cards.length;
  document.getElementById('bioDistilled').textContent = cards.reduce((s, c) => s + (c.distilled_points || []).length, 0);
  document.getElementById('bioAliasCount').textContent = totalAliases;
  document.getElementById('bioLastUpdate').textContent = lastUpdate;

  const list = document.getElementById('bioCardList');
  if (cards.length === 0) {
    list.innerHTML = '<div style="color:var(--text-2);padding:12px;text-align:center">暂无传记卡</div>';
  } else {
    list.innerHTML = cards.map(c => {
      const anchors = (c.identity_anchors || []).slice(0, 3).join(' · ');
      const bioPreview = (c.short_bio || '暂无传记').slice(0, 80);
      const rels = (c.relationships || []).slice(0, 2).map(r => r.fact_hint || r.target_name || '').filter(Boolean).join('；');
      const pendingN = (c.pending_messages || []).length;
      const distilledN = (c.distilled_points || []).length;
      return `<div class="card" style="cursor:pointer" onclick="bioOpenCard('${c.user_id.replace(/'/g, "\\'")}')">
        <div style="display:flex;justify-content:space-between;align-items:start;gap:12px">
          <div style="flex:1">
            <div style="font-weight:600;font-size:15px;margin-bottom:4px">${c.name || c.user_id}</div>
            ${anchors ? `<div style="font-size:12px;color:var(--primary);margin-bottom:4px">${anchors}</div>` : ''}
            ${bioPreview ? `<div style="font-size:13px;color:var(--text-2);margin-bottom:4px">${bioPreview}...</div>` : ''}
            ${rels ? `<div style="font-size:12px;color:var(--text-3)">关系：${rels}</div>` : ''}
            <div style="font-size:11px;color:var(--text-3);margin-top:2px">
              待处理消息：${pendingN} 条 · 蒸馏要点：${distilledN} 条
            </div>
          </div>
          <div style="text-align:right;flex-shrink:0">
            <div style="font-size:11px;color:var(--text-3)">${c.user_id}</div>
            <div style="font-size:11px;color:var(--text-3);margin-top:2px">更新：${(c.last_updated_at||'').slice(0,10)||'—'}</div>
          </div>
        </div>
      </div>`;
    }).join('');
  }

  const aliasEl = document.getElementById('bioAliasSection');
  const aliases = Object.entries(aliasIndex);
  if (aliases.length === 0) {
    aliasEl.innerHTML = '<div style="text-align:center;padding:12px;color:var(--text-2)">暂无别名条目</div>';
  } else {
    aliasEl.innerHTML = `
      <table style="width:100%;border-collapse:collapse;font-size:13px">
        <thead><tr style="border-bottom:1px solid var(--border)">
          <th style="text-align:left;padding:8px 6px">别名</th>
          <th style="text-align:left;padding:8px 6px">指向</th>
          <th style="text-align:left;padding:8px 6px">权重</th>
          <th style="text-align:left;padding:8px 6px">来源</th>
          <th style="text-align:right;padding:8px 6px">操作</th>
        </tr></thead>
        <tbody>
          ${aliases.map(([alias, entries]) => entries.map((e, i) => `
            <tr style="border-bottom:1px solid var(--border);${i===0 ? '' : 'opacity:0.7'}">
              <td style="padding:6px">${i===0 ? `<strong>${alias}</strong>` : ''}</td>
              <td style="padding:6px">${e.user_name || e.user_id}</td>
              <td style="padding:6px">${e.weight.toFixed(2)}</td>
              <td style="padding:6px;font-size:11px;color:var(--text-3)">${e.source}</td>
              <td style="padding:6px;text-align:right">
                <button class="btn btn-sm btn-outline" onclick="bioRemoveAlias('${alias.replace(/'/g, "\\'")}','${e.user_id.replace(/'/g, "\\'")}')" style="font-size:11px;padding:2px 8px">删除</button>
              </td>
            </tr>
          `).join('')).join('')}
        </tbody>
      </table>
      <div style="margin-top:12px;display:flex;gap:8px;align-items:end">
        <div><label style="font-size:12px;color:var(--text-2)">别名</label><input id="bioNewAlias" placeholder="e.g. 狗福" style="width:100px"></div>
        <div><label style="font-size:12px;color:var(--text-2)">user_id</label><input id="bioNewAliasUid" placeholder="e.g. qq_123" style="width:120px"></div>
        <div><label style="font-size:12px;color:var(--text-2)">显示名</label><input id="bioNewAliasName" placeholder="e.g. 临雀" style="width:100px"></div>
        <div><button class="btn btn-sm btn-primary" onclick="bioAddAlias()" style="margin-top:auto">添加</button></div>
      </div>
    `;
  }
}

async function bioRemoveAlias(alias, userId) {
  if (!confirm(`确定要删除 "${alias}" → ${userId} 的别名映射吗？`)) return;
  try {
    await post(pApi('/biography/aliases'), { action: 'delete', alias, user_id: userId });
    toast('别名已删除');
    bioFetch();
  } catch (e) { toast('删除失败: ' + e.message, 'error'); }
}

async function bioAddAlias() {
  const alias = document.getElementById('bioNewAlias').value.trim();
  const uid = document.getElementById('bioNewAliasUid').value.trim();
  const name = document.getElementById('bioNewAliasName').value.trim();
  if (!alias || !uid) { toast('请填写别名和user_id', 'error'); return; }
  try {
    await post(pApi('/biography/aliases'), { action: 'add', alias, user_id: uid, user_name: name });
    toast('别名已添加');
    bioFetch();
  } catch (e) { toast('添加失败: ' + e.message, 'error'); }
}

function bioOpenCard(userId) {
  const card = (bioData.cards || []).find(c => c.user_id === userId);
  if (!card) return;
  document.getElementById('bioModalTitle').textContent = `传记详情 — ${card.name || card.user_id}`;
  const relsLines = (card.relationships || []).map(r =>
    `<div style="font-size:13px;color:var(--text);margin-bottom:2px">· ${r.target_name || '?'}：${r.fact_hint || '—'}</div>`
  ).join('');
  const distilledLines = (card.distilled_points || []).length > 0
    ? (card.distilled_points || []).map(p => `<div style="font-size:12px;color:var(--text-2);margin-bottom:2px;padding-left:8px;border-left:2px solid var(--success)">${p}</div>`).join('')
    : '<div style="font-size:12px;color:var(--text-3)">暂无蒸馏要点</div>';
  const pendingLines = (card.pending_messages || []).length > 0
    ? (card.pending_messages || []).map(m => `<div style="font-size:11px;color:var(--text-3);margin-bottom:1px">${m}</div>`).join('')
    : '<div style="font-size:12px;color:var(--text-3)">暂无待处理消息</div>';
  const lastDistill = card.last_distill_at ? card.last_distill_at.slice(0, 19).replace('T', ' ') : '—';
  document.getElementById('bioModalBody').innerHTML = `
    <div style="display:flex;flex-direction:column;gap:14px">
      <div style="display:flex;gap:24px;flex-wrap:wrap">
        <div><span style="font-size:12px;color:var(--text-3)">User ID</span><div style="font-size:13px;color:var(--text);font-family:monospace">${card.user_id}</div></div>
        <div><span style="font-size:12px;color:var(--text-3)">别名</span><div style="font-size:13px;color:var(--text)">${(card.aliases||[]).join(', ') || '—'}</div></div>
        <div><span style="font-size:12px;color:var(--text-3)">最近更新</span><div style="font-size:13px;color:var(--text)">${(card.last_updated_at||'').slice(0,19).replace('T',' ')||'—'}</div></div>
        <div><span style="font-size:12px;color:var(--text-3)">最近蒸馏</span><div style="font-size:13px;color:var(--text)">${lastDistill}</div></div>
      </div>
      ${card.identity_anchors && card.identity_anchors.length > 0 ? `
        <div>
          <div style="font-weight:600;font-size:13px;color:var(--primary);margin-bottom:4px">身份锚点</div>
          <div style="font-size:13px;color:var(--text)">${card.identity_anchors.map(a => `<span style="background:var(--bg-2);padding:2px 8px;border-radius:4px;margin-right:4px">${a}</span>`).join('')}</div>
        </div>
      ` : ''}
      <div>
        <div style="font-weight:600;font-size:13px;margin-bottom:4px">人物传记</div>
        <div style="font-size:13px;color:var(--text);line-height:1.6;white-space:pre-wrap">${card.short_bio || '暂无传记'}</div>
      </div>
      ${relsLines ? `<div><div style="font-weight:600;font-size:13px;margin-bottom:4px">关系锚点</div>${relsLines}</div>` : ''}
      <div>
        <div style="font-weight:600;font-size:13px;margin-bottom:4px">
          蒸馏要点（${(card.distilled_points||[]).length} 条）
          <span style="font-size:11px;color:var(--text-3);font-weight:normal"> — 层1凝练输出，攒够后触发层2传记更新</span>
        </div>
        ${distilledLines}
      </div>
      <div>
        <div style="font-weight:600;font-size:13px;margin-bottom:4px">
          待处理消息（${(card.pending_messages||[]).length} 条）
          <span style="font-size:11px;color:var(--text-3);font-weight:normal"> — 原始群聊消息攒批，等待蒸馏</span>
        </div>
        <div style="max-height:150px;overflow-y:auto;background:var(--bg-2);border-radius:8px;padding:8px">
          ${pendingLines}
        </div>
      </div>
    </div>
  `;
  document.getElementById('bioModal').style.display = 'flex';
}

function bioCloseModal() {
  document.getElementById('bioModal').style.display = 'none';
}

function bioPersonaSelectInit() {
  const el = document.getElementById('bioPersonaSelect');
  if (!el) return;

  const sel = document.createElement('select');
  sel.style.cssText = 'padding:4px 8px;border-radius:6px;background:var(--bg-2);color:var(--text);border:1px solid var(--border);font-size:13px;min-width:120px';
  sel.innerHTML = '<option value="">选择人格</option>';
  el.innerHTML = '';
  el.appendChild(sel);

  function populate() {
    sel.innerHTML = '<option value="">选择人格</option>';
    personas.forEach(p => {
      sel.innerHTML += `<option value="${p.name}">${p.persona_name || p.name}</option>`;
    });
    if (currentPersona) sel.value = currentPersona;
  }
  populate();

  sel.onchange = function () {
    selectPersona(this.value);
    bioFetch();
  };
}

// ── Page Loader Registrations (analytics) ─────────────
registerPageLoader('token-tracker', { init: loadTokenTracker, refresh: ttLoadData });
registerPageLoader('cognition', { init: loadCognition });
registerPageLoader('diary', { init: diaryLoadData });
registerPageLoader('users', { init: loadUsers });
registerPageLoader('glossary', { init: loadGlossary });
registerPageLoader('memory-viz', { init: loadMemoryViz });
registerPageLoader('biography', {
  init: async function () {
    bioPersonaSelectInit();
    if (currentPersona) bioFetch();
  },
  refresh: async function () { if (currentPersona) bioFetch(); }
});
