import { useFrame, useLoader, type ThreeEvent } from '@react-three/fiber';
import { Suspense, useMemo, useRef } from 'react';
import * as THREE from 'three';
import { moodTint } from '@/lib/mood';
import { useLive } from '@/state/live';
import { usePrefs } from '@/state/prefs';
import { useScene, type SceneMode } from '@/state/scene';
import { glowTexture } from './textures';
import { useShader } from './shader';

const HEIGHT = 5.4;

/** 各个镜头下她站在哪、多大、多亮 */
const POSES: Record<SceneMode, { pos: [number, number, number]; scale: number; bright: number }> = {
  door: { pos: [2.1, -0.55, -1.2], scale: 0.95, bright: 0.6 },
  home: { pos: [-1.75, -0.4, 0], scale: 0.94, bright: 1 },
  room: { pos: [-5.2, -1.95, -0.6], scale: 0.62, bright: 0.95 },
  sky: { pos: [-3.6, -6.5, 0], scale: 0.8, bright: 0.8 },
  jar: { pos: [-5.6, -2.35, -0.6], scale: 0.55, bright: 0.95 },
};

const vertex = /* glsl */ `
  uniform float uTime;
  uniform float uSway;
  uniform float uBreath;
  uniform float uWiggle;
  varying vec2 vUv;
  void main() {
    vUv = uv;
    vec3 p = position;
    float side = smoothstep(0.16, 0.48, abs(uv.x - 0.5));
    float lower = 1.0 - smoothstep(0.0, 0.5, uv.y);
    // 两侧的长发随风慢慢飘，越往下摆幅越大；裙摆有自己的节奏
    p.x += sin(uTime * 1.1 + uv.y * 3.6) * 0.05 * side * (1.15 - uv.y) * uSway;
    p.y += sin(uTime * 0.9 + uv.x * 5.0) * 0.012 * side * uSway;
    p.x += sin(uTime * 1.6 + uv.x * 7.0) * 0.022 * lower * uSway;
    // 呼吸：以脚下为锚点轻轻起伏，胸口附近略微舒展
    float breath = sin(uTime * 1.5);
    p.y += uv.y * breath * 0.026 * uBreath;
    p.x *= 1.0 + breath * 0.005 * uBreath * (1.0 - abs(uv.y - 0.5) * 2.0);
    // 被摸头时的一下小颤动
    p.x += sin(uTime * 38.0) * 0.02 * uWiggle * smoothstep(0.55, 1.0, uv.y);
    gl_Position = projectionMatrix * modelViewMatrix * vec4(p, 1.0);
  }
`;

const fragment = /* glsl */ `
  uniform sampler2D uMap;
  uniform float uBright;
  uniform float uSat;
  uniform float uOpacity;
  uniform vec3 uRim;
  uniform vec2 uTexel;
  varying vec2 vUv;
  void main() {
    vec4 c = texture2D(uMap, vUv);
    // 立绘的下沿融进云海里
    c.a *= smoothstep(0.0, 0.2, vUv.y);
    if (c.a < 0.01) discard;
    float g = dot(c.rgb, vec3(0.299, 0.587, 0.114));
    c.rgb = mix(vec3(g) * vec3(0.82, 0.86, 1.0), c.rgb, uSat);
    c.rgb *= uBright;
    // 轮廓光：沿着透明边缘描一圈月光，颜色跟着心情走
    vec2 o = uTexel * 6.0;
    float around = texture2D(uMap, vUv + vec2(o.x, 0.0)).a * texture2D(uMap, vUv - vec2(o.x, 0.0)).a
                 * texture2D(uMap, vUv + vec2(0.0, o.y)).a * texture2D(uMap, vUv - vec2(0.0, o.y)).a;
    float rim = (1.0 - around) * c.a;
    c.rgb += uRim * rim * 0.55;
    gl_FragColor = vec4(c.rgb, c.a * uOpacity);
  }
`;

function PortraitMesh({ url }: { url: string }) {
  const texture = useLoader(THREE.TextureLoader, url);
  const group = useRef<THREE.Group>(null);
  const halo = useRef<THREE.SpriteMaterial>(null);
  const wiggle = useRef(0);
  const pats = useRef(useScene.getState().pats);

  const aspect = texture.image ? texture.image.width / texture.image.height : 1;
  const uniforms = useMemo(() => {
    texture.colorSpace = THREE.SRGBColorSpace;
    texture.anisotropy = 4;
    return {
      uMap: { value: texture },
      uTime: { value: 0 },
      uSway: { value: 1 },
      uBreath: { value: 1 },
      uWiggle: { value: 0 },
      uBright: { value: 0.3 },
      uSat: { value: 1 },
      uOpacity: { value: 0 },
      uRim: { value: new THREE.Color('#bcd0ff') },
      uTexel: {
        value: new THREE.Vector2(1 / (texture.image?.width ?? 1024), 1 / (texture.image?.height ?? 1024)),
      },
    };
  }, [texture]);
  const tint = useMemo(() => new THREE.Color(), []);
  const head = useMemo(() => new THREE.Vector3(), []);
  const anchor = useRef<[number, number]>([0, 0]);

  useFrame((state, dt) => {
    const { mode, sleeping, valence } = useScene.getState();
    const { calm } = usePrefs.getState();
    const pose = POSES[mode];
    const k = 1 - Math.exp(-dt * 2.2);
    const g = group.current;
    if (!g) return;
    const t = state.clock.elapsedTime;

    const sp = useScene.getState().pats;
    if (sp !== pats.current) {
      pats.current = sp;
      wiggle.current = 1;
    }
    wiggle.current = Math.max(0, wiggle.current - dt * 2.5);

    // 位置：在各个镜头之间缓缓走过去；轻轻上下漂浮
    const bob = calm ? 0 : Math.sin(t * 0.7) * 0.06;
    g.position.x += (pose.pos[0] - g.position.x) * k;
    g.position.y += (pose.pos[1] + bob - g.position.y) * k;
    g.position.z += (pose.pos[2] - g.position.z) * k;
    const s = g.scale.x + (pose.scale - g.scale.x) * k;
    g.scale.setScalar(s);

    // 视差：她会微微朝你的鼠标转过来
    const px = calm ? 0 : state.pointer.x;
    const py = calm ? 0 : state.pointer.y;
    g.rotation.y += (px * 0.16 - g.rotation.y) * k;
    g.rotation.x += (-py * 0.05 - g.rotation.x) * k;
    g.rotation.z += ((sleeping ? -0.05 : 0) - g.rotation.z) * k;

    const u = uniforms;
    u.uTime.value = t;
    u.uSway.value += ((calm ? 0 : sleeping ? 0.35 : 1) - u.uSway.value) * k;
    u.uBreath.value += ((calm ? 0.25 : sleeping ? 0.6 : 1) - u.uBreath.value) * k;
    u.uWiggle.value = calm ? 0 : wiggle.current;
    u.uBright.value += ((sleeping ? 0.5 : pose.bright) - u.uBright.value) * k;
    u.uSat.value += ((sleeping ? 0.35 : 1) - u.uSat.value) * k;
    u.uOpacity.value += (1 - u.uOpacity.value) * k;
    tint.copy(moodTint(valence));
    u.uRim.value.lerp(tint, k);
    if (halo.current) {
      halo.current.color.lerp(tint, k);
      halo.current.opacity += ((sleeping ? 0.18 : mode === 'door' ? 0.12 : 0.42) - halo.current.opacity) * k;
    }

    // 把她头顶旁边那一点投影到屏幕上，DOM 里的小念头气泡就跟着她走
    head
      .set(HEIGHT * aspect * 0.32, HEIGHT * 0.42, 0)
      .applyMatrix4(g.matrixWorld)
      .project(state.camera);
    const x = Math.round(((head.x + 1) / 2) * state.size.width);
    const y = Math.round(((1 - head.y) / 2) * state.size.height);
    if (x !== anchor.current[0] || y !== anchor.current[1]) {
      anchor.current = [x, y];
      const root = document.documentElement.style;
      root.setProperty('--her-x', `${x}px`);
      root.setProperty('--her-y', `${y}px`);
      root.setProperty('--her-scale', s.toFixed(3));
    }
  });

  const onPat = (e: ThreeEvent<PointerEvent>) => {
    e.stopPropagation();
    const { mode, sleeping, pat } = useScene.getState();
    if (mode === 'door') return;
    pat();
    const lines = sleeping
      ? ['唔……再睡五分钟……', '（翻了个身）', 'Zzz……嘿嘿……']
      : ['嘿嘿，被摸摸头了', '欸？找我有事吗～', '再摸就要收费啦（骗你的）', '今天也辛苦啦'];
    useLive.getState().think('feel', lines[Math.floor(Math.random() * lines.length)]);
  };

  const material = useShader(
    () => ({
      uniforms: uniforms,
      vertexShader: vertex,
      fragmentShader: fragment,
      transparent: true,
      depthWrite: false,
    }),
    [uniforms],
  );

  return (
    <group ref={group} position={POSES.door.pos}>
      <sprite position={[0, 0.4, -0.4]} scale={[HEIGHT * 1.15, HEIGHT * 1.15, 1]}>
        <spriteMaterial
          ref={halo}
          map={glowTexture()}
          transparent
          opacity={0}
          depthWrite={false}
          blending={THREE.AdditiveBlending}
        />
      </sprite>
      <mesh
        material={material}
        onPointerDown={onPat}
        onPointerOver={() => (document.body.style.cursor = 'pointer')}
        onPointerOut={() => (document.body.style.cursor = '')}
      >
        <planeGeometry args={[HEIGHT * aspect, HEIGHT, 72, 72]} />
      </mesh>
    </group>
  );
}

export function Portrait() {
  const url = useScene((s) => s.avatarUrl);
  if (!url) return null;
  return (
    <Suspense fallback={null}>
      <PortraitMesh key={url} url={url} />
    </Suspense>
  );
}
