export interface PersonaSummary {
  name: string;
  persona_name: string;
  running: boolean;
  pid: number | null;
  heartbeat_at: string | null;
  started_at: string | null;
  active: boolean;
  has_config: boolean;
}

export interface PersonasResponse {
  personas: PersonaSummary[];
  active: string;
  active_personas: string[];
}

export interface Mood {
  valence: number;
  arousal: number;
  baseline_valence: number;
  baseline_arousal: number;
  emotion: string;
  emotion_cn: string;
  updated_at: string;
}

export interface RecentLine {
  group_id: string;
  speaker_name: string;
  role: 'human' | 'assistant' | 'system' | string;
  content: string;
  timestamp: string;
}

export interface Glance {
  window_hours: number;
  mood: Mood | null;
  conversation: {
    heard: number;
    replied: number;
    groups: { group_id: string; messages: number }[];
    last_activity_at: string;
    recent: RecentLine[];
  };
  memory: { total: number; new: number };
  episodes: { kind: string; outcome: string; seed: string; at: string }[];
}

export interface Metrics {
  pid: number | null;
  uptime_seconds: number;
  token_usage: {
    total_input: number;
    total_output: number;
    call_count: number;
  };
  memory: { memory_unit_count: number };
  cognition: { event_count: number };
}

export interface Health {
  running: boolean;
  healthy: boolean;
  checks: {
    process?: { status?: string; pid?: number | null };
    heartbeat?: { status?: string; heartbeat_at?: string | null; age_seconds?: number | null };
    adapter?: { status?: string; online?: boolean | null; degraded?: boolean };
    config?: { status?: string; files?: string[] };
    memory?: { status?: string };
  };
}

export interface EngineEvent {
  type: string;
  timestamp?: string;
  persona?: string;
  resources?: string[];
  data?: Record<string, any>;
}

export interface Profile {
  name: string;
  aliases: string[];
  full_system_prompt: string;
}

export interface CurvePoint {
  time: string;
  coefficient: number;
}

export interface Experience {
  engagement_sensitivity: number;
  expressiveness: number;
  group_reply_strategies: Record<string, 'smart' | 'keyword'>;
  min_reply_interval_seconds: number;
  main_model_reply_cooldown_seconds: number;
  reply_time_curve_points: CurvePoint[];
  max_sentence_chars: number;
  memory_unit_top_k: number;
  memory_unit_token_budget: number;
  persona_notebook_token_budget: number;
  enable_tools: boolean;
  max_tool_rounds: number;
  auto_install_tool_deps: boolean;
  other_ai_names: string[];
  message_prefixes: string[];
}

export interface NapCatAdapter {
  type: 'napcat';
  enabled: boolean;
  ws_url: string;
  token: string;
  qq_number: string;
  allowed_group_ids: string[];
  allowed_private_user_ids: string[];
  peer_ai_ids: string[];
  group_dispatch_enabled: boolean;
  dispatch_db_path: string;
  dispatch_priority: number;
  dispatch_min_reply_interval_seconds: number;
  dispatch_lease_seconds: number;
  dispatch_peer_cooldown_seconds: number;
  dispatch_max_peer_turns: number;
  dispatch_score_collection_seconds: number;
  dispatch_activity_window_seconds: number;
  dispatch_activity_penalty_per_reply: number;
  dispatch_max_activity_penalty: number;
  enable_group_chat: boolean;
  enable_private_chat: boolean;
  root: string;
}

export interface Account {
  kind: string;
  name: string;
  username: string;
  secret: string;
  env: string;
}

export interface AccountsResponse {
  accounts: Account[];
  kinds: string[];
  default_env: Record<string, string>;
}

export interface DispatchWorker {
  worker_id: string;
  account_id?: string;
  online: boolean;
  priority?: number;
  last_seen?: number;
  last_reply_at?: number;
  reply_count?: number;
  recent_reply_count?: number;
}

export interface DispatchGroup {
  group_id: string;
  active: boolean;
  active_worker_id?: string;
  active_remaining_seconds?: number;
  last_human_at?: number;
  last_reply_at?: number;
  peer_turns?: number;
}

export interface DispatchEvent {
  event_id: string;
  group_id: string;
  worker_id?: string;
  status: string;
  reason?: string;
  response_strategy?: string;
  base_score?: number;
  final_score?: number;
  updated_at: number;
}

export interface DispatchOverview {
  available: boolean;
  workers?: DispatchWorker[];
  groups?: DispatchGroup[];
  events?: DispatchEvent[];
  summary?: {
    workers_total: number;
    workers_online: number;
    groups_total: number;
    active_turns: number;
    decisions_24h: number;
    sent_24h: number;
  };
  policy?: { dispatch_enabled: boolean; adapters_enabled: number };
}
