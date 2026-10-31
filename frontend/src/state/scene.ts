import { create } from 'zustand';

/** 世界的「镜头位置」：首页她站在中间，其它房间她退到角落陪着你，星图镜头抬向夜空。 */
export type SceneMode = 'door' | 'home' | 'room' | 'sky';

interface SceneState {
  mode: SceneMode;
  sleeping: boolean;
  valence: number;
  arousal: number;
  avatarUrl: string | null;
  /** 被摸头的次数：变化时场景放出小爱心 */
  pats: number;
  set: (patch: Partial<Omit<SceneState, 'set' | 'pat'>>) => void;
  pat: () => void;
}

export const useScene = create<SceneState>((set) => ({
  mode: 'door',
  sleeping: false,
  valence: 0.2,
  arousal: 0.3,
  avatarUrl: `${import.meta.env.BASE_URL}yuebai.webp`,
  pats: 0,
  set: (patch) => set(patch),
  pat: () => set((s) => ({ pats: s.pats + 1 })),
}));
