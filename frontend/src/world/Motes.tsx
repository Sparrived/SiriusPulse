import { useFrame } from '@react-three/fiber';
import { useEffect, useMemo, useRef } from 'react';
import * as THREE from 'three';
import { useScene } from '@/state/scene';
import { glowTexture, heartTexture } from './textures';
import { useShader } from './shader';

const moteVertex = /* glsl */ `
  attribute float aSeed;
  uniform float uTime;
  uniform float uPixelRatio;
  uniform float uEnergy;
  varying float vAlpha;
  void main() {
    vec3 p = position;
    float t = uTime * (0.05 + aSeed * 0.06) * (0.6 + uEnergy);
    // 慢慢往上飘，飘到顶再从云里重新冒出来
    p.y = mod(p.y + t * 3.0 + 4.0, 9.0) - 4.0;
    p.x += sin(uTime * 0.4 + aSeed * 30.0) * 0.35;
    p.z += cos(uTime * 0.3 + aSeed * 20.0) * 0.25;
    vec4 mv = modelViewMatrix * vec4(p, 1.0);
    vAlpha = smoothstep(-4.0, -2.8, p.y) * smoothstep(5.0, 2.5, p.y) * (0.5 + 0.5 * sin(uTime * 2.0 + aSeed * 50.0));
    gl_PointSize = (14.0 + aSeed * 18.0) * uPixelRatio / -mv.z;
    gl_Position = projectionMatrix * mv;
  }
`;

const moteFragment = /* glsl */ `
  uniform sampler2D uMap;
  uniform vec3 uColor;
  varying float vAlpha;
  void main() {
    vec4 tex = texture2D(uMap, gl_PointCoord);
    gl_FragColor = vec4(uColor, tex.a * vAlpha * 0.9);
  }
`;

/** 漂浮的小光点：像萤火，也像月光里的尘埃。她越兴奋，飘得越快。 */
export function Motes({ count = 70 }: { count?: number }) {
  const { geometry, uniforms } = useMemo(() => {
    const positions = new Float32Array(count * 3);
    const seeds = new Float32Array(count);
    for (let i = 0; i < count; i++) {
      positions.set([(Math.random() - 0.5) * 16, Math.random() * 9 - 4, (Math.random() - 0.5) * 6], i * 3);
      seeds[i] = Math.random();
    }
    const g = new THREE.BufferGeometry();
    g.setAttribute('position', new THREE.BufferAttribute(positions, 3));
    g.setAttribute('aSeed', new THREE.BufferAttribute(seeds, 1));
    return {
      geometry: g,
      uniforms: {
        uTime: { value: 0 },
        uPixelRatio: { value: Math.min(window.devicePixelRatio, 2) * 4 },
        uEnergy: { value: 0.3 },
        uMap: { value: glowTexture() },
        uColor: { value: new THREE.Color('#ffe7b8') },
      },
    };
  }, [count]);

  useFrame((state, dt) => {
    const { arousal, sleeping } = useScene.getState();
    uniforms.uTime.value = state.clock.elapsedTime;
    const target = sleeping ? 0.1 : arousal;
    uniforms.uEnergy.value += (target - uniforms.uEnergy.value) * (1 - Math.exp(-dt));
  });

  const material = useShader(
    () => ({
      uniforms: uniforms,
      vertexShader: moteVertex,
      fragmentShader: moteFragment,
      transparent: true,
      depthWrite: false,
      blending: THREE.AdditiveBlending,
    }),
    [uniforms],
  );

  return <points geometry={geometry} material={material}></points>;
}

interface Heart {
  sprite: THREE.Sprite;
  life: number;
  vx: number;
  vy: number;
  spin: number;
}

/** 摸头时从她头顶冒出来的小爱心 */
export function Hearts() {
  const group = useRef<THREE.Group>(null);
  const hearts = useRef<Heart[]>([]);
  const map = useMemo(() => heartTexture(), []);

  useEffect(
    () =>
      useScene.subscribe((s, prev) => {
        if (s.pats === prev.pats || !group.current) return;
        const origin =
          s.mode === 'room' ? new THREE.Vector3(-5.2, 0.2, 0) : new THREE.Vector3(-1.75, 2.1, 0.3);
        for (let i = 0; i < 5; i++) {
          const material = new THREE.SpriteMaterial({ map, transparent: true, depthWrite: false });
          const sprite = new THREE.Sprite(material);
          sprite.position
            .copy(origin)
            .add(new THREE.Vector3((Math.random() - 0.5) * 0.8, Math.random() * 0.3, 0));
          sprite.scale.setScalar(0.01);
          group.current.add(sprite);
          hearts.current.push({
            sprite,
            life: 0,
            vx: (Math.random() - 0.5) * 0.9,
            vy: 0.9 + Math.random() * 0.7,
            spin: (Math.random() - 0.5) * 2,
          });
        }
      }),
    [map],
  );

  useFrame((_, dt) => {
    hearts.current = hearts.current.filter((h) => {
      h.life += dt;
      const p = h.life / 1.6;
      if (p >= 1) {
        group.current?.remove(h.sprite);
        h.sprite.material.dispose();
        return false;
      }
      h.sprite.position.x += h.vx * dt;
      h.sprite.position.y += h.vy * dt;
      h.vy *= 0.985;
      const pop = p < 0.15 ? p / 0.15 : 1;
      h.sprite.scale.setScalar(0.42 * pop * (1 + Math.sin(h.life * 9) * 0.06));
      h.sprite.material.rotation = Math.sin(h.life * 3 + h.spin) * 0.3;
      h.sprite.material.opacity = 1 - Math.max(0, (p - 0.6) / 0.4);
      return true;
    });
  });

  return <group ref={group} />;
}
