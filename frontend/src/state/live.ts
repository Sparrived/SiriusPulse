import { create } from 'zustand';

export type ThoughtKind = 'hear' | 'feel' | 'think' | 'tool' | 'reply' | 'quiet' | 'self' | 'remind';

export interface Thought {
  id: number;
  kind: ThoughtKind;
  text: string;
  at: number;
}

interface LiveState {
  thoughts: Thought[];
  /** 场景用来做呼应动画的「心跳」：每来一个事件 +1 */
  beat: number;
  lastKind: ThoughtKind | null;
  think: (kind: ThoughtKind, text: string) => void;
  forget: (id: number) => void;
  clear: () => void;
}

let seq = 0;
const MAX = 4;

export const useLive = create<LiveState>((set) => ({
  thoughts: [],
  beat: 0,
  lastKind: null,
  think: (kind, text) =>
    set((s) => {
      // 同一种念头连续出现时只更新文字，不刷屏
      const last = s.thoughts[s.thoughts.length - 1];
      if (last && last.kind === kind && last.text === text) {
        return { beat: s.beat + 1, lastKind: kind };
      }
      const next = [...s.thoughts, { id: ++seq, kind, text, at: Date.now() }].slice(-MAX);
      return { thoughts: next, beat: s.beat + 1, lastKind: kind };
    }),
  forget: (id) => set((s) => ({ thoughts: s.thoughts.filter((t) => t.id !== id) })),
  clear: () => set({ thoughts: [] }),
}));
