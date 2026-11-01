import { QueryClient, useMutation, useQuery } from '@tanstack/react-query';
import { api } from './api';
import type {
  Account,
  AccountsResponse,
  DispatchOverview,
  Experience,
  Glance,
  Health,
  Metrics,
  NapCatAdapter,
  PersonasResponse,
  Profile,
} from './types';
import { usePersonaStore } from '@/state/persona';

export const queryClient = new QueryClient({
  defaultOptions: {
    queries: { staleTime: 15_000, retry: 1, refetchOnWindowFocus: true },
  },
});

export const keys = {
  personas: ['personas'] as const,
  glance: (p: string | null) => ['glance', p] as const,
  metrics: (p: string | null) => ['metrics', p] as const,
  health: (p: string | null) => ['health', p] as const,
  avatar: (p: string | null) => ['avatar', p] as const,
};

/** WebSocket data_changed 资源名 → 需要失效的查询前缀 */
export const RESOURCE_QUERIES: Record<string, readonly string[][]> = {
  personas: [['personas']],
  monitoring: [['personas'], ['metrics'], ['health']],
  dashboard: [['metrics'], ['health']],
  tokens: [['metrics'], ['tokens']],
  conversations: [['glance'], ['diary']],
  memory: [['glance'], ['memories'], ['dedupe']],
  autonomy: [['glance'], ['autonomy']],
  life: [['glance'], ['life'], ['life-work']],
  'work-mode': [['work-mode']],
  'tool-history': [['tool-history']],
  cognition: [['glance'], ['metrics'], ['cognition'], ['cognition-analysis']],
};

export function usePersonas() {
  return useQuery({
    queryKey: keys.personas,
    queryFn: () => api.get<PersonasResponse>('/api/personas', { global: true }),
    refetchInterval: 20_000,
  });
}

export function useCurrentPersona() {
  const current = usePersonaStore((s) => s.current);
  const { data } = usePersonas();
  return data?.personas.find((p) => p.name === current) ?? null;
}

export function useGlance() {
  const persona = usePersonaStore((s) => s.current);
  return useQuery({
    queryKey: keys.glance(persona),
    queryFn: () => api.get<Glance>('/api/persona/glance'),
    enabled: !!persona,
    refetchInterval: 60_000,
  });
}

export function useMetrics() {
  const persona = usePersonaStore((s) => s.current);
  return useQuery({
    queryKey: keys.metrics(persona),
    queryFn: () => api.get<Metrics>('/api/monitoring/metrics'),
    enabled: !!persona,
  });
}

export function useHealth() {
  const persona = usePersonaStore((s) => s.current);
  return useQuery({
    queryKey: keys.health(persona),
    queryFn: () => api.get<Health>('/api/monitoring/health'),
    enabled: !!persona,
    refetchInterval: 30_000,
  });
}

export interface Avatar {
  url: string;
  /** 是不是她自己的立绘（否则是内置的月白） */
  custom: boolean;
}

/** 立绘：没有自定义图时服务端给内置的月白。图片要带认证头，所以走 fetch → blob URL。 */
export function useAvatar(name: string | null) {
  return useQuery({
    queryKey: keys.avatar(name),
    enabled: !!name,
    staleTime: Infinity,
    queryFn: async (): Promise<Avatar> => {
      try {
        const res = await api.get<Response>(`/api/personas/${encodeURIComponent(name!)}/avatar`, {
          global: true,
          raw: true,
        });
        return {
          url: URL.createObjectURL(await res.blob()),
          custom: res.headers.get('X-Avatar-Custom') === '1',
        };
      } catch {
        return { url: `${import.meta.env.BASE_URL}yuebai.webp`, custom: false };
      }
    },
  });
}

export function useAvatarChange(name: string | null) {
  return useMutation({
    mutationFn: (file: File | null) =>
      file
        ? api.post(`/api/personas/${encodeURIComponent(name!)}/avatar`, file, {
            global: true,
            headers: { 'Content-Type': file.type || 'application/octet-stream' },
          })
        : api.del(`/api/personas/${encodeURIComponent(name!)}/avatar`, { global: true }),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: keys.avatar(name) }),
  });
}

/** 某位伙伴的一瞥（星图里看别人家时用，不切换当前人格） */
export function useGlanceOf(name: string | null) {
  return useQuery({
    queryKey: keys.glance(name),
    queryFn: () => api.get<Glance>('/api/persona/glance', { persona: name! }),
    enabled: !!name,
  });
}

export function useWakeSleep() {
  return useMutation({
    mutationFn: ({ name, wake }: { name: string; wake: boolean }) =>
      api.post(`/api/personas/${encodeURIComponent(name)}/${wake ? 'start' : 'stop'}`, {}, { global: true }),
    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: keys.personas });
      queryClient.invalidateQueries({ queryKey: ['health'] });
      queryClient.invalidateQueries({ queryKey: ['metrics'] });
    },
  });
}

// ── 镜子 / 信箱 ─────────────────────────────────────────

function usePersonaQuery<T>(name: string, path: string, refetchInterval?: number) {
  const persona = usePersonaStore((s) => s.current);
  return useQuery({
    queryKey: [name, persona],
    queryFn: () => api.get<T>(path),
    enabled: !!persona,
    refetchInterval,
    // 草稿只在第一次拿到数据时初始化，后台刷新不打扰正在写的字；
    // 但每次进房间都重新取一次，草稿等这次取回来再起（见 useDraft 的 ready）
    refetchOnWindowFocus: false,
    refetchOnMount: 'always',
  });
}

export const useProfile = () => usePersonaQuery<Profile>('profile', '/api/persona/persona');
export const useExperience = () => usePersonaQuery<Experience>('experience', '/api/persona/experience');
export const useAdapters = () =>
  usePersonaQuery<{ adapters: NapCatAdapter[] }>('adapters', '/api/persona/adapters');
export const useAccounts = () => usePersonaQuery<AccountsResponse>('accounts', '/api/persona/accounts');

export function useDispatch() {
  return useQuery({
    queryKey: ['dispatch'],
    queryFn: () => api.get<DispatchOverview>('/api/dispatcher/overview', { global: true }),
    refetchInterval: 5000,
  });
}

// 保存都显式带上人格：草稿属于哪位伙伴，就只写回她家，不受这期间切换伙伴的影响
export const saveProfile = (persona: string, profile: Profile) =>
  api.post('/api/persona/persona/save', { persona: profile }, { persona });
/** 只提交给出的字段：镜子和信箱各改各的，不会互相覆盖 */
export const saveExperience = (persona: string, patch: Partial<Experience>) =>
  api.post('/api/persona/experience', { experience: patch }, { persona });
export const saveAdapters = (persona: string, adapters: NapCatAdapter[]) =>
  api.post('/api/persona/adapters', { adapters }, { persona });
export const saveAccounts = (persona: string, accounts: Account[]) =>
  api.post<{ accounts: Account[] }>('/api/persona/accounts', { accounts }, { persona });
