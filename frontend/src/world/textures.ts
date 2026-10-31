import * as THREE from 'three';

/** 程序生成的柔光贴图：月晕、光点、小爱心——不依赖任何外部图片。 */
function canvasTexture(size: number, draw: (ctx: CanvasRenderingContext2D, s: number) => void) {
  const canvas = document.createElement('canvas');
  canvas.width = canvas.height = size;
  draw(canvas.getContext('2d')!, size);
  const tex = new THREE.CanvasTexture(canvas);
  tex.colorSpace = THREE.SRGBColorSpace;
  return tex;
}

let glow: THREE.Texture | null = null;
export function glowTexture(): THREE.Texture {
  glow ??= canvasTexture(128, (ctx, s) => {
    const g = ctx.createRadialGradient(s / 2, s / 2, 0, s / 2, s / 2, s / 2);
    g.addColorStop(0, 'rgba(255,255,255,1)');
    g.addColorStop(0.25, 'rgba(255,255,255,0.55)');
    g.addColorStop(0.6, 'rgba(255,255,255,0.12)');
    g.addColorStop(1, 'rgba(255,255,255,0)');
    ctx.fillStyle = g;
    ctx.fillRect(0, 0, s, s);
  });
  return glow;
}

let heart: THREE.Texture | null = null;
export function heartTexture(): THREE.Texture {
  heart ??= canvasTexture(128, (ctx, s) => {
    ctx.translate(s / 2, s / 2 + 6);
    ctx.scale(s / 40, s / 40);
    ctx.beginPath();
    ctx.moveTo(0, 6);
    ctx.bezierCurveTo(-14, -4, -8, -16, 0, -8);
    ctx.bezierCurveTo(8, -16, 14, -4, 0, 6);
    ctx.closePath();
    ctx.shadowColor = 'rgba(255,150,180,0.9)';
    ctx.shadowBlur = 4;
    ctx.fillStyle = '#ff8fae';
    ctx.fill();
  });
  return heart;
}
