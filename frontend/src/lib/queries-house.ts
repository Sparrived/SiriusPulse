import { useQuery } from '@tanstack/react-query';
import { api } from './api';
import { usePersonaStore } from '@/state/persona';
import type { ParamDef, Settings } from '@/ui/params/ParamForm';

// ── 背包 ──────────────────────────────────────────────

export interface ToolInfo {
  name: string;
  description: string;
  version: string;
  enabled: boolean;
  developer_only: boolean;
  silent: boolean;
  tags: string[];
  parameters: ParamDef[];
  config_parameters: ParamDef[];
  config: Settings;
}

export interface PluginCommand {
  name: string;
  patterns: string[];
  pattern_type: string;
  description: string;
  examples?: string[];
  hidden_from_intent: boolean;
}

export interface PluginInfo {
  name: string;
  display_name: string;
  description: string;
  version: string;
  author: string;
  enabled: boolean;
  commands: PluginCommand[];
  events: { type: string; cron?: string; description?: string }[];
  parameters: ParamDef[];
  ui_schema: unknown;
  nl_examples: string[];
  settings: Settings;
  has_source?: boolean;
}

export interface PluginPermissions {
  group_blacklist: string[];
  developer_only: boolean;
  hidden_from_intent: boolean;
  rate_limit_calls_per_minute: number | null;
  developer_only_locked: boolean;
  hidden_from_intent_locked: boolean;
}

export interface PluginDetail extends PluginInfo {
  source_file?: string;
  source_content?: string;
  permissions: PluginPermissions;
}

export interface McpServer {
  enabled: boolean;
  transport: 'stdio' | 'streamable_http' | 'sse' | string;
  command?: string;
  args?: string[];
  cwd?: string;
  url?: string;
  env?: Record<string, string>;
  headers?: Record<string, string>;
}

export interface ToolCallRecord {
  tool_name: string;
  timestamp: number;
  success: boolean;
  duration_ms: number;
  caller_user_id?: string;
  params?: Record<string, unknown>;
  result_summary?: string;
  error?: string;
}

export interface ToolStats {
  calls: number;
  successes: number;
  failures: number;
  avg_ms: number;
  errors: string[];
}

export function useTools() {
  const persona = usePersonaStore((s) => s.current);
  return useQuery({
    queryKey: ['tools', persona],
    queryFn: () => api.get<{ tools: ToolInfo[] }>('/api/persona/tools'),
    enabled: !!persona,
  });
}

export function usePlugins() {
  return useQuery({
    queryKey: ['plugins'],
    queryFn: () => api.get<{ plugins: PluginInfo[] }>('/api/plugins', { global: true }),
  });
}

export function usePluginDetail(name: string | null) {
  return useQuery({
    queryKey: ['plugin', name],
    queryFn: () => api.get<PluginDetail>(`/api/plugins/${encodeURIComponent(name!)}`, { global: true }),
    enabled: !!name,
    refetchOnWindowFocus: false,
  });
}

export function useMcp() {
  const persona = usePersonaStore((s) => s.current);
  return useQuery({
    queryKey: ['mcp', persona],
    queryFn: () => api.get<{ servers: Record<string, McpServer>; secret_mask: string }>('/api/persona/mcp'),
    enabled: !!persona,
    refetchOnWindowFocus: false,
  });
}

export function useModels() {
  return useQuery({
    queryKey: ['models'],
    queryFn: () =>
      api.get<{ model_choices: { label: string; value: string }[] }>('/api/models', { global: true }),
    staleTime: 5 * 60_000,
  });
}

export function useToolHistory(filters: { tool: string; success: string }, page: number, size = 30) {
  const persona = usePersonaStore((s) => s.current);
  const params = new URLSearchParams({ limit: String(size), offset: String(page * size) });
  if (filters.tool) params.set('tool_name', filters.tool);
  if (filters.success) params.set('success', filters.success);
  return useQuery({
    queryKey: ['tool-history', persona, filters.tool, filters.success, page],
    queryFn: () =>
      api.get<{ history: ToolCallRecord[]; total: number; stats: Record<string, ToolStats> }>(
        `/api/persona/tool-history?${params}`,
      ),
    enabled: !!persona,
    placeholderData: (prev) => prev,
  });
}

const enc = encodeURIComponent;
export const toggleTool = (persona: string, name: string, enabled: boolean) =>
  api.post(`/api/persona/tools/${enc(name)}/toggle`, { enabled }, { persona });
export const saveToolConfig = (persona: string, name: string, config: Settings, enabled: boolean) =>
  api.post(`/api/persona/tools/${enc(name)}/config`, { config, enabled }, { persona });
export const saveMcp = (persona: string, servers: Record<string, McpServer>) =>
  api.post<{ servers: Record<string, McpServer> }>('/api/persona/mcp', { servers }, { persona });
export const togglePlugin = (name: string, enabled: boolean) =>
  api.post(`/api/plugins/${enc(name)}/toggle`, { enabled }, { global: true });
export const savePluginSettings = (name: string, settings: Settings) =>
  api.post(`/api/plugins/${enc(name)}/settings`, { settings }, { global: true });
export const savePluginPermissions = (name: string, permissions: Partial<PluginPermissions>) =>
  api.put(`/api/plugins/${enc(name)}/config`, permissions, { global: true });

// ── 小账本 ────────────────────────────────────────────

export interface UsageRow {
  name: string;
  calls: number;
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  cached_prompt_tokens?: number;
}

export interface TokenReport {
  summary: {
    total_calls: number;
    total_prompt_tokens: number;
    total_completion_tokens: number;
    total_tokens: number;
  };
  cache_stats: { cache_hit_rate_pct: number; cache_info_coverage_pct: number; cached_prompt_tokens: number };
  response_avg: { avg_total_tokens?: number; avg_prompt_tokens?: number; avg_completion_tokens?: number };
  hourly: {
    hour_ts: number;
    calls: number;
    total_tokens: number;
    prompt_tokens: number;
    completion_tokens: number;
  }[];
  hourly_distribution: { hour: number; calls: number }[];
  by_model: UsageRow[];
  by_group: UsageRow[];
  by_task: UsageRow[];
  section_breakdown: Record<string, number>;
  recent_with_breakdown: {
    id: number;
    timestamp: number;
    task_name: string;
    group_id: string;
    prompt_tokens: number;
    completion_tokens: number;
    total_tokens: number;
    duration_ms: number;
    error_type: string;
    cached_prompt_tokens: number;
  }[];
  retry_stats: { retry_rate_pct: number; total_retries: number };
  failure_stats: {
    failure_rate_pct: number;
    failure_calls: number | null;
    by_type: { name: string; calls: number }[];
  };
  duration_stats: { overall: { avg_ms: number | null } };
  empty_reply_stats: { empty_rate_pct: number };
  period_comparison: {
    current: { total_tokens: number; calls: number };
    previous: { total_tokens: number; calls: number };
    change_total_tokens: number;
    change_calls: number;
  };
}

export function useTokens(range: { start?: number; end?: number }) {
  const persona = usePersonaStore((s) => s.current);
  const params = new URLSearchParams();
  if (range.start) params.set('start', String(Math.floor(range.start)));
  if (range.end) params.set('end', String(Math.floor(range.end)));
  return useQuery({
    queryKey: ['tokens', persona, range.start ?? 0],
    queryFn: () => api.get<TokenReport>(`/api/persona/tokens?${params}`),
    enabled: !!persona,
    placeholderData: (prev) => prev,
  });
}

// ── 阁楼 ──────────────────────────────────────────────

export interface GlobalConfig {
  webui_host?: string;
  webui_port?: number;
  log_level?: string;
  amkr_base_url?: string;
  amkr_local_api_key?: string;
  amkr_workspace?: string;
  amkr_public_url?: string;
  amkr_ui_enabled?: boolean;
  [other: string]: unknown;
}

export interface AmkrWorkspace {
  persona: string;
  workspace: string;
  registered: string[];
  missing: string[];
  unbound: string[];
  inference_ready: boolean;
  panel_ready: boolean;
  error?: string;
}

export interface AmkrStatus {
  configured: boolean;
  base_url: string;
  browser_base_url: string;
  workspace_base: string;
  ui_url: string;
  reachable: boolean;
  error: string;
  version: string;
  ops_enabled: boolean;
  webui_mounted: boolean;
  known_tasks: string[];
  workspaces: AmkrWorkspace[];
}

export interface EmbeddingStatus {
  running: boolean;
  ready: boolean;
  error: string;
  model: string;
  base_url: string;
}

export interface LogChunk {
  lines: string[];
  offset: number;
  size: number;
  exists: boolean;
  name: string;
  path: string;
}

export function useGlobalConfig() {
  return useQuery({
    queryKey: ['global-config'],
    queryFn: () => api.get<GlobalConfig>('/api/global-config', { global: true }),
    refetchOnWindowFocus: false,
    refetchOnMount: 'always',
  });
}

export function useAmkr() {
  return useQuery({
    queryKey: ['amkr'],
    queryFn: () => api.get<AmkrStatus>('/api/amkr/status', { global: true }),
    refetchInterval: 30_000,
  });
}

export function useEmbedding() {
  return useQuery({
    queryKey: ['embedding'],
    queryFn: () => api.get<EmbeddingStatus>('/api/embedding/status', { global: true }),
  });
}

export const saveGlobalConfig = (patch: GlobalConfig) =>
  api.post('/api/global-config', patch, { global: true });
export const registerAmkr = (persona?: string) =>
  api.post<{ results: Record<string, unknown> }>('/api/amkr/register', persona ? { persona } : {}, {
    global: true,
  });
export const rotateInferenceKey = (persona: string) =>
  api.post<{ persona: string; inference_key: string }>(
    '/api/amkr/rotate-inference-key',
    { persona },
    { global: true },
  );
export const amkrPanel = (persona: string) =>
  api.get<{ persona: string; url: string }>(`/api/amkr/panel?persona=${enc(persona)}`, { global: true });
export const rebuildEmbedding = () =>
  api.post<{ success: boolean; units: number; failed: number; error?: string }>(
    '/api/embedding/rebuild',
    {},
    { global: true },
  );
export const shutdownAll = () => api.post('/api/shutdown', {}, { global: true });
export const fetchLogs = (source: 'webui' | 'persona', offset: number, lines: number, persona?: string) =>
  source === 'webui'
    ? api.get<LogChunk>(`/api/system/logs?lines=${lines}&offset=${offset}`, { global: true })
    : api.get<LogChunk>(`/api/persona/logs?source=persona&lines=${lines}&offset=${offset}`, { persona });
