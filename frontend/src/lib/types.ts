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
