"""Configuration data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sirius_pulse.mixins import JsonSerializable


@dataclass(slots=True)
class ExpressivenessConfig:
    """Single-knob expressiveness / reply eagerness regulator.

    只有一个主旋钮 ``expressiveness``（0.0~1.0）：
        - 0.0 = 极度克制（几乎不主动说话，被@才回）
        - 0.5 = 默认平衡
        - 1.0 = 极度活泼（积极抢话、主动参与）

    所有内部阈值都从这个值自动推导，用户无需理解每个参数含义。
    高级用户可通过 ``overrides`` 字典单独覆盖任意阈值。
    """

    expressiveness: float = 0.5  # 0.0 ~ 1.0
    overrides: dict[str, float] = field(default_factory=dict)

    # === derived thresholds (do not set directly, use overrides instead) ===

    @property
    def directed_threshold(self) -> float:
        """0.0→0.8, 0.5→0.6, 1.0→0.4"""
        return self.overrides.get("directed_threshold", 0.8 - self.expressiveness * 0.4)

    @property
    def weak_directed_threshold(self) -> float:
        """0.0→0.6, 0.5→0.4, 1.0→0.2"""
        return self.overrides.get("weak_directed_threshold", 0.6 - self.expressiveness * 0.4)

    @property
    def gap_readiness_threshold(self) -> float:
        """0.0→0.45, 0.5→0.25, 1.0→0.05"""
        return self.overrides.get("gap_readiness_threshold", 0.45 - self.expressiveness * 0.4)

    @property
    def entitlement_threshold(self) -> float:
        """0.0→0.5, 0.5→0.3, 1.0→0.1"""
        return self.overrides.get("entitlement_threshold", 0.5 - self.expressiveness * 0.4)

    @property
    def redundancy_threshold(self) -> float:
        """0.0→0.75, 0.5→0.55, 1.0→0.35"""
        return self.overrides.get("redundancy_threshold", 0.75 - self.expressiveness * 0.4)

    @property
    def sarcasm_boost(self) -> float:
        """0.0→0.05, 0.5→0.15, 1.0→0.25"""
        return self.overrides.get("sarcasm_boost", 0.05 + self.expressiveness * 0.2)

    @property
    def cooldown_seconds(self) -> float:
        """0.0→90s, 0.5→30s, 1.0→5s"""
        return self.overrides.get("cooldown_seconds", 90 - self.expressiveness * 85)

    def to_dict(self) -> dict[str, Any]:
        return {
            "expressiveness": self.expressiveness,
            "overrides": dict(self.overrides),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ExpressivenessConfig":
        return cls(
            expressiveness=max(0.0, min(1.0, data.get("expressiveness", 0.5))),
            overrides=dict(data.get("overrides", {})),
        )


@dataclass(slots=True)
class Agent:
    """AI agent definition with model and parameters."""

    name: str
    persona: str
    model: str
    temperature: float = 0.7
    max_tokens: int = 512
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class AgentPreset:
    """Pre-configured agent with system prompt."""

    agent: Agent
    global_system_prompt: str


@dataclass(slots=True)
class SessionDefaults:
    """Workspace-level defaults used to build SessionConfig instances."""

    history_max_messages: int = 24
    history_max_chars: int = 6000
    max_recent_participant_messages: int = 5
    enable_auto_compression: bool = True


@dataclass(slots=True)
class ProviderPolicy:
    """Workspace-level provider bootstrap policy."""

    prefer_workspace_registry: bool = True


@dataclass(slots=True)
class WorkspaceBootstrap:
    """Host-provided defaults injected at workspace open time.

    The host (plugin / CLI) fills in the fields it cares about; the runtime
    decides how to merge them into the workspace and whether to persist.
    """

    active_agent_key: str | None = None
    session_defaults: SessionDefaults | None = None
    orchestration_defaults: dict[str, object] | None = None
    provider_entries: list[dict[str, object]] | None = None
    provider_policy: ProviderPolicy | None = None


@dataclass(slots=True)
class MemoryPolicy:
    """Centralized memory system configuration.

    Controls memory fact limits, confidence thresholds, decay behaviour,
    observed-set caps and prompt-injection budget.
    """

    max_facts_per_user: int = 50
    transient_confidence_threshold: float = 0.85
    event_dedup_window_minutes: int = 5
    max_observed_set_size: int = 100
    max_summary_facts_per_type: int = 5
    max_summary_total_chars: int = 2000
    decay_schedule: dict[int, float] = field(
        default_factory=lambda: {
            7: 0.95,
            30: 0.80,
            60: 0.55,
            90: 0.30,
            180: 0.05,
        }
    )


@dataclass(slots=True)
class OrchestrationPolicy:
    """Multi-model orchestration strategy (required).

    Supports two configuration approaches:

    Approach 1 - Unified Model: all tasks use the same model
        - Set unified_model: model name
        - Simplifies configuration, suitable for small task volumes

    Approach 2 - Per-Task Configuration: specify model for each task
        - Set task_models: {"memory_extract": "model-a", "event_extract": "model-b", ...}
        - Supports fine-grained task-level control

    Task Enablement:
        - All tasks (memory_extract, event_extract, intent_analysis) enabled by default
        - Use task_enabled dict to enable/disable specific tasks
        - Example: task_enabled={"memory_extract": False} disables memory extraction tasks
    """

    # Configuration approach selection (choose one, cannot both be empty)
    unified_model: str = ""  # Approach 1: all tasks use this model (higher priority)
    task_models: dict[str, str] = field(default_factory=dict)  # Approach 2: per-task configuration

    # Task enablement control (bool fields, all enabled by default)
    task_enabled: dict[str, bool] = field(
        default_factory=lambda: {
            "memory_extract": True,
            "cognition_analyze": True,
        }
    )

    # Per-task parameter tuning
    task_temperatures: dict[str, float] = field(default_factory=dict)
    task_max_tokens: dict[str, int] = field(default_factory=dict)
    task_retries: dict[str, int] = field(default_factory=dict)

    # Multimodal processing configuration
    max_multimodal_inputs_per_turn: int = 4
    max_multimodal_value_length: int = 4096

    # Prompt-driven content splitting (built-in marker; AI autonomously decides granularity)
    enable_prompt_driven_splitting: bool = True
    memory_idle_consolidation_seconds: int = 3600

    # Memory Extract frequency control (避免调用过于频繁导致内容碎片化)
    memory_extract_batch_size: int = 1  # 每隔N条消息执行一次提取（1=每次，3=每3条）
    memory_extract_min_content_length: int = 0  # 最小内容长度阈值（字符数），0=无限制

    # Event Extract batch size (v2: 每N条消息批量提取一次用户观察)
    event_extract_batch_size: int = 5  # 每隔N条消息执行一次事件观察提取

    # Background memory consolidation (后台记忆归纳; live session 启动后静默常驻)
    consolidation_interval_seconds: int = 7200  # 归纳间隔（秒）
    consolidation_min_entries: int = 6  # 事件最少条数
    consolidation_min_notes: int = 4  # 摘要最少条数
    consolidation_min_facts: int = 15  # 事实最少条数

    # Engagement decision system (参与决策系统, v0.14.0 重写)
    session_reply_mode: str = "always"  # auto|always|never
    engagement_sensitivity: float = 0.5  # 0.0(极度克制) - 1.0(积极参与)
    heat_window_seconds: float = 60.0  # 热度分析滑动窗口（秒）

    # Pending-message batching: when the queued messages for a session exceed
    # this threshold, runtime enters backlog-silent mode and merges consecutive
    # turns from the same speaker into a single model call. Set to 0 to disable.
    pending_message_threshold: float = 4.0

    # Memory policy (centralized memory system configuration)
    memory: MemoryPolicy = field(default_factory=MemoryPolicy)

    # Self-memory system (AI diary + glossary)
    enable_self_memory: bool = True
    self_memory_extract_batch_size: int = (
        3  # AI replies between self-memory extractions (count-based trigger)
    )
    self_memory_min_chars: int = (
        0  # Also trigger when AI reply ≥ N chars (0 = disabled; OR logic with batch_size)
    )
    self_memory_max_diary_prompt_entries: int = 6  # Max diary entries injected into prompt
    self_memory_max_glossary_prompt_terms: int = 15  # Max glossary terms injected into prompt

    # Reply frequency limiter (global rate control independent of auto_reply)
    min_reply_interval_seconds: float = (
        0.0  # Minimum gap between two assistant replies; 0 = disabled
    )
    reply_frequency_window_seconds: float = 60.0  # Sliding window
    reply_frequency_max_replies: int = 8  # Max replies within the window
    reply_frequency_exempt_on_mention: bool = True  # Bypass limit when AI is directly mentioned

    # LLM concurrency limiter: cap parallel LLM generation calls per session context.
    # Algorithm-only steps (heat, keyword intent) are unaffected.
    # Set to 0 to disable (unlimited). Recommended: 1~3.
    max_concurrent_llm_calls: int = 1

    # Skill system: allow AI to invoke external code via function_call (tools)
    enable_skills: bool = True
    max_skill_rounds: int = 3  # max consecutive skill call rounds per turn
    skill_execution_timeout: float = 30.0  # max seconds per SKILL execution, 0 = no limit
    auto_install_skill_deps: bool = True  # auto-install missing SKILL dependencies via uv/pip

    # Hidden planning mode: normal chat can stay lightweight while plan runs privately.
    plan_mode_enabled: bool = False
    plan_mode_limit_normal_tools: bool = False
    plan_mode_allow_light_chat: bool = True
    plan_mode_presence_enabled: bool = False
    plan_mode_presence_min_interval_seconds: float = 45.0
    plan_mode_presence_enter_message: str = "我看到了，这个得稍微捋一下。"
    plan_mode_presence_update_message: str = "补充我看到了，我会按新的前提来。"

    def __post_init__(self) -> None:
        if "cognition_analyze" not in self.task_enabled:
            self.task_enabled = dict(self.task_enabled)
            self.task_enabled["cognition_analyze"] = True

    def is_task_enabled(self, task_name: str) -> bool:
        return bool(self.task_enabled.get(task_name, True))

    def resolve_model_for_task(self, task_name: str, *, default_model: str = "") -> str:
        explicit_model = str(self.task_models.get(task_name, "")).strip()
        if explicit_model:
            return explicit_model
        if self.unified_model:
            return self.unified_model.strip()
        return default_model.strip()

    def validate(self) -> None:
        """Validate configuration legitimacy."""
        if not self.unified_model and not self.task_models:
            raise ValueError(
                "Multi-model orchestration configuration error: must specify either "
                "unified_model (approach 1) or task_models (approach 2)."
            )

        if self.unified_model and self.task_models:
            raise ValueError(
                "Multi-model orchestration configuration error: unified_model (approach 1) "
                "and task_models (approach 2) cannot be specified simultaneously. "
                "Please choose one approach."
            )

        if self.memory_extract_batch_size <= 0:
            raise ValueError("memory_extract_batch_size 必须大于 0。")
        if self.memory_extract_min_content_length < 0:
            raise ValueError("memory_extract_min_content_length 不能小于 0。")

        if self.event_extract_batch_size <= 0:
            raise ValueError("event_extract_batch_size 必须大于 0。")
        if self.self_memory_extract_batch_size <= 0:
            raise ValueError("self_memory_extract_batch_size 必须大于 0。")
        if self.self_memory_min_chars < 0:
            raise ValueError("self_memory_min_chars 不能小于 0。")

        normalized_reply_mode = self.session_reply_mode.strip().lower()
        if normalized_reply_mode not in {
            "auto",
            "smart",
            "always",
            "never",
            "silent",
            "none",
            "no_reply",
        }:
            raise ValueError("session_reply_mode 仅支持 auto/smart/always/never/silent/none/no_reply。")

        if not 0.0 <= self.engagement_sensitivity <= 1.0:
            raise ValueError("engagement_sensitivity 必须在 [0,1] 范围内。")
        if self.heat_window_seconds <= 0:
            raise ValueError("heat_window_seconds 必须大于 0。")
        if self.pending_message_threshold < 0:
            raise ValueError("pending_message_threshold 不能小于 0。")
        if self.memory_idle_consolidation_seconds <= 0:
            raise ValueError("memory_idle_consolidation_seconds must be greater than 0.")
        if self.min_reply_interval_seconds < 0:
            raise ValueError("min_reply_interval_seconds 不能小于 0。")


@dataclass(slots=True)
class ConfigParameter:
    """通用配置参数定义 —— Plugin 和 Skill 系统共享。

    被 PluginParameterDef 和 SkillParameter 继承或引用，
    提供统一的前端表单渲染与参数校验契约。
    """

    name: str
    type: str = (
        "str"  # str | int | float | bool | list | model | password | object_array | checkbox_group
    )
    description: str = ""
    required: bool = False
    default: Any = None
    choices: list[str] | None = None  # 用于 checkbox_group 类型
    fields: list[dict[str, Any]] | None = None  # 用于 object_array 类型，定义子字段结构
    group: str = ""  # 参数分组


@dataclass(slots=True)
class TokenUsageRecord(JsonSerializable):
    """Record of token usage for a task execution."""

    actor_id: str
    task_name: str
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    input_chars: int = 0
    output_chars: int = 0
    estimation_method: str = "char_div4"
    retries_used: int = 0
    persona_name: str = ""
    group_id: str = ""
    provider_name: str = ""
    breakdown_json: str = ""
    duration_ms: float = 0.0
    error_type: str = ""
    error_message: str = ""
    conversation_depth: int = 0


@dataclass(slots=True)
class WorkspaceConfig:
    """Persisted workspace-level configuration source."""

    work_path: Path
    data_path: Path | None = None
    layout_version: int = 2
    bootstrap_signature: str = ""
    active_agent_key: str = ""
    session_defaults: SessionDefaults = field(default_factory=SessionDefaults)
    orchestration_defaults: dict[str, Any] = field(default_factory=dict)
    provider_policy: ProviderPolicy = field(default_factory=ProviderPolicy)

    def __post_init__(self) -> None:
        self.work_path = Path(self.work_path)
        self.data_path = self.work_path if self.data_path is None else Path(self.data_path)

    @property
    def config_path(self) -> Path:
        return self.work_path

    def to_dict(self) -> dict[str, Any]:
        return {
            "work_path": str(self.work_path),
            "data_path": str(self.data_path),
            "layout_version": self.layout_version,
            "bootstrap_signature": self.bootstrap_signature,
            "active_agent_key": self.active_agent_key,
            "session_defaults": {
                "history_max_messages": self.session_defaults.history_max_messages,
                "history_max_chars": self.session_defaults.history_max_chars,
                "max_recent_participant_messages": self.session_defaults.max_recent_participant_messages,
                "enable_auto_compression": self.session_defaults.enable_auto_compression,
            },
            "orchestration_defaults": dict(self.orchestration_defaults),
            "provider_policy": {
                "prefer_workspace_registry": self.provider_policy.prefer_workspace_registry
            },
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "WorkspaceConfig":
        session_defaults_payload = payload.get("session_defaults", {})
        provider_policy_payload = payload.get("provider_policy", {})
        return cls(
            work_path=Path(payload.get("work_path", ".")),
            data_path=Path(payload.get("data_path", payload.get("work_path", "."))),
            layout_version=int(payload.get("layout_version", 2)),
            bootstrap_signature=str(payload.get("bootstrap_signature", "")).strip(),
            active_agent_key=str(payload.get("active_agent_key", "")).strip(),
            session_defaults=SessionDefaults(
                history_max_messages=int(session_defaults_payload.get("history_max_messages", 24)),
                history_max_chars=int(session_defaults_payload.get("history_max_chars", 6000)),
                max_recent_participant_messages=int(
                    session_defaults_payload.get("max_recent_participant_messages", 5)
                ),
                enable_auto_compression=bool(
                    session_defaults_payload.get("enable_auto_compression", True)
                ),
            ),
            orchestration_defaults=dict(payload.get("orchestration_defaults", {})),
            provider_policy=ProviderPolicy(
                prefer_workspace_registry=bool(
                    provider_policy_payload.get("prefer_workspace_registry", True)
                ),
            ),
        )


@dataclass(slots=True)
class MultiModelConfig:
    """多模型协作配置对象。"""

    task_models: dict[str, str]
    task_temperatures: dict[str, float] | None = None
    task_max_tokens: dict[str, int] | None = None
    task_retries: dict[str, int] | None = None
    max_multimodal_inputs_per_turn: int = 4
    max_multimodal_value_length: int = 4096

    def __post_init__(self) -> None:
        if self.task_temperatures is None:
            self.task_temperatures = {}
        if self.task_max_tokens is None:
            self.task_max_tokens = {}
        if self.task_retries is None:
            self.task_retries = {}

    def to_orchestration_policy(self) -> OrchestrationPolicy:
        """转换为 OrchestrationPolicy 对象。"""
        return OrchestrationPolicy(
            unified_model="",
            task_models=self.task_models,
            task_temperatures=self.task_temperatures or {},
            task_max_tokens=self.task_max_tokens or {},
            task_retries=self.task_retries or {},
            max_multimodal_inputs_per_turn=self.max_multimodal_inputs_per_turn,
            max_multimodal_value_length=self.max_multimodal_value_length,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_models": dict(self.task_models),
            "task_temperatures": dict(self.task_temperatures or {}),
            "task_max_tokens": dict(self.task_max_tokens or {}),
            "task_retries": dict(self.task_retries or {}),
            "max_multimodal_inputs_per_turn": self.max_multimodal_inputs_per_turn,
            "max_multimodal_value_length": self.max_multimodal_value_length,
        }


@dataclass(slots=True, init=False)
class SessionConfig:
    """Session configuration including agent, paths, and orchestration policy."""

    preset: AgentPreset
    work_path: Path
    data_path: Path
    history_max_messages: int = 24
    history_max_chars: int = 6000
    max_recent_participant_messages: int = 5
    enable_auto_compression: bool = True
    orchestration: OrchestrationPolicy = field(default_factory=OrchestrationPolicy)
    session_id: str = "default"

    def __init__(
        self,
        *,
        work_path: Path,
        data_path: Path | None = None,
        preset: AgentPreset,
        history_max_messages: int = 24,
        history_max_chars: int = 6000,
        max_recent_participant_messages: int = 5,
        enable_auto_compression: bool = True,
        orchestration: OrchestrationPolicy | None = None,
        session_id: str = "default",
    ) -> None:
        self.preset = preset
        self.work_path = Path(work_path)
        self.data_path = self.work_path if data_path is None else Path(data_path)
        self.history_max_messages = history_max_messages
        self.history_max_chars = history_max_chars
        self.max_recent_participant_messages = max_recent_participant_messages
        self.enable_auto_compression = enable_auto_compression
        self.session_id = str(session_id).strip() or "default"

        # If no orchestration provided, create default: use main AI model as unified model
        if orchestration is None:
            orchestration = OrchestrationPolicy(unified_model=preset.agent.model)

        self.orchestration = orchestration
        # Validate multi-model orchestration configuration
        self.orchestration.validate()

    @property
    def agent(self) -> Agent:
        return self.preset.agent

    @agent.setter
    def agent(self, value: Agent) -> None:
        self.preset = AgentPreset(
            agent=value, global_system_prompt=self.preset.global_system_prompt
        )

    @property
    def global_system_prompt(self) -> str:
        return self.preset.global_system_prompt

    @global_system_prompt.setter
    def global_system_prompt(self, value: str) -> None:
        self.preset = AgentPreset(agent=self.preset.agent, global_system_prompt=value)

    @property
    def config_path(self) -> Path:
        return self.work_path
