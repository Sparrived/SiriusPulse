import { create } from 'zustand';

// 每个标签页各自记住正在看哪个伙伴（sessionStorage）；新标签页沿用上一次的选择。
const TAB_KEY = 'sirius-persona';
const LAST_KEY = 'sirius-persona-last';

interface PersonaState {
  current: string | null;
  choose: (name: string) => void;
}

export const usePersonaStore = create<PersonaState>((set) => ({
  current: sessionStorage.getItem(TAB_KEY) ?? localStorage.getItem(LAST_KEY),
  choose: (name) => {
    sessionStorage.setItem(TAB_KEY, name);
    localStorage.setItem(LAST_KEY, name);
    set({ current: name });
  },
}));
