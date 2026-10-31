import { useFrame } from '@react-three/fiber';
import { useMemo } from 'react';
import * as THREE from 'three';
import { daylight } from '@/lib/time';
import { useScene } from '@/state/scene';
import { useShader } from './shader';

const vertex = /* glsl */ `
  varying vec2 vUv;
  void main() {
    vUv = uv;
    gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
  }
`;

// 分形噪声云：底部是一整片云海，她就站在云上
const fragment = /* glsl */ `
  uniform float uTime;
  uniform float uSeed;
  uniform float uDensity;
  uniform vec3 uColor;
  uniform float uOpacity;
  varying vec2 vUv;
  float hash(vec2 p) { return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453); }
  float noise(vec2 p) {
    vec2 i = floor(p), f = fract(p);
    f = f * f * (3.0 - 2.0 * f);
    return mix(mix(hash(i), hash(i + vec2(1, 0)), f.x), mix(hash(i + vec2(0, 1)), hash(i + 1.0), f.x), f.y);
  }
  float fbm(vec2 p) {
    float v = 0.0, a = 0.5;
    for (int i = 0; i < 5; i++) { v += a * noise(p); p *= 2.03; a *= 0.5; }
    return v;
  }
  void main() {
    vec2 p = vUv * vec2(6.0, 2.0) + vec2(uTime * 0.02 + uSeed, uSeed * 0.3);
    float n = fbm(p + fbm(p * 0.7 + uTime * 0.01));
    // 上沿是蓬松的云顶，越往下越实
    float top = smoothstep(1.0, 0.25, vUv.y + (n - 0.5) * 0.55);
    float edge = smoothstep(0.0, 0.12, vUv.x) * smoothstep(1.0, 0.88, vUv.x);
    float a = smoothstep(uDensity, 1.0, n + top * 0.6) * top * edge;
    vec3 col = mix(uColor * 0.82, uColor, smoothstep(0.3, 0.9, n));
    gl_FragColor = vec4(col, a * uOpacity);
  }
`;

interface Layer {
  y: number;
  z: number;
  w: number;
  h: number;
  seed: number;
  density: number;
  opacity: number;
}

const LAYERS: Layer[] = [
  { y: -3.9, z: -6, w: 46, h: 5, seed: 3.1, density: 0.35, opacity: 0.7 },
  { y: -3.5, z: -1.5, w: 34, h: 3.2, seed: 7.7, density: 0.3, opacity: 0.85 },
  { y: -3.35, z: 1.2, w: 26, h: 2.4, seed: 1.3, density: 0.25, opacity: 0.95 },
];

function MistLayer({ layer }: { layer: Layer }) {
  const uniforms = useMemo(
    () => ({
      uTime: { value: 0 },
      uSeed: { value: layer.seed },
      uDensity: { value: layer.density },
      uColor: { value: new THREE.Color('#dfe4ff') },
      uOpacity: { value: layer.opacity },
    }),
    [layer],
  );
  const night = useMemo(() => new THREE.Color('#5d6399'), []);
  const day = useMemo(() => new THREE.Color('#f3f5ff'), []);
  const target = useMemo(() => new THREE.Color(), []);

  useFrame((state, dt) => {
    const { sleeping, mode } = useScene.getState();
    const light = sleeping || mode === 'door' ? 0.1 : daylight();
    target.copy(night).lerp(day, 0.25 + light * 0.75);
    uniforms.uColor.value.lerp(target, 1 - Math.exp(-dt));
    uniforms.uTime.value = state.clock.elapsedTime;
  });

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
    <mesh position={[0, layer.y, layer.z]} material={material}>
      <planeGeometry args={[layer.w, layer.h]} />
    </mesh>
  );
}

export function Mist() {
  return (
    <group>
      {LAYERS.map((layer) => (
        <MistLayer key={layer.seed} layer={layer} />
      ))}
    </group>
  );
}
