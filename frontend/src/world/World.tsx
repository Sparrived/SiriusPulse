import { Canvas, useFrame, useThree } from '@react-three/fiber';
import { Bloom, EffectComposer, Noise, Vignette } from '@react-three/postprocessing';
import { useMemo } from 'react';
import * as THREE from 'three';
import { usePrefs } from '@/state/prefs';
import { useScene, type SceneMode } from '@/state/scene';
import { Constellation } from './Constellation';
import { Mist } from './Mist';
import { Moon } from './Moon';
import { Hearts, Motes } from './Motes';
import { Portrait } from './Portrait';
import { Sky } from './Sky';
import { Stars } from './Stars';

const CAMERA: Record<SceneMode, { pos: [number, number, number]; look: [number, number, number] }> = {
  door: { pos: [0, 0.2, 9.5], look: [0, 0.6, 0] },
  home: { pos: [0, 0.15, 8], look: [0, 0.35, 0] },
  room: { pos: [0.4, 0.4, 8.6], look: [0.2, 0.6, 0] },
  sky: { pos: [0, 1.5, 7], look: [0, 6, -10] },
};

/** 镜头在各个房间之间平滑移动；鼠标带来一点点景深视差。 */
function CameraRig() {
  const look = useMemo(() => new THREE.Vector3(0, 0.5, 0), []);
  const goal = useMemo(() => new THREE.Vector3(), []);
  const { camera } = useThree();

  useFrame((state, dt) => {
    const { mode } = useScene.getState();
    const { calm } = usePrefs.getState();
    const shot = CAMERA[mode];
    const k = 1 - Math.exp(-dt * (calm ? 4 : 1.6));
    const px = calm ? 0 : state.pointer.x * 0.35;
    const py = calm ? 0 : state.pointer.y * 0.18;
    goal.set(shot.pos[0] + px, shot.pos[1] + py, shot.pos[2]);
    camera.position.lerp(goal, k);
    goal.set(...shot.look);
    look.lerp(goal, k);
    camera.lookAt(look);
  });
  return null;
}

export function World() {
  const lowPower = usePrefs((s) => s.lowPower);
  return (
    <Canvas
      className="world-canvas"
      dpr={lowPower ? 1 : [1, 1.75]}
      gl={{ antialias: !lowPower, powerPreference: lowPower ? 'low-power' : 'high-performance' }}
      camera={{ fov: 40, near: 0.1, far: 200, position: CAMERA.door.pos }}
      style={{ position: 'fixed', inset: 0 }}
      aria-hidden
      // 调试用：开发模式下可在控制台通过 __r3f 查看场景
      onCreated={(state) => import.meta.env.DEV && ((window as any).__r3f = state)}
    >
      <Sky />
      <Stars count={lowPower ? 500 : 1400} />
      <Moon />
      <Constellation />
      <Mist />
      <Portrait />
      <Motes count={lowPower ? 24 : 70} />
      <Hearts />
      <CameraRig />
      {!lowPower && (
        <EffectComposer multisampling={0}>
          <Bloom mipmapBlur intensity={0.55} luminanceThreshold={0.72} luminanceSmoothing={0.25} />
          <Noise opacity={0.03} />
          <Vignette offset={0.28} darkness={0.55} />
        </EffectComposer>
      )}
    </Canvas>
  );
}
