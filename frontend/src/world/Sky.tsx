import { useFrame } from '@react-three/fiber';
import { useMemo } from 'react';
import * as THREE from 'three';
import { dayPhase, type DayPhase } from '@/lib/time';
import { moodTint } from '@/lib/mood';
import { useScene } from '@/state/scene';
import { useShader } from './shader';

const PALETTES: Record<DayPhase, [string, string, string]> = {
  night: ['#0a0f2e', '#1c2453', '#3d3f7c'],
  dawn: ['#2b3878', '#a8a3d9', '#ffcdc4'],
  day: ['#6f98e6', '#b5cbf6', '#eef1ff'],
  dusk: ['#262f6a', '#8a7bc4', '#ffb59d'],
};
const SLEEP: [string, string, string] = ['#070a22', '#151b44', '#2b2d63'];

const vertex = /* glsl */ `
  varying vec3 vDir;
  void main() {
    vDir = normalize(position);
    gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
  }
`;

const fragment = /* glsl */ `
  uniform vec3 uTop;
  uniform vec3 uMid;
  uniform vec3 uBottom;
  uniform vec3 uTint;
  uniform float uTime;
  varying vec3 vDir;

  float hash(vec2 p) { return fract(sin(dot(p, vec2(127.1, 311.7))) * 43758.5453); }

  void main() {
    float h = vDir.y;
    vec3 col = mix(uBottom, uMid, smoothstep(-0.25, 0.18, h));
    col = mix(col, uTop, smoothstep(0.18, 0.75, h));
    // 心情的颜色像一层薄雾贴着地平线
    float band = exp(-pow((h + 0.02) * 4.2, 2.0));
    col = mix(col, col * 0.55 + uTint * 0.55, band * 0.35);
    // 极轻的抖动，消除渐变色带
    col += (hash(gl_FragCoord.xy + uTime) - 0.5) / 255.0;
    gl_FragColor = vec4(col, 1.0);
  }
`;

export function Sky() {
  const material = useShader(() => ({
    uniforms: {
      uTop: { value: new THREE.Color(PALETTES.night[0]) },
      uMid: { value: new THREE.Color(PALETTES.night[1]) },
      uBottom: { value: new THREE.Color(PALETTES.night[2]) },
      uTint: { value: new THREE.Color('#9fb8ff') },
      uTime: { value: 0 },
    },
    vertexShader: vertex,
    fragmentShader: fragment,
    side: THREE.BackSide,
    depthWrite: false,
  }));
  const uniforms = material.uniforms;
  const target = useMemo(() => [new THREE.Color(), new THREE.Color(), new THREE.Color()], []);

  useFrame((state, dt) => {
    const { sleeping, valence, mode } = useScene.getState();
    const palette = sleeping || mode === 'door' ? SLEEP : PALETTES[dayPhase()];
    palette.forEach((hex, i) => target[i].set(hex));
    const k = 1 - Math.exp(-dt * 0.9);
    uniforms.uTop.value.lerp(target[0], k);
    uniforms.uMid.value.lerp(target[1], k);
    uniforms.uBottom.value.lerp(target[2], k);
    uniforms.uTint.value.lerp(moodTint(valence), k);
    uniforms.uTime.value = state.clock.elapsedTime % 100;
  });

  return (
    <mesh scale={60} renderOrder={-10} material={material}>
      <sphereGeometry args={[1, 48, 32]} />
    </mesh>
  );
}
