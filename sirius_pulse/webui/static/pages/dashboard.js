import { store } from '../store.js';
import { get, post, navTo, selectPersona } from '../app.js';
import { toast, animateNumber, formatHeartbeat } from '../components.js';
import { GlobeRenderer } from './globe-renderer.js';
import { createRealtimeRefresh } from './realtime.js';
import { createScopedPage } from '../page-context.js';

const scopedPage = createScopedPage();

export function dispose() {
  destroy();
  scopedPage.use(null, null);
}
const $ = scopedPage.$;

// ==================== 动态星空渲染器 ====================

class StarfieldRenderer {
  constructor(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.stars = [];
    this.shootingStars = [];
    this.nebulae = [];
    this.animId = null;
    this.time = 0;

    this.resize();
    this.init();
    this._onResize = () => {
      this.resize();
      this.init();
    };
    scopedPage.on(window, 'resize', this._onResize);
  }

  resize() {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    this.canvas.width = window.innerWidth * dpr;
    this.canvas.height = window.innerHeight * dpr;
    this.canvas.style.width = `${window.innerWidth}px`;
    this.canvas.style.height = `${window.innerHeight}px`;
    this.ctx.scale(dpr, dpr);
    this.w = window.innerWidth;
    this.h = window.innerHeight;
  }

  init() {
    this.stars = [];
    this.nebulae = [];

    // 按层级生成星星：远（小暗）→ 近（大亮）
    const layers = [
      { count: 180, radiusMin: 0.3, radiusMax: 0.8, opacityMin: 0.15, opacityMax: 0.4 },
      { count: 100, radiusMin: 0.6, radiusMax: 1.4, opacityMin: 0.3, opacityMax: 0.7 },
      { count: 50, radiusMin: 1.0, radiusMax: 2.0, opacityMin: 0.5, opacityMax: 0.9 },
      { count: 15, radiusMin: 1.8, radiusMax: 2.8, opacityMin: 0.7, opacityMax: 1.0 },
    ];

    // 星星色温偏移：暖白 / 冷白 / 淡蓝 / 淡黄
    const tints = [
      [255, 255, 255],
      [200, 220, 255],
      [255, 245, 230],
      [180, 210, 255],
      [255, 240, 220],
    ];

    for (const layer of layers) {
      for (let i = 0; i < layer.count; i++) {
        const tint = tints[Math.floor(Math.random() * tints.length)];
        const baseR = layer.radiusMin + Math.random() * (layer.radiusMax - layer.radiusMin);
        this.stars.push({
          x: Math.random() * this.w,
          y: Math.random() * this.h,
          radius: baseR,
          baseOpacity: layer.opacityMin + Math.random() * (layer.opacityMax - layer.opacityMin),
          // 闪烁为核心：差异化的速度和幅度，每颗星有自己的呼吸节奏
          twinkleSpeed: 0.3 + Math.random() * 3.0,
          twinklePhase: Math.random() * Math.PI * 2,
          twinkleAmount: 0.35 + Math.random() * 0.55,
          tint,
          isBright: baseR > 1.6,
        });
      }
    }

    // 星云光晕
    const nebulaColors = [
      'rgba(76, 154, 255, 0.012)',
      'rgba(163, 113, 247, 0.010)',
      'rgba(63, 185, 80, 0.008)',
      'rgba(255, 61, 143, 0.006)',
    ];
    for (let i = 0; i < 4; i++) {
      this.nebulae.push({
        x: Math.random() * this.w,
        y: Math.random() * this.h,
        radius: 200 + Math.random() * 300,
        color: nebulaColors[i % nebulaColors.length],
        driftX: (Math.random() - 0.5) * 0.08,
        driftY: (Math.random() - 0.5) * 0.06,
        phase: Math.random() * Math.PI * 2,
        pulseSpeed: 0.3 + Math.random() * 0.4,
      });
    }
  }

  spawnShootingStar() {
    // 随机概率生成流星（低频率，偶尔划过更有意境）
    if (Math.random() > 0.0015) return;
    // 从右上方向左下方飞
    const startX = this.w * 0.3 + Math.random() * this.w * 0.7;
    const startY = Math.random() * this.h * 0.35;
    const angle = Math.PI / 5 + Math.random() * Math.PI / 8;
    const speed = 5 + Math.random() * 8;
    const length = 60 + Math.random() * 100;

    this.shootingStars.push({
      x: startX,
      y: startY,
      angle,
      speed,
      length,
      opacity: 1,
      width: 1.2 + Math.random() * 1.2,
      // 流星头部颜色（白→蓝白渐变）
      hue: 200 + Math.random() * 40,
    });
  }

  render() {
    const ctx = this.ctx;
    const w = this.w;
    const h = this.h;
    this.time += 0.016;

    // 清空画布
    ctx.clearRect(0, 0, w, h);

    // 绘制深空背景渐变
    const bg = ctx.createRadialGradient(w / 2, h / 2, 0, w / 2, h / 2, Math.max(w, h) * 0.75);
    bg.addColorStop(0, '#0d1120');
    bg.addColorStop(0.5, '#080c16');
    bg.addColorStop(1, '#040610');
    ctx.fillStyle = bg;
    ctx.fillRect(0, 0, w, h);

    // 绘制星云光晕
    for (const neb of this.nebulae) {
      const pulse = 0.8 + 0.2 * Math.sin(this.time * neb.pulseSpeed + neb.phase);
      const r = neb.radius * pulse;
      neb.x += neb.driftX;
      neb.y += neb.driftY;
      // 边界回绕
      if (neb.x < -r) neb.x = w + r;
      if (neb.x > w + r) neb.x = -r;
      if (neb.y < -r) neb.y = h + r;
      if (neb.y > h + r) neb.y = -r;

      const grad = ctx.createRadialGradient(neb.x, neb.y, 0, neb.x, neb.y, r);
      grad.addColorStop(0, neb.color);
      grad.addColorStop(1, 'transparent');
      ctx.fillStyle = grad;
      ctx.fillRect(neb.x - r, neb.y - r, r * 2, r * 2);
    }

    // 绘制星星
    for (const star of this.stars) {
      const twinkle = Math.sin(this.time * star.twinkleSpeed + star.twinklePhase);
      const opacity = star.baseOpacity * (1 + twinkle * star.twinkleAmount);
      const clampedOpacity = Math.max(0, Math.min(1, opacity));

      const [r, g, b] = star.tint;

      // 亮星额外光晕
      if (star.isBright) {
        const glowRadius = star.radius * 4;
        const glow = ctx.createRadialGradient(star.x, star.y, 0, star.x, star.y, glowRadius);
        glow.addColorStop(0, `rgba(${r}, ${g}, ${b}, ${clampedOpacity * 0.25})`);
        glow.addColorStop(1, 'transparent');
        ctx.beginPath();
        ctx.arc(star.x, star.y, glowRadius, 0, Math.PI * 2);
        ctx.fillStyle = glow;
        ctx.fill();

        // 十字光芒
        const rayLen = star.radius * 3 * (0.7 + twinkle * 0.3);
        const rayOpacity = clampedOpacity * 0.35;
        ctx.strokeStyle = `rgba(${r}, ${g}, ${b}, ${rayOpacity})`;
        ctx.lineWidth = 0.5;
        ctx.beginPath();
        ctx.moveTo(star.x - rayLen, star.y);
        ctx.lineTo(star.x + rayLen, star.y);
        ctx.stroke();
        ctx.beginPath();
        ctx.moveTo(star.x, star.y - rayLen);
        ctx.lineTo(star.x, star.y + rayLen);
        ctx.stroke();
      }

      // 星星本体
      ctx.beginPath();
      ctx.arc(star.x, star.y, star.radius, 0, Math.PI * 2);
      ctx.fillStyle = `rgba(${r}, ${g}, ${b}, ${clampedOpacity})`;
      ctx.fill();
    }

    // 流星
    this.spawnShootingStar();
    this.shootingStars = this.shootingStars.filter(s => s.opacity > 0.02);
    for (const s of this.shootingStars) {
      ctx.save();
      ctx.translate(s.x, s.y);
      ctx.rotate(s.angle);

      // 流星尾巴渐变
      const tailGrad = ctx.createLinearGradient(0, 0, -s.length, 0);
      tailGrad.addColorStop(0, `hsla(${s.hue}, 80%, 90%, ${s.opacity})`);
      tailGrad.addColorStop(0.3, `hsla(${s.hue}, 60%, 70%, ${s.opacity * 0.5})`);
      tailGrad.addColorStop(1, 'transparent');

      ctx.beginPath();
      ctx.moveTo(0, 0);
      ctx.lineTo(-s.length, 0);
      ctx.strokeStyle = tailGrad;
      ctx.lineWidth = s.width;
      ctx.lineCap = 'round';
      ctx.stroke();

      // 流星头部亮点
      const headGrad = ctx.createRadialGradient(0, 0, 0, 0, 0, s.width * 3);
      headGrad.addColorStop(0, `hsla(${s.hue}, 100%, 95%, ${s.opacity})`);
      headGrad.addColorStop(1, 'transparent');
      ctx.fillStyle = headGrad;
      ctx.beginPath();
      ctx.arc(0, 0, s.width * 3, 0, Math.PI * 2);
      ctx.fill();

      ctx.restore();

      s.x += Math.cos(s.angle) * s.speed;
      s.y += Math.sin(s.angle) * s.speed;
      s.opacity -= 0.012;
    }
  }

  destroy() {
    if (this.animId) {
      cancelAnimationFrame(this.animId);
      this.animId = null;
    }
    window.removeEventListener('resize', this._onResize);
  }
}

// ==================== 应用状态 ====================

let globe = null;
let starfield = null;
let currentPersona = null;
const realtime = createRealtimeRefresh(refreshDashboardRealtime, {
  resources: ['dashboard', 'monitoring', 'personas', 'tokens', 'cognition', 'skill-history', 'conversations'],
  debounceMs: 700,
  personaScoped: false,
});
let personasData = [];
let onPersonaFocus = null;
let panelPersonaIndex = -1;

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

export async function init(container, params = {}) {
  scopedPage.use(params?.ctx, container);
  initStarfield();
  initGlobe();
  await loadPersonas();
  await loadStats();
  startAnimationLoop();
  bindEvents();
  realtime.start();
}

async function refreshDashboardRealtime() {
  await loadPersonas();
  await loadStats();
  if (currentPersona) {
    const index = personasData.findIndex(p => p.name === currentPersona.name);
    if (index >= 0) updatePersonaPanel(index);
  }
}

// 初始化星空背景
function initStarfield() {
  const canvas = $('starfieldCanvas');
  if (!canvas) return;
  starfield = new StarfieldRenderer(canvas);
}

// 初始化球体
function initGlobe() {
  const canvas = $('planetGlobe');
  if (!canvas) return;

  // 增大 canvas 尺寸，为大气层效果留出空间
  canvas.width = 480;
  canvas.height = 480;

  globe = new GlobeRenderer(canvas);
}

// ==================== 动画循环 ====================

function startAnimationLoop() {
  const animate = () => {
    // 星空背景始终渲染（无论球体是否存在）
    if (starfield) {
      starfield.render();
    }
    if (globe) {
      globe.render();
      updatePersonaCards();
    }
    requestAnimationFrame(animate);
  };
  animate();
}

// ==================== 人格卡片更新 ====================

function updatePersonaCards() {
  if (!globe || !personasData.length) return;

  const container = $('personaCardsContainer');
  if (!container) return;

  const hero = scopedPage.query('.planet-hero');
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
    card.addEventListener('click', async () => {
      const idx = Array.from(container.children).indexOf(card);
      const persona = personasData[idx];
      if (persona) {
        await selectPersona(persona.name);
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
  const canvas = $('planetGlobe');
  if (!canvas || !globe) return;

  // 顶栏下拉框选择人格时，联动球体旋转
  onPersonaFocus = (e) => {
    const name = e.detail;
    const idx = personasData.findIndex(p => p.name === name);
    if (idx >= 0 && globe) {
      globe.focusSpot(idx);
      updatePersonaPanel(idx);
    }
  };
  scopedPage.on(window, 'persona:focus', onPersonaFocus);

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
  canvas.addEventListener('click', async (e) => {
    if (globe.hasHoveredSpot()) {
      const persona = personasData[globe.hoveredSpot];
      if (persona) {
        await selectPersona(persona.name);
        updatePersonaPanel(globe.hoveredSpot);
        globe.selectedSpot = globe.hoveredSpot;
      }
    } else {
      // 点击空白区域取消选中
      globe.selectedSpot = -1;
      // 隐藏配置按钮
      if ($('panelConfigBtn')) $('panelConfigBtn').style.display = 'none';
      if ($('panelStartBtn')) $('panelStartBtn').style.display = 'none';
      if ($('panelStopBtn')) $('panelStopBtn').style.display = 'none';
    }
  });

  // 添加人格按钮
  const addBtn = $('addPersonaBtn');
  if (addBtn) {
    addBtn.addEventListener('click', () => navTo('create-persona'));
  }

  // 关闭程序按钮
  const shutdownBtn = $('shutdownBtn');
  if (shutdownBtn) {
    shutdownBtn.addEventListener('click', async () => {
      if (!confirm('确定要关闭整个程序吗？所有服务将停止。')) return;
      try {
        await post('/shutdown', {});
        toast('正在关闭程序...', 'info');
        // 等待服务停止后显示提示
        scopedPage.timeout(() => {
          document.body.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100vh;color:var(--text-2);font-size:18px;">程序已关闭，可以关闭此页面</div>';
        }, 2000);
      } catch (e) {
    if (e?.name === 'AbortError') return;
        // 请求可能因服务关闭而失败，这是正常的
        scopedPage.timeout(() => {
          document.body.innerHTML = '<div style="display:flex;align-items:center;justify-content:center;height:100vh;color:var(--text-2);font-size:18px;">程序已关闭，可以关闭此页面</div>';
        }, 1500);
      }
    });
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
        const res = await post(`/persona/start`, {});
        if (res.success) {
          toast(`${currentPersona.name} 已启动`, 'success');
          await loadPersonas();
          panelPersonaIndex = -1;
          updatePersonaPanel(personasData.findIndex(p => p.name === currentPersona.name));
        } else {
          toast(res.error || '启动失败', 'error');
        }
      } catch (e) {
    if (e?.name === 'AbortError') return;
        toast('启动失败', 'error');
      }
    });
  }

  if (stopBtn) {
    stopBtn.addEventListener('click', async () => {
      if (!currentPersona) return;
      try {
        const res = await post(`/persona/stop`, {});
        if (res.success) {
          toast(`${currentPersona.name} 已停止`, 'success');
          await loadPersonas();
          panelPersonaIndex = -1;
          updatePersonaPanel(personasData.findIndex(p => p.name === currentPersona.name));
        } else {
          toast(res.error || '停止失败', 'error');
        }
      } catch (e) {
    if (e?.name === 'AbortError') return;
        toast('停止失败', 'error');
      }
    });
  }

  // 统计项点击涟漪效果 + 可导航项跳转
  scopedPage.$$('.stat-item').forEach(item => {
    item.addEventListener('click', (e) => {
      const ripple = document.createElement('span');
      ripple.className = 'stat-ripple';
      const rect = item.getBoundingClientRect();
      const size = Math.max(rect.width, rect.height) * 2;
      ripple.style.width = ripple.style.height = `${size}px`;
      ripple.style.left = `${e.clientX - rect.left - size / 2}px`;
      ripple.style.top = `${e.clientY - rect.top - size / 2}px`;
      item.appendChild(ripple);
      scopedPage.timeout(() => ripple.remove(), 500);

      // 带 data-nav-page 属性的统计项点击后跳转对应页面
      const targetPage = item.dataset.navPage;
      if (targetPage) {
        navTo(targetPage);
      }
    });
  });

  // Embedding 状态点击 → 弹窗
  const embeddingItem = $('dsEmbedding')?.closest('.stat-item');
  if (embeddingItem) {
    embeddingItem.addEventListener('click', () => showEmbeddingModal());
  }
}

// ==================== Embedding 气泡弹窗 ====================

function closeEmbedPopover() {
  const pop = $('embedPopover');
  if (pop) {
    pop.style.animation = 'popover-pop-in 0.15s ease reverse forwards';
    scopedPage.timeout(() => pop.remove(), 150);
  }
  document.removeEventListener('click', onEmbedOutsideClick);
}

function onEmbedOutsideClick(e) {
  const pop = $('embedPopover');
  if (pop && !pop.contains(e.target)) {
    closeEmbedPopover();
  }
}

async function showEmbeddingModal() {
  const existing = $('embedPopover');
  if (existing) { closeEmbedPopover(); return; }

  let status = { running: false, ready: false, error: '加载中...' };
  try { status = await get('/embedding/status'); } catch {}

  const embeddingItem = $('dsEmbedding')?.closest('.stat-item');
  if (!embeddingItem) return;

  const parent = embeddingItem.closest('.arc-panel-content') || embeddingItem.parentElement;
  parent.style.position = 'relative';

  const pop = document.createElement('div');
  pop.id = 'embedPopover';
  pop.className = 'embed-popover';

  // 定位到 embedding 项右侧
  const itemRect = embeddingItem.getBoundingClientRect();
  const parentRect = parent.getBoundingClientRect();
  pop.style.left = `${itemRect.right - parentRect.left + 8}px`;
  pop.style.top = `${itemRect.top - parentRect.top}px`;

  function renderPopover(s) {
    const stateText = s.ready ? '就绪' : s.running ? '加载中' : '离线';
    const stateColor = s.ready ? 'var(--success)' : s.running ? 'var(--warn)' : 'var(--text-3)';
    const dotClass = s.ready ? 'running' : '';

    pop.innerHTML = `
      <div class="embed-popover-title">Embedding 服务</div>
      <div class="embed-popover-status">
        <span class="status-dot ${dotClass}" style="width:7px;height:7px"></span>
        <span style="color:${stateColor};font-size:13px">${stateText}</span>
      </div>
      ${s.error ? `<div style="color:var(--text-3);font-size:11px;margin-bottom:10px;word-break:break-all">${s.error}</div>` : ''}
      <div class="embed-popover-actions">
        <button class="btn btn-primary btn-sm" id="embedRestartBtn">重启</button>
        <button class="btn btn-sm" id="embedRefreshBtn">刷新</button>
      </div>
    `;

    const refreshBtn = pop.querySelector('#embedRefreshBtn');
    const restartBtn = pop.querySelector('#embedRestartBtn');

    if (refreshBtn) refreshBtn.onclick = async (e) => {
      e.stopPropagation();
      try {
        const fresh = await get('/embedding/status');
        renderPopover(fresh);
        updateEmbeddingPanel(fresh);
      } catch { toast('刷新失败', 'error'); }
    };

    if (restartBtn) restartBtn.onclick = async (e) => {
      e.stopPropagation();
      restartBtn.disabled = true;
      restartBtn.textContent = '重启中…';
      try {
        const res = await post('/embedding/restart', {});
        if (res.success) {
          toast('Embedding 服务已重启', 'success');
          renderPopover({ running: true, ready: true, error: '' });
          updateEmbeddingPanel({ running: true, ready: true, error: '' });
        } else {
          toast('重启失败: ' + (res.error || '未知错误'), 'error');
          renderPopover({ running: false, ready: false, error: res.error || '重启失败' });
        }
      } catch {
        toast('重启请求失败', 'error');
        restartBtn.disabled = false;
        restartBtn.textContent = '重启';
      }
    };
  }

  renderPopover(status);
  parent.appendChild(pop);

  scopedPage.timeout(() => scopedPage.on(document, 'click', onEmbedOutsideClick), 0);
}

function updateEmbeddingPanel(s) {
  if (!$('dsEmbedding') || !$('dsEmbeddingIcon')) return;
  if (s.ready) {
    $('dsEmbedding').textContent = '就绪';
    $('dsEmbedding').style.color = 'var(--success)';
    $('dsEmbeddingIcon').style.color = 'var(--success)';
  } else if (s.running) {
    $('dsEmbedding').textContent = '加载中';
    $('dsEmbedding').style.color = 'var(--warn)';
    $('dsEmbeddingIcon').style.color = 'var(--warn)';
  } else {
    $('dsEmbedding').textContent = '离线';
    $('dsEmbedding').style.color = 'var(--text-3)';
    $('dsEmbeddingIcon').style.color = 'var(--text-3)';
  }
}

// 更新右侧面板
function updatePersonaPanel(index) {
  const persona = personasData[index];
  if (!persona) return;

  currentPersona = persona;

  // 仅在人格切换时更新面板内容和请求监控数据
  const isSamePersona = (panelPersonaIndex === index);
  panelPersonaIndex = index;

  const nameEl = $('panelPersonaName');
  if (nameEl) nameEl.textContent = persona.persona_name || persona.name;

  const isRunning = persona.running;
  const statusEl = $('panelStatus');
  if (statusEl) {
    statusEl.innerHTML = `
      <span class="status-dot ${isRunning ? 'running' : ''}"></span>
      <span style="color:${isRunning ? 'var(--success)' : 'var(--text-3)'}">${isRunning ? '运行中' : '已停止'}</span>
    `;
  }

  const metaEl = $('panelMeta');
  if (metaEl) metaEl.textContent = persona.persona_summary || '暂无描述';

  // 更新按钮
  const startBtn = $('panelStartBtn');
  const stopBtn = $('panelStopBtn');
  const configBtn = $('panelConfigBtn');
  if (startBtn) startBtn.style.display = isRunning ? 'none' : 'inline-flex';
  if (stopBtn) stopBtn.style.display = isRunning ? 'inline-flex' : 'none';
  if (configBtn) configBtn.style.display = 'inline-flex';

  // 仅在人格切换时加载监控数据，避免鼠标移动时频繁请求
  if (!isSamePersona) {
    loadPersonaMonitoring(persona.name);
  }
}

// 格式化运行时长
function formatUptime(seconds) {
  if (!seconds || seconds <= 0) return '—';
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (h > 24) {
    const d = Math.floor(h / 24);
    return `${d}天${h % 24}时`;
  }
  if (h > 0) return `${h}时${m}分`;
  return `${m}分`;
}

// 健康状态图标映射
const HEALTH_ICONS = { ok: '✅', down: '❌', warning: '⚠️', missing: '⚠️', empty: '⚠️' };

// 异步加载人格监控数据
async function loadPersonaMonitoring(name) {
  const metricsEl = $('panelMetrics');
  const healthEl = $('panelHealth');
  if (!metricsEl || !healthEl) return;

  metricsEl.style.display = 'none';
  healthEl.style.display = 'none';

  try {
    const [metricsRes, healthRes] = await Promise.all([
      get(`/monitoring/metrics`).catch(() => null),
      get(`/monitoring/health`).catch(() => null),
    ]);

    // 填充运行指标
    if (metricsRes && !metricsRes.error) {
      metricsEl.style.display = 'flex';
      const token = metricsRes.token_usage || {};
      const memory = metricsRes.memory || {};
      const cognition = metricsRes.cognition || {};

      if ($('panelPid')) $('panelPid').textContent = metricsRes.pid || '—';
      if ($('panelUptime')) $('panelUptime').textContent = formatUptime(metricsRes.uptime_seconds);
      if ($('panelTokenIn')) $('panelTokenIn').textContent = (token.total_input || 0).toLocaleString();
      if ($('panelTokenOut')) $('panelTokenOut').textContent = (token.total_output || 0).toLocaleString();
      if ($('panelCalls')) $('panelCalls').textContent = (token.call_count || 0).toLocaleString();
      if ($('panelDiary')) $('panelDiary').textContent = (memory.diary_count || 0).toLocaleString();
      if ($('panelGlossary')) $('panelGlossary').textContent = (memory.glossary_count || 0).toLocaleString();
      if ($('panelCognition')) $('panelCognition').textContent = (cognition.event_count || 0).toLocaleString();
    }

    // 填充健康状态
    if (healthRes && !healthRes.error) {
      healthEl.style.display = 'flex';
      const checks = healthRes.checks || {};

      renderHealthItem('healthProcess', checks.process);
      renderHealthItem('healthConfig', checks.config);
      renderHealthItem('healthMemory', checks.memory);
    }
  } catch {
    // 加载失败时隐藏面板
    metricsEl.style.display = 'none';
    healthEl.style.display = 'none';
  }
}

// 渲染单个健康检查项
function renderHealthItem(elementId, check) {
  const el = $(elementId);
  if (!el || !check) {
    if (el) el.style.display = 'none';
    return;
  }
  el.style.display = 'flex';
  const icon = HEALTH_ICONS[check.status] || '⚠️';
  el.querySelector('.persona-health-icon').textContent = icon;
}

// ==================== 数据加载 ====================

async function loadStats() {
  try {
    const [tokenRes, telemetryRes, embeddingRes] = await Promise.all([
      get('/tokens').catch(() => ({})),
      get('/telemetry').catch(() => ({})),
      get('/embedding/status').catch(() => ({})),
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
    const cache = tokenRes.cache_stats || {};
    if ($('dsCache')) {
      $('dsCache').textContent = cache.cache_info_calls
        ? `${cache.cache_hit_rate_pct || 0}%`
        : '—';
    }
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

    // Embedding 状态
    if ($('dsEmbedding') && $('dsEmbeddingIcon')) {
      if (embeddingRes.ready) {
        $('dsEmbedding').textContent = '就绪';
        $('dsEmbedding').style.color = 'var(--success)';
        $('dsEmbeddingIcon').style.color = 'var(--success)';
      } else if (embeddingRes.running) {
        $('dsEmbedding').textContent = '加载中';
        $('dsEmbedding').style.color = 'var(--warn)';
        $('dsEmbeddingIcon').style.color = 'var(--warn)';
      } else {
        $('dsEmbedding').textContent = '离线';
        $('dsEmbedding').style.color = 'var(--text-3)';
        $('dsEmbeddingIcon').style.color = 'var(--text-3)';
      }
    }
  } catch (e) {
    if (e?.name === 'AbortError') return;
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
    if (e?.name === 'AbortError') return;
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
  realtime.stop();
  if (onPersonaFocus) {
    window.removeEventListener('persona:focus', onPersonaFocus);
    onPersonaFocus = null;
  }
  if (starfield) {
    starfield.destroy();
    starfield = null;
  }
  if (globe) {
    globe.destroy();
    globe = null;
  }
}
