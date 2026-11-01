import { useFrame } from '@react-three/fiber';
import { useEffect, useMemo, useRef } from 'react';
import * as THREE from 'three';
import { NEW_STAR, starAnchors, useScene, type StarInfo } from '@/state/scene';
import { glowTexture } from './textures';

function hash(s: string): number {
  let h = 2166136261;
  for (let i = 0; i < s.length; i++) h = Math.imul(h ^ s.charCodeAt(i), 16777619);
  return ((h >>> 0) % 1000) / 1000;
}

/** 星星的位置：按名字排成一道起伏的弧，名字决定一点点抖动，所以每次打开都在同一个地方 */
export function layoutStars(names: string[]): Map<string, THREE.Vector3> {
  const slots = [...names, NEW_STAR];
  const n = slots.length;
  const out = new Map<string, THREE.Vector3>();
  slots.forEach((name, i) => {
    const t = n === 1 ? 0.5 : i / (n - 1);
    const j = name === NEW_STAR ? 0.5 : hash(name);
    out.set(
      name,
      new THREE.Vector3(
        -5.8 + 11.6 * t + (j - 0.5) * 0.8,
        7.3 + Math.sin(t * Math.PI * 1.7 + 0.6) * 1.0 + (j - 0.5) * 0.6,
        -9 + (j - 0.5) * 1.6,
      ),
    );
  });
  return out;
}

let ring: THREE.Texture | null = null;
function ringTexture() {
  if (ring) return ring;
  const c = document.createElement('canvas');
  c.width = c.height = 128;
  const g = c.getContext('2d')!;
  g.strokeStyle = 'rgba(255,255,255,0.9)';
  g.lineWidth = 3;
  g.setLineDash([7, 9]);
  g.beginPath();
  g.arc(64, 64, 50, 0, Math.PI * 2);
  g.stroke();
  ring = new THREE.CanvasTexture(c);
  return ring;
}

const AWAKE = new THREE.Color('#ffe9c4');
const ASLEEP = new THREE.Color('#9fb0ff');
const tmp = new THREE.Vector3();

interface Live {
  halo: THREE.SpriteMaterial;
  core: THREE.SpriteMaterial;
  group: THREE.Group;
}

/** 星图：每位伙伴是一颗星。醒着的暖白发亮，睡着的淡蓝；新来的伙伴会「啪」地亮起来。 */
export function Constellation() {
  const stars = useScene((s) => s.stars);
  const root = useRef<THREE.Group>(null);
  const lines = useRef<THREE.LineSegments>(null);
  const live = useRef(new Map<string, Live>());
  const known = useRef<Set<string> | null>(null);
  const fade = useRef(0);

  const names = useMemo(() => stars.map((s) => s.name).sort(), [stars]);
  const layout = useMemo(() => layoutStars(names), [names]);
  const byName = useMemo(() => new Map(stars.map((s) => [s.name, s] as [string, StarInfo])), [stars]);

  // 第一次拿到名单时全部算「早就在了」；之后出现的新名字放一次诞生的光
  const bornAt = useMemo(() => {
    const now = performance.now() / 1000;
    const map = new Map<string, number>();
    if (known.current === null && names.length) known.current = new Set(names);
    for (const name of names) map.set(name, known.current?.has(name) ? -10 : now);
    names.forEach((name) => known.current?.add(name));
    return map;
  }, [names]);

  const lineGeometry = useMemo(() => {
    const pts: number[] = [];
    const order = [...names, NEW_STAR];
    for (let i = 0; i < order.length - 1; i++) {
      pts.push(...layout.get(order[i])!.toArray(), ...layout.get(order[i + 1])!.toArray());
    }
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.Float32BufferAttribute(pts, 3));
    return g;
  }, [names, layout]);

  // 虚线需要每段的长度，几何体变了（来了新伙伴）才重算
  useEffect(() => {
    lines.current?.computeLineDistances();
  }, [lineGeometry]);

  useFrame((state, dt) => {
    const { mode, focusStar } = useScene.getState();
    const target = mode === 'sky' ? 1 : 0;
    fade.current += (target - fade.current) * (1 - Math.exp(-dt * 2.5));
    if (root.current) root.current.visible = fade.current > 0.01;
    if (!root.current?.visible) return;
    if (lines.current) {
      (lines.current.material as THREE.LineDashedMaterial).opacity = 0.35 * fade.current;
    }
    const t = state.clock.elapsedTime;
    const now = performance.now() / 1000;
    for (const [name, pos] of layout) {
      const star = live.current.get(name);
      if (!star) continue;
      const isNew = name === NEW_STAR;
      const info = byName.get(name);
      const focused = focusStar === name;
      const age = now - (bornAt.get(name) ?? -10);
      // 诞生：先猛地一亮，再慢慢落回平常的大小
      const birth = age < 3 ? Math.exp(-age * 1.6) * 3 : 0;
      const grow = Math.min(1, Math.max(0, age) * 1.2);
      const twinkle = 1 + Math.sin(t * (info?.awake ? 2.2 : 0.9) + hash(name) * 20) * 0.08;
      const base = isNew ? 1.1 : info?.awake ? 1.6 : 1.05;
      const s = (base * (focused ? 1.35 : 1) * twinkle + birth) * (isNew ? 1 : grow);
      star.group.scale.setScalar(s);
      star.group.position.copy(pos);
      const color = isNew ? ASLEEP : info?.awake ? AWAKE : ASLEEP;
      star.halo.color.lerp(color, 0.1);
      star.halo.opacity = (isNew ? 0.35 + Math.sin(t * 1.6) * 0.15 : info?.awake ? 0.8 : 0.4) * fade.current;
      star.core.opacity = (isNew ? 0.8 : 1) * fade.current;
      if (isNew) star.core.rotation = t * 0.25;

      const el = starAnchors.get(name);
      if (el) {
        tmp.copy(pos).project(state.camera);
        const x = ((tmp.x + 1) / 2) * state.size.width;
        const y = ((1 - tmp.y) / 2) * state.size.height;
        el.style.transform = `translate3d(${x.toFixed(1)}px, ${y.toFixed(1)}px, 0)`;
        el.style.opacity = String(fade.current);
      }
    }
  });

  const glow = glowTexture();
  return (
    <group ref={root} visible={false}>
      <lineSegments ref={lines} geometry={lineGeometry}>
        <lineDashedMaterial
          color="#dfe6ff"
          dashSize={0.18}
          gapSize={0.22}
          transparent
          opacity={0}
          depthWrite={false}
        />
      </lineSegments>
      {[...layout.keys()].map((name) => (
        <group
          key={name}
          ref={(g) => {
            if (!g) {
              live.current.delete(name);
              return;
            }
            const [halo, core] = g.children as THREE.Sprite[];
            live.current.set(name, {
              group: g,
              halo: halo.material as THREE.SpriteMaterial,
              core: core.material as THREE.SpriteMaterial,
            });
          }}
        >
          <sprite scale={1.6}>
            <spriteMaterial
              map={glow}
              transparent
              opacity={0}
              depthWrite={false}
              blending={THREE.AdditiveBlending}
            />
          </sprite>
          <sprite scale={name === NEW_STAR ? 0.9 : 0.32}>
            <spriteMaterial
              map={name === NEW_STAR ? ringTexture() : glow}
              color="#ffffff"
              transparent
              opacity={0}
              depthWrite={false}
              blending={THREE.AdditiveBlending}
            />
          </sprite>
        </group>
      ))}
    </group>
  );
}
