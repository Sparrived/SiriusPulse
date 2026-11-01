// 日记 / 回忆 / 心事 三个房间用到的接口形状

// ── 日记 ──────────────────────────────────────────────
export interface ToolCall {
  id?: string;
  type?: string;
  function?: { name?: string; arguments?: string };
  name?: string;
  arguments?: string;
}

export interface ChainMessage {
  role: string;
  content?: string | null;
  reasoning_content?: string | null;
  tool_calls?: ToolCall[] | null;
}

export interface ChatEntry {
  entry_id?: string;
  timestamp: string;
  role: 'human' | 'assistant' | 'system' | 'tool' | string;
  user_id?: string;
  speaker_name?: string;
  group_id: string;
  content: string;
  reasoning_content?: string | null;
  tool_calls?: ToolCall[] | null;
  tool_call_id?: string;
  intent_scores?: Record<string, number> | null;
  memory_refs?: string[] | null;
  conversation_chain?: ChainMessage[];
  system_prompt?: string;
  tags?: string[];
  memory_compressed?: boolean;
}

export interface ConversationsResponse {
  messages: ChatEntry[];
  groups: string[];
  total: number;
}

export interface DayTimeline {
  groups: string[];
  basic_timeline: {
    days: string[];
    buckets: Record<string, Record<string, { human?: number; assistant?: number; system?: number }>>;
  };
}

export interface Intention {
  intention_id: string;
  created_at: string;
  what: string;
  why: string;
  resolution: string;
  status: string;
  kind: string;
  audience: string;
  audience_label: string;
  urgency: number;
  attempts: number;
  resolved_at: string;
  outcome: string;
}

export interface Episode {
  episode_id: string;
  started_at: string;
  ended_at: string;
  kind: string;
  seed: string;
  outcome: string;
  resolution: string;
  audience: string;
  status: string;
  project_id?: string;
}

export interface AutonomyResponse {
  summary: {
    intentions_total: number;
    intentions_open: number;
    episodes_total: number;
    last_episode_at: string;
  };
  intentions: Intention[];
  episodes: Episode[];
}

export interface LifeNow {
  phase: 'asleep' | 'morning' | 'day' | 'evening' | string;
  asleep: boolean;
  wake_time: string;
  sleep_time: string;
  energy: number;
  energy_word: string;
  activity: string;
  next_block: LifeBlock | null;
  life_date: string;
}

export interface LifeBlock {
  start: string;
  end: string;
  activity: string;
  project_id: string;
  kind: 'work' | 'routine';
  status: 'planned' | 'doing' | 'done' | 'skipped' | 'missed' | string;
  outcome: string;
  reason: string;
}

export interface LifeAgenda {
  date: string;
  note: string;
  made_at: string;
  blocks: LifeBlock[];
}

export interface LifeProject {
  project_id: string;
  title: string;
  kind: string;
  kind_label: string;
  why: string;
  next_step: string;
  steps: { at: string; did: string }[];
  status: 'active' | 'paused' | 'done' | 'dropped' | string;
  started_at: string;
  touched_at: string;
  finished_at: string;
  outcome: string;
}

export interface LifeWorkRow {
  project_id: string;
  title: string;
  kind: string;
  status: string;
  chars: number;
  updated_at: string;
}

export interface LifeResponse {
  now: LifeNow;
  self_note: { recent: string; interests: string[]; tomorrow: string; updated_at: string };
  date: string;
  agenda: LifeAgenda | null;
  projects: LifeProject[];
  works: LifeWorkRow[];
}

export interface LifeWorkResponse {
  project: LifeProject;
  markdown: string;
}

export interface WorkStep {
  round?: number;
  kind?: string;
  text: string;
  tools?: { name: string; arguments: string }[];
  results?: { tool: string; output: string }[];
}

export interface WorkSession {
  session_id?: string;
  group_id?: string;
  goal: string;
  status: string;
  result: string;
  source: string;
  task_name: string;
  started_at: string;
  ended_at: string;
  steps: WorkStep[];
}

export interface WorkModeResponse {
  summary: { sessions_total: number; sessions_completed: number; sessions_running: number };
  sessions: WorkSession[];
  settings: { task_name: string };
  task_options: { label: string; value: string }[];
}

// ── 回忆 ──────────────────────────────────────────────
export interface MemoryUnit {
  unit_id: string;
  group_id: string;
  created_at: string;
  unit_type: string;
  scope: string;
  scope_id: string;
  summary: string;
  participants: string[];
  topics: string[];
  keywords: string[];
  salience: number;
  confidence: number;
  lifespan: string;
  should_prompt: boolean;
  source_ids: string[];
  [extra: string]: unknown;
}

export interface MemoryUnitsResponse {
  units: MemoryUnit[];
  groups: string[];
  total: number;
}

export interface DedupeChange {
  group_id: string;
  decision: 'DUPLICATE' | 'MERGE' | 'CONFLICT';
  incoming_summary: string;
  target_summary: string;
  result_summary: string;
  reason: string;
}

export interface DedupeStatus {
  job_id?: string;
  status: string;
  progress?: number;
  error?: string;
  worker_running: boolean;
  report?: {
    summary: Record<string, number>;
    total: number;
    changes_total: number;
    changes: DedupeChange[];
  } | null;
}

// ── 心事 ──────────────────────────────────────────────
export interface CognitionEvent {
  id: number;
  timestamp: number;
  group_id: string;
  user_id: string;
  valence: number;
  arousal: number;
  basic_emotion: string;
  intensity: number;
  social_intent: string;
}

export interface CognitionResponse {
  events: CognitionEvent[];
  emotion_distribution: Record<string, number>;
}

export interface Histogram {
  labels: string[];
  counts: number[];
  total: number;
}

export interface CognitionAnalysis {
  has_data: boolean;
  intent_distribution?: Record<string, number>;
  group_summary?: {
    group_id: string;
    event_count: number;
    unique_users: number;
    avg_valence: number;
    avg_arousal: number;
    last_event: number;
  }[];
  hourly_distribution?: Record<string, number>;
  score_histograms?: Partial<Record<'directed' | 'sarcasm' | 'entitlement', Histogram>>;
  strategy_distribution?: Record<string, number>;
  decision_summary?: {
    total: number;
    avg_score: number | null;
    avg_threshold: number | null;
    reason_distribution: Record<string, number>;
    heat_distribution: Record<string, number>;
  };
  decision_timeline?: {
    timestamp: number;
    strategy: string;
    score: number;
    threshold: number;
    reason: string;
  }[];
}
