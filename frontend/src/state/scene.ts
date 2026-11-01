import { create } from 'zustand';

/**
 * 世界的「镜头位置」：首页她站在中间，其它房间她退到角落陪着你，星图镜头抬向夜空，
 * 回忆里镜头对着一只装满萤火的玻璃瓶。
 */
export type SceneMode = 'door' | 'home' | 'room' | 'sky' | 'jar';

/** 记忆瓶里的一只萤火：一条记忆单元 */
export interface Firefly {
  id: string;
  type: string;
  salience: number;
  confidence: number;
  /** 不符合当前筛选时变暗 */
  dim: boolean;
}

/** 星图里的一颗星：一位伙伴 */
export interface StarInfo {
  name: string;
  awake: boolean;
}

/** 星图里那个还空着的位置（迎接新伙伴） */
export const NEW_STAR = '__new__';

interface SceneState {
  mode: SceneMode;
  sleeping: boolean;
  valence: number;
  arousal: number;
  avatarUrl: string | null;
  /** 被摸头的次数：变化时场景放出小爱心 */
  pats: number;
  stars: StarInfo[];
  focusStar: string | null;
  fireflies: Firefly[];
  /** 选中/悬停的记忆 */
  focusMemory: string | null;
  hoverMemory: string | null;
  /** 悬停来自瓶子里的萤火（才显示跟着鼠标的小纸条），而不是右边的列表 */
  hoverFromJar: boolean;
  /** 正在整理回忆：瓶子会轻轻发亮 */
  jarBusy: boolean;
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
  stars: [],
  focusStar: null,
  fireflies: [],
  focusMemory: null,
  hoverMemory: null,
  hoverFromJar: false,
  jarBusy: false,
  set: (patch) => set(patch),
  pat: () => set((s) => ({ pats: s.pats + 1 })),
}));

/**
 * DOM 标签的登记处：星图里每颗星的名字牌是普通 DOM，3D 场景每帧把星星投影到屏幕上，
 * 直接改这些元素的 transform（不经过 React 渲染，拖动视角时也跟得上）。
 */
export const starAnchors = new Map<string, HTMLElement>();
