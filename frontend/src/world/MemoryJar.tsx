import { useFrame, useThree, type ThreeEvent } from '@react-three/fiber';
import { useEffect, useMemo, useRef } from 'react';
import * as THREE from 'three';
import { memoryColor } from '@/lib/memory-kinds';
import { useScene } from '@/state/scene';
import { useShader } from './shader';
import { glowTexture } from './textures';

const JAR_POS = new THREE.Vector3(-2.5, 0.15, 0);
const MAX = 2000;

function hash(s: string, salt = 0): number {
  let h = 2166136261 ^ salt;
  for (let i = 0; i < s.length; i++) h = Math.imul(h ^ s.charCodeAt(i), 16777619);
  return ((h >>> 0) % 100000) / 100000;
}

/** 瓶身轮廓：圆肚子、收口、一圈瓶唇 */
function jarProfile(): THREE.Vector2[] {
  const pts: THREE.Vector2[] = [];
  const add = (x: number, y: number) => pts.push(new THREE.Vector2(x, y));
  add(0.01, -1.55);
  add(0.9, -1.55);
  add(1.12, -1.4);
  add(1.22, -1.0);
  add(1.24, 0.3);
  add(1.16, 0.95);
  add(0.9, 1.3);
  add(0.72, 1.45);
  add(0.72, 1.62);
  add(0.8, 1.66);
  add(0.8, 1.74);
  add(0.7, 1.76);
  return pts;
}

const glassVertex = /* glsl */ `
  varying vec3 vNormal;
  varying vec3 vView;
  varying float vY;
  void main() {
    vec4 mv = modelViewMatrix * vec4(position, 1.0);
    vNormal = normalize(normalMatrix * normal);
    vView = normalize(-mv.xyz);
    vY = position.y;
    gl_Position = projectionMatrix * mv;
  }
`;

// 玻璃：中间几乎透明，边缘（菲涅尔）亮起来，再加一道竖着的高光
const glassFragment = /* glsl */ `
  uniform float uGlow;
  uniform float uBack;
  uniform vec3 uTint;
  varying vec3 vNormal;
  varying vec3 vView;
  varying float vY;
  void main() {
    float f = 1.0 - abs(dot(normalize(vNormal), normalize(vView)));
    float rim = pow(f, 2.4);
    float streak = smoothstep(0.82, 0.9, vNormal.x * 0.6 + vNormal.z * 0.8) * smoothstep(-1.2, 0.6, vY) * 0.5;
    float a = (0.015 + rim * 0.36 + streak * 0.6) * (uBack > 0.5 ? 0.3 : 1.0);
    vec3 col = mix(vec3(0.85, 0.9, 1.0), uTint, 0.25) + uGlow * uTint * 0.6;
    gl_FragColor = vec4(col, a + uGlow * 0.12);
  }
`;

const flyVertex = /* glsl */ `
  attribute vec3 aColor;
  attribute float aSize;
  attribute float aBright;
  uniform float uPixelRatio;
  uniform float uTime;
  varying vec3 vColor;
  varying float vBright;
  void main() {
    vec4 mv = modelViewMatrix * vec4(position, 1.0);
    vColor = aColor;
    vBright = aBright * (0.75 + 0.25 * sin(uTime * 2.3 + position.x * 13.0 + position.y * 7.0));
    gl_PointSize = aSize * uPixelRatio / -mv.z;
    gl_Position = projectionMatrix * mv;
  }
`;

const flyFragment = /* glsl */ `
  uniform sampler2D uMap;
  varying vec3 vColor;
  varying float vBright;
  void main() {
    float a = texture2D(uMap, gl_PointCoord).a;
    vec3 core = mix(vColor, vec3(1.0), smoothstep(0.55, 1.0, a) * 0.7);
    gl_FragColor = vec4(core, a * vBright);
  }
`;

/** 记忆瓶：她记得的每件事都是瓶子里的一只萤火 */
export function MemoryJar() {
  const fireflies = useScene((s) => s.fireflies);
  const root = useRef<THREE.Group>(null);
  const points = useRef<THREE.Points>(null);
  const fade = useRef(0);
  const ring = useRef<THREE.Sprite>(null);
  const { raycaster } = useThree();

  useEffect(() => {
    raycaster.params.Points = { threshold: 0.09 };
  }, [raycaster]);

  const lathe = useMemo(() => new THREE.LatheGeometry(jarProfile(), 64), []);
  const glassUniforms = (back: boolean) => ({
    uGlow: { value: 0 },
    uBack: { value: back ? 1 : 0 },
    uTint: { value: new THREE.Color('#bcd0ff') },
  });
  const front = useShader(() => ({
    uniforms: glassUniforms(false),
    vertexShader: glassVertex,
    fragmentShader: glassFragment,
    transparent: true,
    depthWrite: false,
    side: THREE.FrontSide,
  }));
  const back = useShader(() => ({
    uniforms: glassUniforms(true),
    vertexShader: glassVertex,
    fragmentShader: glassFragment,
    transparent: true,
    depthWrite: false,
    side: THREE.BackSide,
  }));

  const geometry = useMemo(() => {
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.BufferAttribute(new Float32Array(MAX * 3), 3));
    g.setAttribute('aColor', new THREE.BufferAttribute(new Float32Array(MAX * 3), 3));
    g.setAttribute('aSize', new THREE.BufferAttribute(new Float32Array(MAX), 1));
    g.setAttribute('aBright', new THREE.BufferAttribute(new Float32Array(MAX), 1));
    g.setDrawRange(0, 0);
    return g;
  }, []);
  const flies = useShader(() => ({
    uniforms: {
      uPixelRatio: { value: Math.min(window.devicePixelRatio, 2) * 34 },
      uTime: { value: 0 },
      uMap: { value: glowTexture() },
    },
    vertexShader: flyVertex,
    fragmentShader: flyFragment,
    transparent: true,
    depthWrite: false,
    blending: THREE.AdditiveBlending,
  }));

  // 每只萤火在瓶子里的「家」：由记忆 ID 决定，所以刷新后还在原来的地方
  const homes = useMemo(
    () =>
      fireflies.map((f) => {
        const r = Math.sqrt(hash(f.id, 1)) * 0.92;
        const a = hash(f.id, 2) * Math.PI * 2;
        return {
          x: Math.cos(a) * r,
          y: -1.25 + hash(f.id, 3) * 2.35,
          z: Math.sin(a) * r,
          phase: hash(f.id, 4) * 100,
          speed: 0.25 + hash(f.id, 5) * 0.4,
        };
      }),
    [fireflies],
  );

  useEffect(() => {
    const color = geometry.getAttribute('aColor') as THREE.BufferAttribute;
    const c = new THREE.Color();
    fireflies.slice(0, MAX).forEach((f, i) => {
      c.set(memoryColor(f.type));
      color.setXYZ(i, c.r, c.g, c.b);
    });
    color.needsUpdate = true;
    geometry.setDrawRange(0, Math.min(MAX, fireflies.length));
  }, [fireflies, geometry]);

  useFrame((state, dt) => {
    const { mode, focusMemory, hoverMemory, jarBusy } = useScene.getState();
    fade.current += ((mode === 'jar' ? 1 : 0) - fade.current) * (1 - Math.exp(-dt * 3));
    const visible = fade.current > 0.01;
    if (root.current) {
      root.current.visible = visible;
      root.current.scale.setScalar(0.85 + fade.current * 0.15);
    }
    if (!visible) return;
    const t = state.clock.elapsedTime;
    const glow = jarBusy ? 0.5 + Math.sin(t * 3) * 0.4 : 0;
    for (const m of [front, back]) m.uniforms.uGlow.value += (glow - m.uniforms.uGlow.value) * 0.1;
    flies.uniforms.uTime.value = t;

    const pos = geometry.getAttribute('position') as THREE.BufferAttribute;
    const size = geometry.getAttribute('aSize') as THREE.BufferAttribute;
    const bright = geometry.getAttribute('aBright') as THREE.BufferAttribute;
    let focusIndex = -1;
    fireflies.slice(0, MAX).forEach((f, i) => {
      const h = homes[i];
      // 在家附近慢慢绕圈飘
      const s = t * h.speed + h.phase;
      pos.setXYZ(i, h.x + Math.sin(s) * 0.12, h.y + Math.sin(s * 0.7) * 0.1, h.z + Math.cos(s * 0.9) * 0.12);
      const focused = f.id === focusMemory || f.id === hoverMemory;
      if (f.id === focusMemory) focusIndex = i;
      size.setX(i, (10 + f.salience * 22) * (focused ? 1.7 : 1));
      bright.setX(i, (f.dim ? 0.12 : 0.35 + f.confidence * 0.65) * fade.current * (focused ? 1.6 : 1));
    });
    pos.needsUpdate = true;
    size.needsUpdate = true;
    bright.needsUpdate = true;
    geometry.computeBoundingSphere();

    if (ring.current) {
      ring.current.visible = focusIndex >= 0;
      if (focusIndex >= 0) {
        ring.current.position.set(pos.getX(focusIndex), pos.getY(focusIndex), pos.getZ(focusIndex));
        ring.current.scale.setScalar(0.42 + Math.sin(t * 4) * 0.04);
      }
    }
  });

  // 隐藏的物体照样会被射线打中：不在回忆房间时一律不理
  const pick = (e: ThreeEvent<PointerEvent>) => {
    if (useScene.getState().mode !== 'jar' || e.index === undefined) return null;
    return fireflies[e.index] ?? null;
  };

  return (
    <group ref={root} position={JAR_POS} visible={false}>
      <mesh geometry={lathe} material={back} renderOrder={1} />
      <points
        ref={points}
        geometry={geometry}
        material={flies}
        renderOrder={2}
        onPointerMove={(e) => {
          const f = pick(e);
          if (useScene.getState().hoverMemory !== (f?.id ?? null))
            useScene.getState().set({ hoverMemory: f?.id ?? null, hoverFromJar: !!f });
          document.body.style.cursor = f ? 'pointer' : '';
          document.documentElement.style.setProperty('--tip-x', `${e.nativeEvent.clientX}px`);
          document.documentElement.style.setProperty('--tip-y', `${e.nativeEvent.clientY}px`);
        }}
        onPointerOut={() => {
          useScene.getState().set({ hoverMemory: null });
          document.body.style.cursor = '';
        }}
        onClick={(e) => {
          e.stopPropagation();
          const f = pick(e as unknown as ThreeEvent<PointerEvent>);
          if (f) useScene.getState().set({ focusMemory: f.id });
        }}
      />
      <sprite ref={ring} visible={false}>
        <spriteMaterial
          map={glowTexture()}
          color="#ffffff"
          transparent
          opacity={0.5}
          depthWrite={false}
          blending={THREE.AdditiveBlending}
        />
      </sprite>
      <mesh geometry={lathe} material={front} renderOrder={3} />
      {/* 软木塞 */}
      <mesh position={[0, 1.86, 0]} renderOrder={4}>
        <cylinderGeometry args={[0.66, 0.6, 0.32, 40]} />
        <meshBasicMaterial color="#c9a27a" transparent opacity={0.92} />
      </mesh>
      <mesh position={[0, 2.03, 0]} renderOrder={4}>
        <cylinderGeometry args={[0.68, 0.66, 0.04, 40]} />
        <meshBasicMaterial color="#dcbb94" transparent opacity={0.95} />
      </mesh>
      {/* 瓶底下一圈柔光 */}
      <sprite position={[0, -1.55, 0]} scale={[3.6, 1.1, 1]} renderOrder={0}>
        <spriteMaterial
          map={glowTexture()}
          color="#ffe2a8"
          transparent
          opacity={0.25}
          depthWrite={false}
          blending={THREE.AdditiveBlending}
        />
      </sprite>
    </group>
  );
}
