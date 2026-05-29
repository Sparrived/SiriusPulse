/**
 * 球体渲染引擎 - 自定义Canvas 3D球体
 * 支持不规则多边形图斑、鼠标交互、旋转控制
 */

export class GlobeRenderer {
  constructor(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext('2d');
    this.width = canvas.width;
    this.height = canvas.height;
    this.centerX = this.width / 2;
    this.centerY = this.height / 2;
    // 球体半径为 canvas 尺寸的 42%，为大气层效果留出空间
    this.radius = Math.min(this.width, this.height) * 0.42;

    // 旋转角度
    this.beta = 0;
    this.alpha = 20;

    // 自动旋转
    this.autoRotate = true;
    this.autoRotateSpeed = 0.08;

    // 鼠标交互
    this.isDragging = false;
    this.lastMouseX = 0;
    this.lastMouseY = 0;
    this.hoveredSpot = -1;

    // 选中图斑索引（-1 表示未选中）
    this.selectedSpot = -1;

    // 旋转动画目标（null 表示无动画）
    this.targetBeta = null;

    // 到达目标后暂停自动旋转的剩余帧数
    this.pauseTicks = 0;

    // 动画时间计数器（用于脉冲效果）
    this.tick = 0;

    // 流动粒子系统
    this.particles = [];
    this.maxParticles = 80;
    this.initParticles();

    // 图斑数据
    this.spots = [];

    // 随机种子（用于生成固定的不规则形状）
    this.seed = 42;

    // 主题色缓存（从 CSS 变量读取）
    this._themeCache = null;
    this._themeCacheTick = -1;
  }

  // 从 DOM 读取当前主题的 --accent 颜色，缓存 60 帧刷新一次
  getThemeColors() {
    if (this._themeCache && this.tick - this._themeCacheTick < 60) {
      return this._themeCache;
    }
    const style = getComputedStyle(document.documentElement);
    const accent = style.getPropertyValue('--accent').trim() || '#4c9aff';

    // 解析 hex 或 rgb 颜色为 [r, g, b]
    const parse = (color) => {
      if (color.startsWith('#')) {
        const hex = color.slice(1);
        return [
          parseInt(hex.substring(0, 2), 16),
          parseInt(hex.substring(2, 4), 16),
          parseInt(hex.substring(4, 6), 16),
        ];
      }
      const m = color.match(/(\d+)/g);
      return m ? [+m[0], +m[1], +m[2]] : [76, 154, 255];
    };

    const [r, g, b] = parse(accent);
    this._themeCache = { accent, r, g, b };
    this._themeCacheTick = this.tick;
    return this._themeCache;
  }

  // 基于主题 accent 色生成 rgba 字符串
  themeRGBA(alpha) {
    const { r, g, b } = this.getThemeColors();
    return `rgba(${r}, ${g}, ${b}, ${alpha})`;
  }

  // 基于主题 accent 色生成 hex 字符串
  themeHex() {
    return this.getThemeColors().accent;
  }

  // 初始化流动粒子
  initParticles() {
    this.particles = [];
    for (let i = 0; i < this.maxParticles; i++) {
      this.particles.push(this.createParticle());
    }
  }

  // 创建单个粒子
  createParticle() {
    const lng = Math.random() * 360;
    const lat = (Math.random() - 0.5) * 140;
    const speed = 0.3 + Math.random() * 0.8;
    const size = 0.5 + Math.random() * 1.5;
    const life = Math.random();
    const maxLife = 200 + Math.random() * 300;

    return {
      lng,
      lat,
      speed,
      size,
      life,
      maxLife,
      age: Math.random() * maxLife,
      trail: [],
      trailLength: 3 + Math.floor(Math.random() * 5),
    };
  }

  // 初始化图斑，生成不规则多边形顶点
  initSpots(spotData) {
    this.spots = spotData.map((spot, index) => {
      const lng = (spot.x / 2048) * 360;
      const lat = spot.lat || 20 * Math.sin(index * 1.2);

      // 生成不规则多边形顶点（8-12个顶点）
      const numVertices = 8 + Math.floor(this.seededRandom(index * 100) * 5);
      const vertices = this.generateIrregularPolygon(lng, lat, spot.size || 25, numVertices, index);

      return {
        ...spot,
        index,
        lng,
        lat,
        vertices, // 不规则多边形顶点（经纬度）
        screenVertices: [], // 投影后的屏幕坐标
        screenX: null,
        screenY: null,
        isVisible: false,
      };
    });
  }

  // 伪随机数生成器（基于种子）
  seededRandom(seed) {
    const x = Math.sin(seed * 127.1 + 311.7) * 43758.5453;
    return x - Math.floor(x);
  }

  // 生成不规则多边形顶点
  generateIrregularPolygon(centerLng, centerLat, size, numVertices, spotIndex) {
    const vertices = [];
    const baseRadius = size / this.radius * (180 / Math.PI);

    // 使用6-8个顶点，生成不规则多边形
    const vertexCount = 6 + (spotIndex % 3);

    for (let i = 0; i < vertexCount; i++) {
      const angle = (i / vertexCount) * Math.PI * 2;
      // 较大的不规则性
      const r1 = this.seededRandom(spotIndex * 100 + i * 7) * 0.5 + 0.75;
      const r2 = Math.sin(angle * 2 + spotIndex * 3) * 0.15;
      const r3 = Math.cos(angle * 3 + spotIndex * 5) * 0.1;
      const variation = r1 + r2 + r3;
      const radius = baseRadius * variation;

      const lng = centerLng + Math.cos(angle) * radius / Math.cos(centerLat * Math.PI / 180);
      const lat = centerLat + Math.sin(angle) * radius;

      vertices.push({ lng, lat });
    }

    return vertices;
  }

  // 3D坐标转2D坐标
  project(lng, lat) {
    const lngRad = (lng * Math.PI) / 180;
    const latRad = (lat * Math.PI) / 180;
    const betaRad = (this.beta * Math.PI) / 180;
    const alphaRad = (this.alpha * Math.PI) / 180;

    const x = Math.sin(lngRad - betaRad) * Math.cos(latRad);
    const y = Math.sin(latRad) * Math.cos(alphaRad) -
              Math.cos(latRad) * Math.cos(lngRad - betaRad) * Math.sin(alphaRad);
    const z = Math.cos(latRad) * Math.cos(lngRad - betaRad) * Math.cos(alphaRad) +
              Math.sin(latRad) * Math.sin(alphaRad);

    // 使用当前半径（包含呼吸效果）
    const currentR = this.currentRadius || this.radius;

    return {
      x: this.centerX + x * currentR,
      y: this.centerY - y * currentR,
      z: z,
      visible: z > -0.1
    };
  }

  // 判断点是否在正面
  isInFront(lng, lat) {
    const lngRad = (lng * Math.PI) / 180;
    const latRad = (lat * Math.PI) / 180;
    const betaRad = (this.beta * Math.PI) / 180;
    const alphaRad = (this.alpha * Math.PI) / 180;

    const z = Math.cos(latRad) * Math.cos(lngRad - betaRad) * Math.cos(alphaRad) +
              Math.sin(latRad) * Math.sin(alphaRad);
    return z > 0;
  }

  // 将多边形裁剪到可见半球（z > 0）
  // Sutherland-Hodgman 算法：对每条边判断是否跨越 z=0 平面，插值交点
  clipToHemisphere(vertices) {
    if (vertices.length < 3) return [];
    let output = vertices.slice();

    // 对 z > 0 平面做裁剪
    const input = output;
    output = [];
    const n = input.length;
    for (let i = 0; i < n; i++) {
      const curr = input[i];
      const next = input[(i + 1) % n];
      const currIn = curr.z > 0;
      const nextIn = next.z > 0;

      if (currIn) {
        output.push(curr);
        if (!nextIn) {
          // 从正面穿到背面，插值交点
          const t = curr.z / (curr.z - next.z);
          output.push({
            x: curr.x + t * (next.x - curr.x),
            y: curr.y + t * (next.y - curr.y),
            z: 0.001,
            visible: true,
          });
        }
      } else if (nextIn) {
        // 从背面穿到正面，插值交点
        const t = curr.z / (curr.z - next.z);
        output.push({
          x: curr.x + t * (next.x - curr.x),
          y: curr.y + t * (next.y - curr.y),
          z: 0.001,
          visible: true,
        });
      }
    }
    return output;
  }

  // 绘制球体（星球效果）
  drawGlobe() {
    const ctx = this.ctx;
    const cx = this.centerX;
    const cy = this.centerY;
    const r = this.radius;

    // 更新当前半径用于投影计算
    this.currentRadius = r;

    // ===== 星球主体 =====
    // 基础渐变（从主题 accent 色混合深色底色）
    const { r: ar, g: ag, b: ab } = this.getThemeColors();

    // 将 accent 色与深黑混合，ratio 越大 accent 越明显
    const blend = (ratio) => {
      const bg = 8;
      return `rgb(${Math.floor(bg + (ar - bg) * ratio)}, ${Math.floor(bg + (ag - bg) * ratio)}, ${Math.floor(bg + (ab - bg) * ratio)})`;
    };

    const baseGradient = ctx.createRadialGradient(
      cx - r * 0.25, cy - r * 0.25, r * 0.1,
      cx, cy, r
    );
    baseGradient.addColorStop(0, blend(0.38));
    baseGradient.addColorStop(0.25, blend(0.28));
    baseGradient.addColorStop(0.5, blend(0.2));
    baseGradient.addColorStop(0.75, blend(0.13));
    baseGradient.addColorStop(1, blend(0.06));

    ctx.save();
    ctx.beginPath();
    ctx.arc(cx, cy, r, 0, Math.PI * 2);
    ctx.clip();
    ctx.fillStyle = baseGradient;
    ctx.fillRect(cx - r, cy - r, r * 2, r * 2);

    // ===== 星云色块（随球体旋转）=====
    this.drawNebula(ctx, cx, cy, r);

    // ===== 星球表面纹理（大陆感）=====
    this.drawSurfaceTexture(ctx, cx, cy, r);

    // ===== 表面星点 =====
    this.drawSurfaceStars(ctx, cx, cy, r);

    // ===== 经纬线网格 =====
    this.drawGrid();

    // ===== 图斑 =====
    this.drawSpotsSorted();

    // ===== 大气层边缘（多层）=====
    const atmo1 = ctx.createRadialGradient(cx, cy, r * 0.82, cx, cy, r);
    atmo1.addColorStop(0, 'transparent');
    atmo1.addColorStop(0.6, 'transparent');
    atmo1.addColorStop(1, this.themeRGBA(0.12));
    ctx.fillStyle = atmo1;
    ctx.fillRect(cx - r, cy - r, r * 2, r * 2);

    const atmo2 = ctx.createRadialGradient(cx, cy, r * 0.92, cx, cy, r * 1.02);
    atmo2.addColorStop(0, 'transparent');
    atmo2.addColorStop(0.5, this.themeRGBA(0.08));
    atmo2.addColorStop(1, 'transparent');
    ctx.fillStyle = atmo2;
    ctx.fillRect(cx - r * 1.05, cy - r * 1.05, r * 2.1, r * 2.1);

    // ===== 光照高光 =====
    const highlight = ctx.createRadialGradient(
      cx - r * 0.3, cy - r * 0.3, 0,
      cx - r * 0.3, cy - r * 0.3, r * 0.6
    );
    highlight.addColorStop(0, 'rgba(255, 255, 255, 0.08)');
    highlight.addColorStop(1, 'transparent');
    ctx.fillStyle = highlight;
    ctx.fillRect(cx - r, cy - r, r * 2, r * 2);

    ctx.restore();

    // ===== 边缘光圈 =====
    ctx.beginPath();
    ctx.arc(cx, cy, r, 0, Math.PI * 2);
    ctx.strokeStyle = this.themeRGBA(0.25);
    ctx.lineWidth = 1.5;
    ctx.stroke();

    // ===== 流动粒子 =====
    this.drawParticles(ctx, cx, cy, r);

    // ===== 动态大气层脉冲 =====
    this.drawPulsingAtmosphere(ctx, cx, cy, r);
  }

  // 绘制星球表面纹理
  drawSurfaceTexture(ctx, cx, cy, r) {
    const { r: ar, g: ag, b: ab } = this.getThemeColors();

    // 绘制一些大陆形状的阴影（基于主题色）
    for (let i = 0; i < 8; i++) {
      const lng = (i * 45 + 20) % 360;
      const lat = 15 * Math.sin(i * 1.5);
      const p = this.project(lng, lat);

      if (p.visible && this.isInFront(lng, lat)) {
        const dist = Math.sqrt(Math.pow(p.x - cx, 2) + Math.pow(p.y - cy, 2));
        if (dist < r * 0.9) {
          const size = 20 + i * 5;
          const gradient = ctx.createRadialGradient(p.x, p.y, 0, p.x, p.y, size);
          gradient.addColorStop(0, `rgba(${Math.floor(ar * 0.2)}, ${Math.floor(ag * 0.2)}, ${Math.floor(ab * 0.2)}, 0.3)`);
          gradient.addColorStop(1, 'transparent');
          ctx.fillStyle = gradient;
          ctx.beginPath();
          ctx.arc(p.x, p.y, size, 0, Math.PI * 2);
          ctx.fill();
        }
      }
    }
  }

  // 绘制星云色块（随球体旋转的彩色光斑，基于主题色）
  drawNebula(ctx, cx, cy, r) {
    const { r: ar, g: ag, b: ab } = this.getThemeColors();
    // 从 accent 色派生 6 个星云色（色相微偏移 + 饱和度变化）
    const nebulae = [
      { lngOff: 0,   lat: 25,  size: 55, color: `${ar}, ${ag}, ${ab}` },
      { lngOff: 120, lat: -15, size: 45, color: `${Math.floor(ab * 0.8)}, ${Math.floor(ar * 0.5)}, ${Math.floor(ag * 0.9)}` },
      { lngOff: 240, lat: 10,  size: 50, color: `${Math.floor(ag * 0.6)}, ${Math.floor(ab * 0.7)}, ${ar}` },
      { lngOff: 60,  lat: -35, size: 35, color: `${ar}, ${Math.floor(ag * 0.4)}, ${Math.floor(ab * 0.7)}` },
      { lngOff: 180, lat: 40,  size: 40, color: `${Math.floor(ar * 0.4)}, ${ag}, ${Math.floor(ab * 0.8)}` },
      { lngOff: 300, lat: -5,  size: 48, color: `${Math.floor(ab * 0.7)}, ${Math.floor(ag * 0.5)}, ${ar}` },
    ];

    nebulae.forEach(n => {
      const p = this.project(n.lngOff, n.lat);
      if (!p.visible || !this.isInFront(n.lngOff, n.lat)) return;
      const dist = Math.sqrt(Math.pow(p.x - cx, 2) + Math.pow(p.y - cy, 2));
      if (dist > r * 0.92) return;

      const depthFactor = 0.5 + p.z * 0.5;
      const grad = ctx.createRadialGradient(p.x, p.y, 0, p.x, p.y, n.size * depthFactor);
      grad.addColorStop(0, `rgba(${n.color}, ${0.08 * depthFactor})`);
      grad.addColorStop(0.5, `rgba(${n.color}, ${0.03 * depthFactor})`);
      grad.addColorStop(1, 'transparent');
      ctx.fillStyle = grad;
      ctx.beginPath();
      ctx.arc(p.x, p.y, n.size * depthFactor, 0, Math.PI * 2);
      ctx.fill();
    });
  }

  // 绘制表面星点（散落的微小亮点）
  drawSurfaceStars(ctx, cx, cy, r) {
    const { r: ar, g: ag, b: ab } = this.getThemeColors();
    // 固定种子生成星点位置，避免每帧闪烁
    for (let i = 0; i < 40; i++) {
      const lng = this.seededRandom(i * 73.7) * 360;
      const lat = this.seededRandom(i * 91.3) * 140 - 70;
      const p = this.project(lng, lat);

      if (!p.visible || !this.isInFront(lng, lat)) continue;
      const dist = Math.sqrt(Math.pow(p.x - cx, 2) + Math.pow(p.y - cy, 2));
      if (dist > r * 0.92) continue;

      const depthFactor = 0.3 + p.z * 0.7;
      const size = 0.5 + this.seededRandom(i * 31.1) * 1.2;
      // 微弱闪烁
      const flicker = 0.6 + 0.4 * Math.sin(this.tick * 0.02 + i * 2.1);
      const alpha = 0.3 * depthFactor * flicker;

      ctx.beginPath();
      ctx.arc(p.x, p.y, size, 0, Math.PI * 2);
      ctx.fillStyle = `rgba(${Math.min(255, ar + 150)}, ${Math.min(255, ag + 150)}, ${Math.min(255, ab + 150)}, ${alpha})`;
      ctx.fill();
    }
  }

  // 绘制经纬线
  drawGrid() {
    const ctx = this.ctx;
    ctx.strokeStyle = this.themeRGBA(0.12);
    ctx.lineWidth = 0.5;

    // 经线
    for (let lng = 0; lng < 360; lng += 30) {
      ctx.beginPath();
      let started = false;
      for (let lat = -90; lat <= 90; lat += 5) {
        const p = this.project(lng, lat);
        if (p.visible && this.isInFront(lng, lat)) {
          if (!started) {
            ctx.moveTo(p.x, p.y);
            started = true;
          } else {
            ctx.lineTo(p.x, p.y);
          }
        } else {
          started = false;
        }
      }
      ctx.stroke();
    }

    // 纬线
    for (let lat = -60; lat <= 60; lat += 30) {
      ctx.beginPath();
      let started = false;
      for (let lng = 0; lng <= 360; lng += 5) {
        const p = this.project(lng, lat);
        if (p.visible && this.isInFront(lng, lat)) {
          if (!started) {
            ctx.moveTo(p.x, p.y);
            started = true;
          } else {
            ctx.lineTo(p.x, p.y);
          }
        } else {
          started = false;
        }
      }
      ctx.stroke();
    }
  }

  // 按深度排序后绘制图斑
  drawSpotsSorted() {
    // 计算每个图斑的中心深度
    const spotsWithDepth = this.spots.map(spot => {
      const centerP = this.project(spot.lng, spot.lat);
      return { spot, depth: centerP.z, centerVisible: centerP.visible };
    });

    // 按深度排序（远的先画）
    spotsWithDepth.sort((a, b) => a.depth - b.depth);

    // 绘制
    spotsWithDepth.forEach(({ spot, centerVisible }) => {
      this.drawSpotPolygon(spot, centerVisible);
    });
  }

  // 绘制单个不规则多边形图斑
  drawSpotPolygon(spot, centerVisible) {
    const ctx = this.ctx;
    const isHovered = spot.index === this.hoveredSpot;
    const isSelected = spot.index === this.selectedSpot;

    // 投影所有顶点
    const projectedVertices = spot.vertices.map(v => this.project(v.lng, v.lat));

    // 计算图斑中心
    const centerP = this.project(spot.lng, spot.lat);

    // 将多边形裁剪到可见半球，避免背面顶点穿透球体
    const clippedVertices = this.clipToHemisphere(projectedVertices);

    // 裁剪后至少 3 个顶点才绘制
    const isVisible = clippedVertices.length >= 3 && centerP.z > -0.1;

    // 更新可见性状态
    spot.isVisible = isVisible;
    spot.screenX = isVisible ? centerP.x : null;
    spot.screenY = isVisible ? centerP.y : null;

    if (!isVisible) {
      spot.screenVertices = [];
      return;
    }

    // 存储裁剪后的屏幕坐标
    spot.screenVertices = clippedVertices;

    // 计算平均深度用于缩放
    const avgDepth = clippedVertices.reduce((sum, v) => sum + v.z, 0) / clippedVertices.length;
    const depthScale = 0.7 + avgDepth * 0.3;

    // ===== 选中态：脉冲光环效果 =====
    if (isSelected) {
      this.drawSelectedPulse(spot, clippedVertices, centerP);
    }

    // ===== 绘制外层（边框+发光）=====
    ctx.save();

    if (isSelected) {
      // 选中时：使用人格自身的颜色发光
      const borderColor = spot.borderColor || this.themeRGBA(0.6);
      ctx.shadowColor = borderColor.replace(/[\d.]+\)$/, '0.8)');
      ctx.shadowBlur = 25;
    } else if (isHovered) {
      // 悬停时白色发光
      ctx.shadowColor = 'rgba(255, 255, 255, 0.6)';
      ctx.shadowBlur = 20;
    }

    ctx.beginPath();
    this.drawSmoothPolygon(ctx, clippedVertices);
    ctx.closePath();

    // 外层边框
    ctx.strokeStyle = isSelected
      ? (spot.borderColor || this.themeRGBA(0.8))
      : isHovered
        ? 'rgba(255, 255, 255, 0.8)'
        : spot.borderColor || this.themeRGBA(0.6);
    ctx.lineWidth = isSelected ? 3 : isHovered ? 3 : 2;
    ctx.stroke();

    ctx.restore();

    // ===== 绘制内层（填充）=====
    ctx.save();
    ctx.beginPath();
    this.drawSmoothPolygon(ctx, clippedVertices);
    ctx.closePath();

    // 内层填充（稍微缩小）
    const shrinkFactor = 0.92;
    ctx.translate(centerP.x, centerP.y);
    ctx.scale(shrinkFactor, shrinkFactor);
    ctx.translate(-centerP.x, -centerP.y);

    ctx.fillStyle = isSelected
      ? (spot.color || 'rgba(76, 154, 255, 0.15)').replace(/[\d.]+\)$/, '0.35)')
      : isHovered
        ? 'rgba(255, 255, 255, 0.15)'
        : this.getSpotFillColor(spot);
    ctx.fill();

    ctx.restore();

    // ===== 绘制内部纹理/细节 =====
    this.drawSpotTexture(spot, clippedVertices, isHovered || isSelected);

    // 更新鼠标检测范围
    spot.screenSize = this.radius * 0.15;
  }

  // 绘制选中图斑的脉冲光环（形状跟随图斑多边形，贴附球体表面）
  drawSelectedPulse(spot, vertices, centerP) {
    const ctx = this.ctx;
    const borderColor = spot.borderColor || 'rgba(76, 154, 255, 0.6)';

    // 从 borderColor 提取基础色值
    const colorMatch = borderColor.match(/rgba?\((\d+),\s*(\d+),\s*(\d+)/);
    const r = colorMatch ? colorMatch[1] : '76';
    const g = colorMatch ? colorMatch[2] : '154';
    const b = colorMatch ? colorMatch[3] : '255';

    // 脉冲参数：单层扩张后消失
    const t = this.tick * 0.03;
    const pulse = (t / (Math.PI * 2)) % 1;

    ctx.save();

    // 脉冲：从图斑扩张 + 淡出消失
    const scale = 1.0 + 0.4 * pulse;
    const alpha = 0.75 * (1 - pulse);
    ctx.beginPath();
    this.drawScaledPolygon(ctx, vertices, centerP, scale);
    ctx.strokeStyle = `rgba(${r}, ${g}, ${b}, ${alpha})`;
    ctx.lineWidth = 3;
    ctx.stroke();

    // 底层持续发光（裁剪在图斑形状内，贴附球体表面）
    ctx.beginPath();
    this.drawScaledPolygon(ctx, vertices, centerP, 1.0);
    ctx.clip();

    const maxDist = Math.max(
      Math.abs(vertices[0].x - centerP.x),
      Math.abs(vertices[0].y - centerP.y)
    ) * 1.5;
    const glowGradient = ctx.createRadialGradient(
      centerP.x, centerP.y, 0,
      centerP.x, centerP.y, maxDist
    );
    glowGradient.addColorStop(0, `rgba(${r}, ${g}, ${b}, 0.35)`);
    glowGradient.addColorStop(0.4, `rgba(${r}, ${g}, ${b}, 0.15)`);
    glowGradient.addColorStop(1, 'transparent');
    ctx.fillStyle = glowGradient;
    ctx.fillRect(
      centerP.x - maxDist, centerP.y - maxDist,
      maxDist * 2, maxDist * 2
    );

    ctx.restore();
  }

  // 以 centerP 为中心按 scale 缩放多边形顶点并绘制路径
  drawScaledPolygon(ctx, vertices, centerP, scale) {
    for (let i = 0; i < vertices.length; i++) {
      const sx = centerP.x + (vertices[i].x - centerP.x) * scale;
      const sy = centerP.y + (vertices[i].y - centerP.y) * scale;
      if (i === 0) {
        ctx.moveTo(sx, sy);
      } else {
        ctx.lineTo(sx, sy);
      }
    }
    ctx.closePath();
  }

  // 绘制多边形（直线连接）
  drawSmoothPolygon(ctx, vertices) {
    if (vertices.length < 3) return;

    ctx.moveTo(vertices[0].x, vertices[0].y);

    for (let i = 1; i < vertices.length; i++) {
      ctx.lineTo(vertices[i].x, vertices[i].y);
    }

    ctx.closePath();
  }

  // 获取图斑填充色
  getSpotFillColor(spot) {
    const { r, g, b } = this.getThemeColors();
    const colors = [
      `rgba(${r}, ${g}, ${b}, 0.12)`,
      'rgba(63, 185, 80, 0.12)',
      'rgba(163, 113, 247, 0.12)',
      `rgba(${r}, ${g}, ${b}, 0.12)`,
      'rgba(210, 153, 34, 0.12)',
      'rgba(248, 81, 73, 0.12)',
    ];
    return colors[spot.index % colors.length];
  }

  // 绘制图斑内部纹理
  drawSpotTexture(spot, vertices, isHovered) {
    const ctx = this.ctx;

    // 计算边界框
    let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
    vertices.forEach(v => {
      minX = Math.min(minX, v.x);
      maxX = Math.max(maxX, v.x);
      minY = Math.min(minY, v.y);
      maxY = Math.max(maxY, v.y);
    });

    const centerX = (minX + maxX) / 2;
    const centerY = (minY + maxY) / 2;
    const width = maxX - minX;
    const height = maxY - minY;

    // 绘制内部网格线
    ctx.save();
    ctx.beginPath();
    this.drawSmoothPolygon(ctx, vertices);
    ctx.clip();

    ctx.strokeStyle = isHovered ? 'rgba(255, 255, 255, 0.1)' : this.themeRGBA(0.08);
    ctx.lineWidth = 0.5;

    // 水平线
    for (let y = minY; y <= maxY; y += height / 5) {
      ctx.beginPath();
      ctx.moveTo(minX, y);
      ctx.lineTo(maxX, y);
      ctx.stroke();
    }

    // 垂直线
    for (let x = minX; x <= maxX; x += width / 5) {
      ctx.beginPath();
      ctx.moveTo(x, minY);
      ctx.lineTo(x, maxY);
      ctx.stroke();
    }

    // 中心亮点
    const highlightGradient = ctx.createRadialGradient(
      centerX - width * 0.1,
      centerY - height * 0.1,
      0,
      centerX,
      centerY,
      Math.max(width, height) * 0.4
    );
    highlightGradient.addColorStop(0, isHovered ? 'rgba(255, 255, 255, 0.2)' : 'rgba(255, 255, 255, 0.08)');
    highlightGradient.addColorStop(1, 'transparent');

    ctx.fillStyle = highlightGradient;
    ctx.fillRect(minX, minY, width, height);

    ctx.restore();
  }

  // 绘制边缘光晕
  drawGlow() {
    const ctx = this.ctx;

    const gradient = ctx.createRadialGradient(
      this.centerX, this.centerY, this.radius - 10,
      this.centerX, this.centerY, this.radius + 20
    );
    gradient.addColorStop(0, 'rgba(76, 154, 255, 0.1)');
    gradient.addColorStop(0.5, 'rgba(76, 154, 255, 0.05)');
    gradient.addColorStop(1, 'transparent');

    ctx.beginPath();
    ctx.arc(this.centerX, this.centerY, this.radius + 20, 0, Math.PI * 2);
    ctx.fillStyle = gradient;
    ctx.fill();
  }

  // 清除画布
  clear() {
    this.ctx.clearRect(0, 0, this.width, this.height);
  }

  // 更新旋转
  update() {
    this.tick++;
    if (this.targetBeta !== null) {
      // 平滑旋转到目标角度
      let diff = this.targetBeta - this.beta;
      // 取最短旋转路径
      while (diff > 180) diff -= 360;
      while (diff < -180) diff += 360;
      if (Math.abs(diff) < 0.5) {
        this.beta = this.targetBeta;
        this.targetBeta = null;
        this.pauseTicks = 300;
      } else {
        this.beta += diff * 0.08;
      }
    } else if (this.pauseTicks > 0) {
      this.pauseTicks--;
      if (this.pauseTicks <= 0) this.autoRotate = true;
    } else if (this.autoRotate && !this.isDragging) {
      this.beta += this.autoRotateSpeed;
    }
    if (this.beta >= 360) this.beta -= 360;
    if (this.beta < 0) this.beta += 360;
  }

  // 将指定图斑旋转到正面中央并选中
  focusSpot(index) {
    if (index < 0 || index >= this.spots.length) return;
    const spot = this.spots[index];
    this.selectedSpot = index;
    this.autoRotate = false;
    this.targetBeta = spot.lng;
  }

  // 渲染一帧
  render() {
    this.clear();
    this.update();
    this.drawGlobe();
  }

  // 鼠标按下
  onMouseDown(x, y) {
    this.isDragging = true;
    this.lastMouseX = x;
    this.lastMouseY = y;
  }

  // 鼠标移动
  onMouseMove(x, y) {
    if (this.isDragging) {
      const dx = x - this.lastMouseX;
      const dy = y - this.lastMouseY;
      this.beta -= dx * 0.5;
      this.alpha = Math.max(-60, Math.min(60, this.alpha + dy * 0.3));
      this.lastMouseX = x;
      this.lastMouseY = y;
    }

    // 检测悬停（使用图斑中心点）
    this.hoveredSpot = -1;
    this.spots.forEach(spot => {
      if (spot.isVisible && spot.screenX !== null && spot.screenY !== null) {
        const dist = Math.sqrt(Math.pow(x - spot.screenX, 2) + Math.pow(y - spot.screenY, 2));
        if (dist < spot.screenSize + 10) {
          this.hoveredSpot = spot.index;
        }
      }
    });
  }

  // 鼠标释放
  onMouseUp() {
    this.isDragging = false;
  }

  // 检查是否有悬停的图斑
  hasHoveredSpot() {
    return this.hoveredSpot >= 0;
  }

  // 绘制流动粒子
  drawParticles(ctx, cx, cy, r) {
    const { r: ar, g: ag, b: ab } = this.getThemeColors();

    this.particles.forEach((particle, index) => {
      // 更新粒子位置
      particle.lng += particle.speed;
      if (particle.lng >= 360) particle.lng -= 360;

      // 更新年龄
      particle.age++;
      if (particle.age >= particle.maxLife) {
        // 重生粒子
        this.particles[index] = this.createParticle();
        return;
      }

      // 投影到屏幕
      const p = this.project(particle.lng, particle.lat);
      if (!p.visible || !this.isInFront(particle.lng, particle.lat)) return;

      const dist = Math.sqrt(Math.pow(p.x - cx, 2) + Math.pow(p.y - cy, 2));
      if (dist > r * 0.95) return;

      // 计算透明度（生命周期渐入渐出）
      const lifeRatio = particle.age / particle.maxLife;
      let alpha;
      if (lifeRatio < 0.1) {
        alpha = lifeRatio * 10;
      } else if (lifeRatio > 0.9) {
        alpha = (1 - lifeRatio) * 10;
      } else {
        alpha = 1;
      }

      const depthFactor = 0.3 + p.z * 0.7;
      alpha *= depthFactor * 0.6;

      // 更新拖尾
      particle.trail.unshift({ x: p.x, y: p.y });
      if (particle.trail.length > particle.trailLength) {
        particle.trail.pop();
      }

      // 绘制拖尾
      if (particle.trail.length > 1) {
        ctx.beginPath();
        ctx.moveTo(particle.trail[0].x, particle.trail[0].y);
        for (let i = 1; i < particle.trail.length; i++) {
          ctx.lineTo(particle.trail[i].x, particle.trail[i].y);
        }
        ctx.strokeStyle = `rgba(${Math.min(255, ar + 100)}, ${Math.min(255, ag + 100)}, ${Math.min(255, ab + 100)}, ${alpha * 0.3})`;
        ctx.lineWidth = particle.size * 0.5;
        ctx.stroke();
      }

      // 绘制粒子头部
      ctx.beginPath();
      ctx.arc(p.x, p.y, particle.size * depthFactor, 0, Math.PI * 2);
      ctx.fillStyle = `rgba(${Math.min(255, ar + 150)}, ${Math.min(255, ag + 150)}, ${Math.min(255, ab + 150)}, ${alpha})`;
      ctx.fill();

      // 微弱光晕
      const glowGrad = ctx.createRadialGradient(p.x, p.y, 0, p.x, p.y, particle.size * 3 * depthFactor);
      glowGrad.addColorStop(0, `rgba(${Math.min(255, ar + 100)}, ${Math.min(255, ag + 100)}, ${Math.min(255, ab + 100)}, ${alpha * 0.4})`);
      glowGrad.addColorStop(1, 'transparent');
      ctx.fillStyle = glowGrad;
      ctx.beginPath();
      ctx.arc(p.x, p.y, particle.size * 3 * depthFactor, 0, Math.PI * 2);
      ctx.fill();
    });
  }

  // 绘制动态大气层脉冲
  drawPulsingAtmosphere(ctx, cx, cy, r) {
    const pulse = Math.sin(this.tick * 0.02) * 0.5 + 0.5;

    // 外层大气脉冲
    const atmoSize = r * (1.08 + pulse * 0.05);
    const atmoGrad = ctx.createRadialGradient(cx, cy, r * 0.9, cx, cy, atmoSize);
    atmoGrad.addColorStop(0, 'transparent');
    atmoGrad.addColorStop(0.3, this.themeRGBA(0.02 * pulse));
    atmoGrad.addColorStop(0.7, this.themeRGBA(0.04 * pulse));
    atmoGrad.addColorStop(1, 'transparent');

    ctx.fillStyle = atmoGrad;
    ctx.beginPath();
    ctx.arc(cx, cy, atmoSize, 0, Math.PI * 2);
    ctx.fill();

    // 边缘光晕脉冲
    const edgePulse = Math.sin(this.tick * 0.03) * 0.5 + 0.5;
    const glowSize = r * (1.12 + edgePulse * 0.03);
    const glowGrad = ctx.createRadialGradient(cx, cy, r, cx, cy, glowSize);
    glowGrad.addColorStop(0, this.themeRGBA(0.06 * edgePulse));
    glowGrad.addColorStop(0.5, this.themeRGBA(0.02 * edgePulse));
    glowGrad.addColorStop(1, 'transparent');

    ctx.fillStyle = glowGrad;
    ctx.beginPath();
    ctx.arc(cx, cy, glowSize, 0, Math.PI * 2);
    ctx.fill();
  }

  // 停止动画
  destroy() {
    if (this.animationId) {
      cancelAnimationFrame(this.animationId);
      this.animationId = null;
    }
  }
}
