"""Sirius Chat — 支持多人格启用的异步角色扮演程序.

公开 API（直接从顶层导入）:
    from sirius_pulse import EmotionalGroupChatEngine, Message, SessionConfig
    from sirius_pulse import Brain, PreHook, PostHook  # LLM 交互中枢 + Hook 扩展

本包不再提供 `sirius_pulse.api` 子模块；所有公开符号均已平铺到顶层。
"""

from __future__ import annotations

# ── Adapter 框架（v1.3+）──
from sirius_pulse.adapters import (
    AtSegment,
    BaseAdapter,
    FileSegment,
    ImageSegment,
    MessageGroup,
    MessageSegment,
    ParsedEvent,
    ReplySegment,
    TextSegment,
    VoiceSegment,
    at,
    file,
    image,
    reply,
    text,
    voice,
)

# ── Config / Models ──
from sirius_pulse.config import (
    Agent,
    AgentPreset,
    MemoryPolicy,
    MultiModelConfig,
    OrchestrationPolicy,
    ProviderPolicy,
    SessionConfig,
    SessionDefaults,
    TokenUsageRecord,
    WorkspaceBootstrap,
    WorkspaceConfig,
)

# ── Config Builder (shared between plugins and skills) ──
from sirius_pulse.config.config_builder import (
    ConfigBuilder,
    ParamDefinition,
    build_parameters_from_class,
    config_param,
    secret,
)
from sirius_pulse.config.helpers import (
    auto_configure_multimodal_agent,
    configure_full_orchestration,
    configure_orchestration_models,
    configure_orchestration_retries,
    configure_orchestration_temperatures,
    create_agent_with_multimodal,
    create_multimodel_config,
    setup_multimodel_config,
)
from sirius_pulse.config.manager import ConfigManager

# ── Core engine ──
from sirius_pulse.core.brain import Brain, ChatRequest, ChatResult, PostHook, PreHook
from sirius_pulse.core.delayed_response_queue import DelayedResponseQueue
from sirius_pulse.core.emotional_engine import (
    EmotionalGroupChatEngine,
    create_emotional_engine,
)
from sirius_pulse.core.events import SessionEvent, SessionEventBus, SessionEventType
from sirius_pulse.core.identity_resolver import (
    IdentityContext,
    IdentityResolution,
    IdentityResolver,
)
from sirius_pulse.core.model_router import ModelRouter, TaskConfig
from sirius_pulse.core.proactive_trigger import ProactiveTrigger
from sirius_pulse.core.prompt_factory import PromptBundle, PromptFactory, StyleAdapter, StyleParams
from sirius_pulse.core.response_strategy import ResponseStrategyEngine
from sirius_pulse.core.rhythm import RhythmAnalysis, RhythmAnalyzer
from sirius_pulse.core.threshold_engine import ThresholdEngine

# ── Exceptions ──
from sirius_pulse.exceptions import (
    ConfigError,
    ConflictingMemoryError,
    ContentValidationError,
    InvalidConfigError,
    JSONParseError,
    MemoryError,
    MissingConfigError,
    ParseError,
    ProviderAuthError,
    ProviderConnectionError,
    ProviderError,
    ProviderResponseError,
    SiriusException,
    TokenBudgetExceededError,
    TokenError,
    TokenEstimationError,
    UserNotFoundError,
)

# ── Logging ──
from sirius_pulse.logging_config import (
    LogFormat,
    LogLevel,
    configure_logging,
    get_logger,
)

# ── Memory ──
from sirius_pulse.memory.user.unified_models import UnifiedUser

# ── Models ──
from sirius_pulse.models import Message, Transcript
from sirius_pulse.models.emotion import (
    AssistantEmotionState,
    EmotionState,
    EmpathyStrategy,
)
from sirius_pulse.models.intent_v3 import IntentAnalysisV3, SocialIntent
from sirius_pulse.models.response_strategy import ResponseStrategy, StrategyDecision

# ── Plugin system（v1.2+）──
from sirius_pulse.plugins import (
    ArgNode,
    CommandAST,
    CommandParser,
    EngineProxy,
    GroupMention,
    ImageAttachment,
    Lexer,
    MatchResult,
    MessageContext,
    MessageReference,
    OutputDispatcher,
    PatternType,
    PluginBase,
    PluginCommandDef,
    PluginCommandMeta,
    PluginContext,
    PluginDataStore,
    PluginDefinition,
    PluginEventDef,
    PluginExecutor,
    PluginLoader,
    PluginLoadError,
    PluginMatcher,
    PluginNaturalLangDef,
    PluginParameterDef,
    PluginPermissionDef,
    PluginRegistry,
    PluginRenderDef,
    PluginResponse,
    RenderMode,
    Tokenizer,
    TriggerType,
    UserMention,
    command,
    match_plugin,
    parse_command,
)
from sirius_pulse.providers import (
    AliyunBailianProvider,
    AutoRoutingProvider,
    BigModelProvider,
    MimoProvider,
    MimoTokenPlanProvider,
    MockProvider,
    OpenAICompatibleProvider,
    ProviderConfig,
    ProviderRegistry,
    SiliconFlowProvider,
    VolcengineArkProvider,
    WorkspaceProviderManager,
    ensure_provider_platform_supported,
    get_supported_provider_platforms,
    merge_provider_sources,
    normalize_provider_type,
    probe_provider_availability,
    register_provider_with_validation,
    run_provider_detection_flow,
)

# ── Providers ──
from sirius_pulse.providers.base import AsyncLLMProvider, LLMProvider

# ── Session / Workspace ──
from sirius_pulse.session.store import JsonSessionStore, SessionStoreFactory, SqliteSessionStore

# ── Skills ──
from sirius_pulse.skills import (
    BackgroundTaskSpec,
    SkillDataStore,
    SkillDefinition,
    SkillEngineContext,
    SkillExecutor,
    SkillInvocationContext,
    SkillParameter,
    SkillPassiveType,
    SkillRegistry,
    SkillResult,
    TriggerSpec,
)
from sirius_pulse.token.analytics import (
    AnalyticsReport,
    BaselineDict,
    BucketDict,
    TimeSliceDict,
    compute_baseline,
    full_report,
    group_by_actor,
    group_by_model,
    group_by_session,
    group_by_task,
    time_series,
)

# ── Token usage ──
from sirius_pulse.token.token_store import TokenUsageStore
from sirius_pulse.token.usage import (
    TokenUsageBaseline,
    build_token_usage_baseline,
    summarize_token_usage,
)
from sirius_pulse.utils.layout import WorkspaceLayout

__all__ = [
    # Core engine
    "EmotionalGroupChatEngine",
    "create_emotional_engine",
    "Brain",
    "ChatRequest",
    "ChatResult",
    "PreHook",
    "PostHook",
    "SessionEvent",
    "SessionEventBus",
    "SessionEventType",
    "IdentityResolver",
    "IdentityContext",
    "IdentityResolution",
    "ModelRouter",
    "TaskConfig",
    "ProactiveTrigger",
    "PromptFactory",
    "PromptBundle",
    "StyleAdapter",
    "StyleParams",
    "ResponseStrategyEngine",
    "DelayedResponseQueue",
    "RhythmAnalysis",
    "RhythmAnalyzer",
    "ThresholdEngine",
    # Config
    "Agent",
    "AgentPreset",
    "MemoryPolicy",
    "MultiModelConfig",
    "OrchestrationPolicy",
    "ProviderPolicy",
    "SessionConfig",
    "SessionDefaults",
    "TokenUsageRecord",
    "WorkspaceBootstrap",
    "WorkspaceConfig",
    "ConfigManager",
    "configure_full_orchestration",
    "configure_orchestration_models",
    "configure_orchestration_retries",
    "configure_orchestration_temperatures",
    "auto_configure_multimodal_agent",
    "create_agent_with_multimodal",
    "create_multimodel_config",
    "setup_multimodel_config",
    # Models
    "Message",
    "Transcript",
    "EmotionState",
    "AssistantEmotionState",
    "EmpathyStrategy",
    "IntentAnalysisV3",
    "SocialIntent",
    "ResponseStrategy",
    "StrategyDecision",
    # Memory
    "UnifiedUser",
    # Providers
    "AliyunBailianProvider",
    "AsyncLLMProvider",
    "AutoRoutingProvider",
    "BigModelProvider",
    "LLMProvider",
    "MimoProvider",
    "MimoTokenPlanProvider",
    "MockProvider",
    "OpenAICompatibleProvider",
    "ProviderConfig",
    "ProviderRegistry",
    "SiliconFlowProvider",
    "VolcengineArkProvider",
    "WorkspaceProviderManager",
    "ensure_provider_platform_supported",
    "get_supported_provider_platforms",
    "merge_provider_sources",
    "normalize_provider_type",
    "probe_provider_availability",
    "register_provider_with_validation",
    "run_provider_detection_flow",
    # Session / Workspace
    "JsonSessionStore",
    "SessionStoreFactory",
    "SqliteSessionStore",
    "WorkspaceLayout",
    # Skills
    "BackgroundTaskSpec",
    "SkillDataStore",
    "SkillDefinition",
    "SkillEngineContext",
    "SkillExecutor",
    "SkillInvocationContext",
    "SkillParameter",
    "SkillPassiveType",
    "SkillRegistry",
    "SkillResult",
    "TriggerSpec",
    # Config Builder (shared between plugins and skills)
    "ConfigBuilder",
    "ParamDefinition",
    "config_param",
    "secret",
    "build_parameters_from_class",
    # Plugin system（v1.2+）
    "PluginBase",
    "PluginContext",
    "PluginDefinition",
    "PluginResponse",
    "CommandAST",
    "PluginRegistry",
    "PluginLoader",
    "PluginLoadError",
    "PluginExecutor",
    "OutputDispatcher",
    "Tokenizer",
    "Lexer",
    "CommandParser",
    "PluginMatcher",
    "MatchResult",
    "EngineProxy",
    "MessageContext",
    "PluginDataStore",
    "ArgNode",
    "RenderMode",
    "TriggerType",
    "PatternType",
    "PluginCommandDef",
    "PluginEventDef",
    "PluginParameterDef",
    "PluginPermissionDef",
    "PluginRenderDef",
    "PluginNaturalLangDef",
    "UserMention",
    "GroupMention",
    "MessageReference",
    "ImageAttachment",
    "parse_command",
    "match_plugin",
    "command",
    "PluginCommandMeta",
    # Adapter 框架（v1.3+）
    "BaseAdapter",
    "MessageGroup",
    "MessageSegment",
    "TextSegment",
    "AtSegment",
    "ImageSegment",
    "VoiceSegment",
    "FileSegment",
    "ReplySegment",
    "ParsedEvent",
    "text",
    "at",
    "image",
    "voice",
    "file",
    "reply",
    # Token
    "TokenUsageStore",
    "AnalyticsReport",
    "BaselineDict",
    "BucketDict",
    "TimeSliceDict",
    "TokenUsageBaseline",
    "build_token_usage_baseline",
    "compute_baseline",
    "full_report",
    "group_by_actor",
    "group_by_model",
    "group_by_session",
    "group_by_task",
    "summarize_token_usage",
    "time_series",
    # Logging
    "LogFormat",
    "LogLevel",
    "configure_logging",
    "get_logger",
    # Exceptions
    "SiriusException",
    "ProviderError",
    "ProviderConnectionError",
    "ProviderAuthError",
    "ProviderResponseError",
    "TokenError",
    "TokenBudgetExceededError",
    "TokenEstimationError",
    "ParseError",
    "JSONParseError",
    "ContentValidationError",
    "ConfigError",
    "InvalidConfigError",
    "MissingConfigError",
    "MemoryError",
    "UserNotFoundError",
    "ConflictingMemoryError",
]

# 为一些缺少文档的导入项添加 docstring
AsyncLLMProvider.__doc__ = "Base class for LLM providers."
JsonSessionStore.__doc__ = "JSON-based session store for persisting sessions."
SqliteSessionStore.__doc__ = "SQLite-based session store for persisting sessions."
build_token_usage_baseline.__doc__ = "Build a baseline for token usage metrics."
summarize_token_usage.__doc__ = "Summarize token usage statistics."
