import { useEffect, useMemo } from 'react';
import * as THREE from 'three';

/**
 * 自己持有 ShaderMaterial。
 *
 * R3F 9.8 在应用 `<shaderMaterial uniforms={...}>` 时会把每个 uniform 浅拷贝成新的
 * `{ value }` 包装对象：之后在 useFrame 里给自己那份 `uniforms.x.value = n` 赋数字，
 * GPU 永远看不到（只有原地修改的 Color/Vector 碰巧能生效）。直接构造材质再通过
 * `material` 属性挂到 mesh 上，uniform 的引用就始终是同一个。
 */
export function useShader<U extends Record<string, THREE.IUniform>>(
  make: () => { uniforms: U } & Omit<THREE.ShaderMaterialParameters, 'uniforms'>,
  deps: unknown[] = [],
): THREE.ShaderMaterial & { uniforms: U } {
  // eslint-disable-next-line react-hooks/exhaustive-deps
  const material = useMemo(() => new THREE.ShaderMaterial(make()), deps);
  useEffect(() => () => material.dispose(), [material]);
  return material as THREE.ShaderMaterial & { uniforms: U };
}
