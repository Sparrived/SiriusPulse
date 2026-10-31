import { create } from 'zustand';

const KEY = 'sirius-prefs';

export interface Prefs {
  /** 安静模式：关闭摇摆、视差与大幅转场 */
  calm: boolean;
  /** 省电模式：减少粒子、关闭后期光晕 */
  lowPower: boolean;
}

function load(): Prefs {
  const reduce = window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false;
  try {
    const saved = JSON.parse(localStorage.getItem(KEY) ?? 'null');
    if (saved && typeof saved === 'object') return { calm: reduce, lowPower: false, ...saved };
  } catch {
    /* 损坏的偏好直接用默认值 */
  }
  return { calm: reduce, lowPower: false };
}

interface PrefsState extends Prefs {
  toggle: (key: keyof Prefs) => void;
}

export const usePrefs = create<PrefsState>((set, get) => ({
  ...load(),
  toggle: (key) => {
    const next = { calm: get().calm, lowPower: get().lowPower, [key]: !get()[key] };
    localStorage.setItem(KEY, JSON.stringify(next));
    set(next);
  },
}));
