import { useQuery } from '@tanstack/react-query';
import { api } from './api';
import type {
  AutonomyResponse,
  ChatEntry,
  CognitionAnalysis,
  CognitionResponse,
  ConversationsResponse,
  DayTimeline,
  DedupeStatus,
  MemoryUnit,
  MemoryUnitsResponse,
  WorkModeResponse,
} from './types-journal';
import { usePersonaStore } from '@/state/persona';

/** 本地日期 → 后端按字符串比较的 UTC ISO 时间（存档里的时间戳都是 +00:00 结尾） */
export function dayRange(day: string): { start: string; end: string } {
  const [y, m, d] = day.split('-').map(Number);
  const iso = (date: Date) => date.toISOString().replace('Z', '+00:00');
  return { start: iso(new Date(y, m - 1, d)), end: iso(new Date(y, m - 1, d + 1)) };
}

export function localDay(date = new Date()): string {
  const p = (n: number) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${p(date.getMonth() + 1)}-${p(date.getDate())}`;
}

export const DIARY_PAGE = 200;

export function useDiaryDay(day: string, filters: { group: string; search: string }, page: number) {
  const persona = usePersonaStore((s) => s.current);
  const { start, end } = dayRange(day);
  const params = new URLSearchParams({
    limit: String(DIARY_PAGE),
    offset: String(page * DIARY_PAGE),
    start,
    end,
  });
  if (filters.group) params.set('group_id', filters.group);
  if (filters.search) params.set('search', filters.search);
  return useQuery({
    queryKey: ['diary', persona, day, filters.group, filters.search, page],
    queryFn: () => api.get<ConversationsResponse>(`/api/persona/conversations?${params}`),
    enabled: !!persona,
    placeholderData: (prev) => prev,
  });
}

/** 哪些天有过聊天：存档按 UTC 日期分桶，这里只用来在日历上点亮「那天有动静」 */
export function useDayTimeline() {
  const persona = usePersonaStore((s) => s.current);
  return useQuery({
    queryKey: ['diary', persona, 'days'],
    queryFn: () => api.get<DayTimeline>('/api/persona/memory-viz?basic_limit=1'),
    enabled: !!persona,
    staleTime: 60_000,
  });
}

export function deleteChatEntry(persona: string, entry: ChatEntry) {
  const params = new URLSearchParams({ group_id: entry.group_id });
  if (entry.entry_id) params.set('entry_id', entry.entry_id);
  else {
    params.set('timestamp', entry.timestamp);
    params.set('role', entry.role);
    params.set('user_id', entry.user_id ?? '');
    params.set('content', (entry.content ?? '').slice(0, 120));
  }
  return api.del(`/api/persona/conversations?${params}`, { persona });
}

function useScoped<T>(name: string, path: string, refetchInterval?: number) {
  const persona = usePersonaStore((s) => s.current);
  return useQuery({
    queryKey: [name, persona],
    queryFn: () => api.get<T>(path),
    enabled: !!persona,
    refetchInterval,
  });
}

export const useAutonomy = () => useScoped<AutonomyResponse>('autonomy', '/api/persona/autonomy', 30_000);
export const useWorkMode = () => useScoped<WorkModeResponse>('work-mode', '/api/persona/work-mode');
export const useMemories = () =>
  useScoped<MemoryUnitsResponse>('memories', '/api/persona/memory-units?limit=2000');
export const useCognition = () =>
  useScoped<CognitionResponse>('cognition', '/api/persona/cognition?limit=200');
export const useCognitionAnalysis = () =>
  useScoped<CognitionAnalysis>('cognition-analysis', '/api/persona/cognition/analysis');

export function useDedupe(poll: boolean) {
  const persona = usePersonaStore((s) => s.current);
  return useQuery({
    queryKey: ['dedupe', persona],
    queryFn: () => api.get<DedupeStatus>('/api/persona/memory-units/dedupe/status'),
    enabled: !!persona,
    refetchInterval: poll ? 1000 : false,
  });
}

export const saveWorkTask = (persona: string, task_name: string) =>
  api.post('/api/persona/work-mode', { task_name }, { persona });

export const saveMemory = (persona: string, unit: MemoryUnit, isNew: boolean) =>
  isNew
    ? api.post<{ unit: MemoryUnit }>('/api/persona/memory-units', unit, { persona })
    : api.put<{ unit: MemoryUnit }>(`/api/persona/memory-units/${encodeURIComponent(unit.unit_id)}`, unit, {
        persona,
      });

export const forgetMemory = (persona: string, unitId: string) =>
  api.del(`/api/persona/memory-units/${encodeURIComponent(unitId)}`, { persona });

export const startDedupe = (persona: string) =>
  api.post<DedupeStatus>('/api/persona/memory-units/dedupe/scan', {}, { persona });
export const applyDedupe = (persona: string, job_id: string) =>
  api.post<DedupeStatus>('/api/persona/memory-units/dedupe/apply', { job_id }, { persona });
