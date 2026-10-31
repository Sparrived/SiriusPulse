import { useFrame } from '@react-three/fiber';
import { useMemo, useRef } from 'react';
import * as THREE from 'three';
import { daylight } from '@/lib/time';
import { useScene } from '@/state/scene';
import { useShader } from './shader';
import { glowTexture } from './textures';

const vertex = /* glsl */ `
  varying vec3 vNormal;
  varying vec2 vUv;
  void main() {
    vNormal = normalize(normalMatrix * normal);
    vUv = uv;
    gl_Position = projectionMatrix * modelViewMatrix * vec4(position, 1.0);
  }
`;

// 月面：柔和的明暗交界 + 几块淡淡的月海，不追求写实，只要「温柔的月亮」
const fragment = /* glsl */ `
  uniform float uBright;
  varying vec3 vNormal;
  varying vec2 vUv;
  float hash(vec2 p) { return fract(sin(dot(p, vec2(12.9898, 78.233))) * 43758.5453); }
  float noise(vec2 p) {
    vec2 i = floor(p), f = fract(p);
    f = f * f * (3.0 - 2.0 * f);
    return mix(mix(hash(i), hash(i + vec2(1, 0)), f.x), mix(hash(i + vec2(0, 1)), hash(i + 1.0), f.x), f.y);
  }
  void main() {
    float light = clamp(dot(vNormal, normalize(vec3(-0.35, 0.25, 1.0))), 0.0, 1.0);
    float maria = noise(vUv * 7.0) * 0.6 + noise(vUv * 15.0) * 0.4;
    vec3 base = mix(vec3(0.98, 0.97, 1.0), vec3(0.80, 0.82, 0.95), smoothstep(0.45, 0.75, maria) * 0.6);
    vec3 col = base * (0.55 + 0.45 * light) * uBright;
    gl_FragColor = vec4(col, 1.0);
  }
`;

export function Moon() {
  const group = useRef<THREE.Group>(null);
  const halo = useRef<THREE.SpriteMaterial>(null);
  const uniforms = useMemo(() => ({ uBright: { value: 1 } }), []);
  const material = useShader(
    () => ({ uniforms, vertexShader: vertex, fragmentShader: fragment }),
    [uniforms],
  );
  const glow = glowTexture();

  useFrame((state, dt) => {
    const { sleeping, mode } = useScene.getState();
    const night = sleeping || mode === 'door' ? 1 : 1 - daylight();
    const k = 1 - Math.exp(-dt * 0.8);
    // 白天月亮淡成天上的一枚白影；她睡着时月亮升得更高、更亮
    const targetY = sleeping ? 4.6 : 3.4 + night * 0.6;
    if (group.current) {
      group.current.position.y += (targetY - group.current.position.y) * k;
      group.current.rotation.z = Math.sin(state.clock.elapsedTime * 0.05) * 0.05;
    }
    uniforms.uBright.value += (0.55 + night * 0.5 - uniforms.uBright.value) * k;
    if (halo.current) halo.current.opacity += (0.15 + night * 0.65 - halo.current.opacity) * k;
  });

  return (
    <group ref={group} position={[5.6, 3.6, -14]}>
      <sprite scale={9}>
        <spriteMaterial
          ref={halo}
          map={glow}
          color="#cfd8ff"
          transparent
          depthWrite={false}
          blending={THREE.AdditiveBlending}
        />
      </sprite>
      <mesh material={material}>
        <sphereGeometry args={[1.15, 48, 48]} />
      </mesh>
    </group>
  );
}
