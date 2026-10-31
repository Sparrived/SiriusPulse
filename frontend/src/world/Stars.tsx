import { useFrame } from '@react-three/fiber';
import { useEffect, useMemo } from 'react';
import * as THREE from 'three';
import { daylight } from '@/lib/time';
import { useLive } from '@/state/live';
import { useScene } from '@/state/scene';
import { useShader } from './shader';

const vertex = /* glsl */ `
  attribute float aSize;
  attribute float aPhase;
  uniform float uTime;
  uniform float uSpeed;
  uniform float uPulse;
  uniform float uPixelRatio;
  varying float vTwinkle;
  void main() {
    vec4 mv = modelViewMatrix * vec4(position, 1.0);
    float tw = 0.55 + 0.45 * sin(uTime * uSpeed * (0.6 + aPhase) + aPhase * 40.0);
    // 有新消息时，一圈亮光从天顶荡开
    float wave = uPulse * exp(-pow(position.y * 0.08 - (1.0 - uPulse) * 3.0, 2.0) * 2.0);
    vTwinkle = tw + wave;
    gl_PointSize = aSize * uPixelRatio * (1.0 + wave * 0.8) * (60.0 / -mv.z);
    gl_Position = projectionMatrix * mv;
  }
`;

const fragment = /* glsl */ `
  uniform float uOpacity;
  varying float vTwinkle;
  void main() {
    vec2 c = gl_PointCoord - 0.5;
    float d = length(c);
    float core = smoothstep(0.5, 0.0, d);
    // 一点点十字星芒
    float cross = max(0.0, 1.0 - abs(c.x) * 14.0) * max(0.0, 1.0 - abs(c.y) * 2.2)
                + max(0.0, 1.0 - abs(c.y) * 14.0) * max(0.0, 1.0 - abs(c.x) * 2.2);
    float a = (pow(core, 2.4) + cross * 0.25) * vTwinkle * uOpacity;
    gl_FragColor = vec4(vec3(0.92, 0.93, 1.0), a);
  }
`;

export function Stars({ count = 1400 }: { count?: number }) {
  const { geometry, uniforms } = useMemo(() => {
    const positions = new Float32Array(count * 3);
    const sizes = new Float32Array(count);
    const phases = new Float32Array(count);
    for (let i = 0; i < count; i++) {
      // 只撒在上半天球，越高越密
      const u = Math.random();
      const theta = Math.random() * Math.PI * 2;
      const y = Math.pow(u, 0.7) * 0.95 + 0.02;
      const r = 32 + Math.random() * 14;
      const rr = Math.sqrt(1 - y * y);
      positions.set([Math.cos(theta) * rr * r, y * r - 4, Math.sin(theta) * rr * r - 6], i * 3);
      sizes[i] = Math.random() < 0.06 ? 2.6 + Math.random() * 2 : 0.7 + Math.random() * 1.3;
      phases[i] = Math.random();
    }
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.BufferAttribute(positions, 3));
    g.setAttribute('aSize', new THREE.BufferAttribute(sizes, 1));
    g.setAttribute('aPhase', new THREE.BufferAttribute(phases, 1));
    return {
      geometry: g,
      uniforms: {
        uTime: { value: 0 },
        uSpeed: { value: 1 },
        uPulse: { value: 0 },
        uOpacity: { value: 1 },
        uPixelRatio: { value: Math.min(window.devicePixelRatio, 2) },
      },
    };
  }, [count]);

  useFrame((state, dt) => {
    const { sleeping, arousal, mode } = useScene.getState();
    const dark = sleeping || mode === 'door' ? 1 : 1 - daylight() * 0.92;
    const k = 1 - Math.exp(-dt * 1.2);
    uniforms.uOpacity.value += (dark - uniforms.uOpacity.value) * k;
    uniforms.uSpeed.value += (0.6 + arousal * 2.2 - uniforms.uSpeed.value) * k;
    uniforms.uPulse.value = Math.max(0, uniforms.uPulse.value - dt * 0.45);
    uniforms.uTime.value = state.clock.elapsedTime;
  });

  // 引擎每来一个事件，夜空就轻轻亮一下
  useEffect(
    () =>
      useLive.subscribe((s, prev) => {
        if (s.beat !== prev.beat) uniforms.uPulse.value = 1;
      }),
    [uniforms],
  );

  const material = useShader(
    () => ({
      uniforms: uniforms,
      vertexShader: vertex,
      fragmentShader: fragment,
      transparent: true,
      depthWrite: false,
      blending: THREE.AdditiveBlending,
    }),
    [uniforms],
  );

  return <points geometry={geometry} renderOrder={-5} material={material}></points>;
}
