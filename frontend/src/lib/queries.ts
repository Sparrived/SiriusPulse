import { QueryClient, useMutation, useQuery } from '@tanstack/react-query';
import { api } from './api';
import type { Glance, Health, Metrics, PersonasResponse } from './types';
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
  tokens: [['metrics']],
  conversations: [['glance']],
  memory: [['glance']],
  autonomy: [['glance']],
  cognition: [['glance'], ['metrics']],
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

/** 立绘：没有自定义图时用内置的月白。图片要带认证头，所以走 fetch → blob URL。 */
export function useAvatar(name: string | null) {
  return useQuery({
    queryKey: keys.avatar(name),
    enabled: !!name,
    staleTime: Infinity,
    queryFn: async () => {
      try {
        const res = await api.get<Response>(`/api/personas/${encodeURIComponent(name!)}/avatar`, {
          global: true,
          raw: true,
        });
        return URL.createObjectURL(await res.blob());
      } catch {
        return `${import.meta.env.BASE_URL}yuebai.webp`;
      }
    },
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
