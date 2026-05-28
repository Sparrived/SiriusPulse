import { store } from '../store.js';
import { get, post, navTo, selectPersona } from '../app.js';
import { toast, animateNumber, formatHeartbeat, $ } from '../components.js';
import { GlobeRenderer } from './globe-renderer.js';

// ==================== 应用状态 ====================

let globe = null;
let currentPersona = null;
let personasData = [];

// 人格图斑颜色配置
const PERSONA_COLORS = [
  { color: 'rgba(76, 154, 255, 0.15)', borderColor: 'rgba(76, 154, 255, 0.6)' },
  { color: 'rgba(63, 185, 80, 0.15)', borderColor: 'rgba(63, 185, 80, 0.6)' },
  { color: 'rgba(163, 113, 247, 0.15)', borderColor: 'rgba(163, 113, 247, 0.6)' },
  { color: 'rgba(210, 153, 34, 0.15)', borderColor: 'rgba(210, 153, 34, 0.6)' },
  { color: 'rgba(248, 81, 73, 0.15)', borderColor: 'rgba(248, 81, 73, 0.6)' },
  { color: 'rgba(76, 154, 255, 0.15)', borderColor: 'rgba(76, 154, 255, 0.6)' },
];

// ==================== 初始化 ====================

export async function init(container) {
  initGlobe();
  await Promise.all([loadStats(), loadPersonas()]);
  startAnimationLoop();
  bindEvents();
}

// 初始化球体
function initGlobe() {
  const canvas = document.getElementById('planetGlobe');
  if (!canvas) return;

  canvas.width = 400;
  canvas.height = 400;

  globe = new GlobeRenderer(canvas);
}

// ==================== 动画循环 ====================

function startAnimationLoop() {
  if (!globe) return;

  const animate = () => {
    globe.render();
    updatePersonaCards();
    requestAnimationFrame(animate);
  };
  animate();
}

// ==================== 人格卡片更新 ====================

function updatePersonaCards() {
  if (!globe || !personasData.length) return;

  const container = $('personaCardsContainer');
  if (!container) return;

  const hero = document.querySelector('.planet-hero');
  if (!hero) return;

  const heroRect = hero.getBoundingClientRect();
  const centerX = heroRect.width / 2;
  const centerY = heroRect.height / 2;

  // 确保有足够的卡片元素
  while (container.children.length < personasData.length) {
    const card = document.createElement('div');
    card.className = 'persona-orbit-card';
    card.innerHTML = `
      <div class="persona-orbit-name"></div>
      <div class="persona-orbit-status">
        <span class="status-dot"></span>
        <span class="status-text"></span>
      </div>
    `;
    // 点击卡片 = 选中人格 + 更新右侧面板（与点击图斑效果一致）
    card.addEventListener('click', () => {
      const idx = Array.from(container.children).indexOf(card);
      const persona = personasData[idx];
      if (persona) {
        selectPersona(persona.name);
        updatePersonaPanel(idx);
        globe.selectedSpot = idx;
      }
    });
    container.appendChild(card);
  }

  // 更新卡片位置和内容
  personasData.forEach((persona, index) => {
    const spot = globe.spots[index];
    if (!spot) return;

    const card = container.children[index];
    if (!card) return;

    const nameEl = card.querySelector('.persona-orbit-name');
    const statusDot = card.querySelector('.status-dot');
    const statusText = card.querySelector('.status-text');

    nameEl.textContent = persona.persona_name || persona.name;

    const isRunning = persona.running;
    statusDot.className = `status-dot ${isRunning ? 'running' : ''}`;
    statusText.textContent = isRunning ? '运行中' : '已停止';
    statusText.style.color = isRunning ? 'var(--success)' : 'var(--text-3)';

    // 卡片位置（固定布局，均匀分布在球体周围）
    const cardAngle = ((index / personasData.length) * 360 - 90) * Math.PI / 180;
    const cardRadius = 265;
    const cardX = centerX + Math.cos(cardAngle) * cardRadius;
    const cardY = centerY + Math.sin(cardAngle) * cardRadius;

    card.style.left = `${cardX}px`;
    card.style.top = `${cardY}px`;
    card.style.transform = 'translate(-50%, -50%)';

    // 判断图斑是否可见
    const isVisible = spot.isVisible;

    if (isVisible) {
      card.classList.remove('fade-out');
      card.classList.add('fade-in');
      card.style.pointerEvents = 'auto';
    } else {
      card.classList.remove('fade-in');
      card.classList.add('fade-out');
      card.style.pointerEvents = 'none';
    }

    // 悬停高亮
    if (globe.hoveredSpot === index) {
      card.classList.add('highlighted');
    } else {
      card.classList.remove('highlighted');
    }

    // 选中态样式
    if (globe.selectedSpot === index) {
      card.classList.add('selected');
    } else {
      card.classList.remove('selected');
    }
  });
}

// ==================== 事件绑定 ====================

function bindEvents() {
  const canvas = document.getElementById('planetGlobe');
  if (!canvas || !globe) return;

  // 鼠标事件
  canvas.addEventListener('mousedown', (e) => {
    const rect = canvas.getBoundingClientRect();
    globe.onMouseDown(e.clientX - rect.left, e.clientY - rect.top);
    canvas.style.cursor = 'grabbing';
  });

  canvas.addEventListener('mousemove', (e) => {
    const rect = canvas.getBoundingClientRect();
    globe.onMouseMove(e.clientX - rect.left, e.clientY - rect.top);

    canvas.style.cursor = globe.hasHoveredSpot() ? 'pointer' : (globe.isDragging ? 'grabbing' : 'grab');

    // 悬停时停止自动旋转
    if (globe.hasHoveredSpot()) {
      globe.autoRotate = false;
      // 更新右侧面板显示悬停的人格信息
      updatePersonaPanel(globe.hoveredSpot);
    } else if (!globe.isDragging) {
      globe.autoRotate = true;
    }
  });

  canvas.addEventListener('mouseup', () => {
    globe.onMouseUp();
    canvas.style.cursor = 'grab';
  });

  canvas.addEventListener('mouseleave', () => {
    globe.onMouseUp();
    globe.hoveredSpot = -1;
    globe.autoRotate = true;
    canvas.style.cursor = 'grab';
  });

  // 点击事件
  canvas.addEventListener('click', (e) => {
    if (globe.hasHoveredSpot()) {
      const persona = personasData[globe.hoveredSpot];
      if (persona) {
        selectPersona(persona.name);
        updatePersonaPanel(globe.hoveredSpot);
        globe.selectedSpot = globe.hoveredSpot;
      }
    } else {
      // 点击空白区域取消选中
      globe.selectedSpot = -1;
    }
  });

  // 添加人格按钮
  const addBtn = $('addPersonaBtn');
  if (addBtn) {
    addBtn.addEventListener('click', () => navTo('create-persona'));
  }

  // 配置按钮
  const configBtn = $('panelConfigBtn');
  if (configBtn) {
    configBtn.addEventListener('click', () => {
      if (currentPersona) {
        navTo('persona', currentPersona.name);
      }
    });
  }

  // 启动/停止按钮
  const startBtn = $('panelStartBtn');
  const stopBtn = $('panelStopBtn');

  if (startBtn) {
    startBtn.addEventListener('click', async () => {
      if (!currentPersona) return;
      try {
        const res = await post(`/personas/${currentPersona.name}/start`, {});
        if (res.success) {
          toast(`${currentPersona.name} 已启动`, 'success');
          await loadPersonas();
          updatePersonaPanel(personasData.findIndex(p => p.name === currentPersona.name));
        } else {
          toast(res.error || '启动失败', 'error');
        }
      } catch (e) {
        toast('启动失败', 'error');
      }
    });
  }

  if (stopBtn) {
    stopBtn.addEventListener('click', async () => {
      if (!currentPersona) return;
      try {
        const res = await post(`/personas/${currentPersona.name}/stop`, {});
        if (res.success) {
          toast(`${currentPersona.name} 已停止`, 'success');
          await loadPersonas();
          updatePersonaPanel(personasData.findIndex(p => p.name === currentPersona.name));
        } else {
          toast(res.error || '停止失败', 'error');
        }
      } catch (e) {
        toast('停止失败', 'error');
      }
    });
  }
}

// 更新右侧面板
function updatePersonaPanel(index) {
  const persona = personasData[index];
  if (!persona) return;

  currentPersona = persona;

  $('panelPersonaName').textContent = persona.persona_name || persona.name;

  const isRunning = persona.running;
  const statusEl = $('panelStatus');
  statusEl.innerHTML = `
    <span class="status-dot ${isRunning ? 'running' : ''}"></span>
    <span style="color:${isRunning ? 'var(--success)' : 'var(--text-3)'}">${isRunning ? '运行中' : '已停止'}</span>
  `;

  $('panelMeta').textContent = persona.persona_summary || '暂无描述';

  // 更新按钮
  $('panelStartBtn').style.display = isRunning ? 'none' : 'inline-flex';
  $('panelStopBtn').style.display = isRunning ? 'inline-flex' : 'none';
}

// ==================== 数据加载 ====================

async function loadStats() {
  try {
    const [tokenRes, telemetryRes] = await Promise.all([
      get('/tokens').catch(() => ({})),
      get('/telemetry').catch(() => ({})),
    ]);

    const s = tokenRes.summary || {};
    const avg = tokenRes.response_avg || {};
    const personas = store.personas || [];
    const running = personas.filter(p => p.running).length;
    const skills = telemetryRes.skills || {};
    const totalSkillCalls = telemetryRes.total_calls || 0;

    if ($('dsPersonas')) animateNumber($('dsPersonas'), personas.length);
    if ($('dsRunning')) animateNumber($('dsRunning'), running);
    if ($('dsCalls')) animateNumber($('dsCalls'), s.total_calls || 0);
    if ($('dsTokens')) animateNumber($('dsTokens'), s.total_tokens || 0);
    if ($('dsAvgTokens')) animateNumber($('dsAvgTokens'), avg.avg_total_tokens || 0);
    if ($('dsSkillCalls')) animateNumber($('dsSkillCalls'), totalSkillCalls);

    const skillNames = Object.keys(skills);
    if (skillNames.length > 0) {
      let totalSuccess = 0, totalCount = 0;
      skillNames.forEach(name => {
        totalSuccess += (skills[name].success_rate || 0) * skills[name].calls;
        totalCount += skills[name].calls;
      });
      const avgRate = totalCount > 0 ? Math.round(totalSuccess / totalCount) : 0;
      if ($('dsSkillRate')) $('dsSkillRate').textContent = `${avgRate}%`;
    }

    if ($('dsHeartbeat') && personas.length > 0) {
      const latest = personas.reduce((a, b) => (a.heartbeat_at || 0) > (b.heartbeat_at || 0) ? a : b);
      $('dsHeartbeat').textContent = formatHeartbeat(latest.heartbeat_at);
    }
  } catch (e) {
    console.error('统计数据加载失败:', e);
  }
}

async function loadPersonas() {
  try {
    const res = await get('/personas');
    personasData = res.personas || [];
    store.personas = personasData;

    // 初始化人格图斑
    initPersonaSpots();
  } catch (e) {
    console.error('人格数据加载失败:', e);
  }
}

// 初始化人格图斑
function initPersonaSpots() {
  if (!globe || !personasData.length) return;

  const spots = personasData.map((persona, index) => {
    const colorConfig = PERSONA_COLORS[index % PERSONA_COLORS.length];
    // 更分散的分布
    const lng = (index * 137.5) % 360; // 黄金角分布
    const lat = 30 * Math.sin(index * 2.5) + 10 * Math.cos(index * 1.7); // 更大的纬度变化

    return {
      x: (lng / 360) * 2048,
      lat: lat,
      label: persona.persona_name || persona.name,
      size: 45,
      color: colorConfig.color,
      borderColor: colorConfig.borderColor,
    };
  });

  globe.initSpots(spots);
}

// ==================== 清理 ====================

export function destroy() {
  if (globe) {
    globe.destroy();
    globe = null;
  }
}
