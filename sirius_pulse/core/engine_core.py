"""EmotionalGroupChatEngine core: base class definition, __init__, public API, persistence.

This module contains the _EmotionalGroupChatEngineBase class.
Other methods are mixed in from companion modules via multiple inheritance
in emotional_engine.py to form the complete EmotionalGroupChatEngine.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sirius_pulse.core.bg_tasks import BackgroundTasks
from sirius_pulse.core.bg_tasks_delayed import (
    CONTINUE_TOOL_DEF,
    FLOW_CONTROL_TOOL_NAMES,
    STOP_TOOL_DEF,
)
from sirius_pulse.core.brain import Brain
from sirius_pulse.core.cognition import CognitionAnalyzer
from sirius_pulse.core.constants import (
    HEARTBEAT_TIMEOUT_SECONDS,
    REPLY_DEDUP_WINDOW_SECONDS,
)
from sirius_pulse.core.delayed_response_queue import DelayedResponseQueue
from sirius_pulse.core.engine_persistence import EnginePersistence
from sirius_pulse.core.engine_sticker import EngineSticker
from sirius_pulse.core.events import SessionEvent, SessionEventBus, SessionEventType
from sirius_pulse.core.helpers import Helpers
from sirius_pulse.core.identity_resolver import IdentityResolver
from sirius_pulse.core.model_router import ModelRouter
from sirius_pulse.core.plan_runtime import (
    append_plan_event,
    finish_plan_session,
    get_active_plan_session,
    route_message_for_active_plan,
)
from sirius_pulse.core.pipeline import Pipeline
from sirius_pulse.core.prompt_factory import PromptFactory, StyleAdapter
from sirius_pulse.core.rhythm import RhythmAnalyzer

# New v2 memory system (refactor)
from sirius_pulse.memory.basic import BasicMemoryFileStore, BasicMemoryManager
from sirius_pulse.memory.biography.view import BiographyView
from sirius_pulse.memory.cold_detector import ColdDetector
from sirius_pulse.memory.context_assembler import ContextAssembler
from sirius_pulse.memory.diary import DiaryManager
from sirius_pulse.memory.evolution.chain import EvolutionChain
from sirius_pulse.memory.glossary import GlossaryManager
from sirius_pulse.memory.semantic.manager import SemanticMemoryManager
from sirius_pulse.memory.storage import MemoryStorage
from sirius_pulse.memory.user.unified_manager import UnifiedUserManager
from sirius_pulse.models.emotion import AssistantEmotionState, EmotionState
from sirius_pulse.models.models import Message, Transcript, UnifiedUser
from sirius_pulse.models.response_strategy import ResponseStrategy, StrategyDecision

logger = logging.getLogger(__name__)


class _EmotionalGroupChatEngineBase:
    """Next-generation engine for emotional group chat."""

    def __init__(
        self,
        *,
        work_path: Any,
        provider_async: Any | None = None,
        config: dict[str, Any] | None = None,
        persona: Any | None = None,
        vector_store: Any | None = None,
        embedding_client: Any | None = None,
        persona_db_conn: Any | None = None,
        remote_bridge: Any | None = None,
    ) -> None:
        self.config = dict(config or {})
        self.provider_async = provider_async
        self.work_path = work_path
        self._vector_store = vector_store
        self._embedding_client = embedding_client
        self._adapter: Any = None  # 由 add_skill_bridge() 注入，plugin 直接取用
        self._persona_db_conn = persona_db_conn
        self._remote_bridge = remote_bridge

        self._init_expressiveness()
        self._init_persona(persona)
        self._init_orchestration_and_task_models()
        self._init_memory_system()
        self._init_cognitive_layer()
        self._init_decision_layer()
        self._init_model_router()
        self._init_brain()
        self._init_event_bus_and_persistence(work_path)
        self._init_skill_plugin_and_runtime()
        self._init_helpers()
        self._init_bg_tasks()
        self._init_pipeline()
        self._init_persistence()
        self._init_sticker()

        self._register_engine_hooks()

    def _init_expressiveness(self) -> None:
        from sirius_pulse.config.models import ExpressivenessConfig

        expr_cfg = self.config.get("expressiveness", {})
        if isinstance(expr_cfg, (int, float)):
            expr_cfg = {"expressiveness": float(expr_cfg)}
        self.expressiveness = ExpressivenessConfig.from_dict(
            expr_cfg if isinstance(expr_cfg, dict) else {}
        )

    def _init_persona(self, persona: Any) -> None:
        from sirius_pulse.core.persona_store import PersonaStore
        from sirius_pulse.models.persona import PersonaProfile

        if persona is not None:
            self.persona = (
                persona
                if isinstance(persona, PersonaProfile)
                else PersonaProfile.from_dict(dict(persona))
            )
        else:
            loaded = PersonaStore.load(self.work_path)
            if loaded:
                self.persona = loaded
            else:
                raise ValueError(
                    "No persona provided and no saved persona found. "
                    "Please create a persona first (via PersonaStore.save)."
                )

    def _init_orchestration_and_task_models(self) -> None:
        from sirius_pulse.core.orchestration_store import OrchestrationStore

        orch = OrchestrationStore.load(self.work_path)
        if not orch:
            orch = {
                "analysis_model": "gpt-4o-mini",
                "chat_model": "gpt-4o",
                "memory_model": "gpt-4o-mini",
                "plugin_model": "gpt-4o-mini",
            }
            OrchestrationStore.save(self.work_path, orch)
        analysis_model = orch.get("analysis_model", "gpt-4o-mini")
        chat_model = orch.get("chat_model", "gpt-4o")
        memory_model = orch.get("memory_model", "gpt-4o-mini")
        plugin_model = orch.get("plugin_model", "gpt-4o-mini")
        self._default_model = analysis_model
        self._task_models = {
            "cognition_analyze": analysis_model,
            "memory_extract": analysis_model,
            "response_generate": chat_model,
            "passive_skill": chat_model,
            "github_monitor_notify": chat_model,
            "diary_generate": memory_model,
            "diary_consolidate": memory_model,
            "plugin_generate": plugin_model,
            "plugin_analyze": plugin_model,
            "plugin_render": plugin_model,
            "plugin_raw": plugin_model,
        }
        orch_task_models = orch.get("task_models")
        if isinstance(orch_task_models, dict):
            for task, model in orch_task_models.items():
                if (
                    isinstance(model, str)
                    and model.strip()
                    and model.strip() != "__inherit__"
                ):
                    self._task_models[task] = model.strip()
        self._task_models.update(self.config.get("task_models", {}))
        self._orch_task_temperatures = orch.get("task_temperatures")
        self._orch_task_max_tokens = orch.get("task_max_tokens")
        self._orch_task_timeout = orch.get("task_timeout")
        self._orch_task_fallback_model = orch.get("task_fallback_model")

    def _init_memory_system(self) -> None:
        # 共享同一个 SQLite 存储（persona.db）
        self._memory_storage = MemoryStorage(
            self.work_path / "persona.db",
            conn=self._persona_db_conn,
        )

        self.semantic_memory = SemanticMemoryManager(
            self.work_path, storage=self._memory_storage
        )

        self.basic_memory = BasicMemoryManager()
        self.basic_store = BasicMemoryFileStore(
            self.work_path, remote_bridge=self._remote_bridge
        )
        self.diary_manager = DiaryManager(
            self.work_path,
            vector_store=self._vector_store,
            embedding_client=self._embedding_client,
            memory_storage=self._memory_storage,
        )
        self.user_manager = UnifiedUserManager(
            self.work_path,
            persona_name=self.persona.name,
            persona_aliases=self.persona.aliases,
            conn=self._persona_db_conn,
        )
        self.identity_resolver = IdentityResolver()

        # ── 新记忆体系组件（共享 persona.db 连接）──
        self.evolution_chain = EvolutionChain(
            conn=self._persona_db_conn,
        )
        self.biography_view = BiographyView(
            self.evolution_chain,
            user_manager=self.user_manager,
        )
        self.cold_detector = ColdDetector()

        self.context_assembler = ContextAssembler(
            self.basic_memory,
            self.diary_manager._retriever,
            biography_view=self.biography_view,
            is_source_diarized=self.diary_manager.is_source_diarized,
        )
        self.glossary_manager = GlossaryManager(
            self.work_path, persona_name=self.persona.name
        )

    def _init_cognitive_layer(self) -> None:
        self.cognition_analyzer = CognitionAnalyzer(
            provider_async=self.provider_async,
            model_name=self._task_models.get("cognition_analyze", self._default_model),
            ai_name=self.persona.name,
            ai_aliases=self.persona.aliases,
            persona=self.persona,
            plugin_registry=None,  # 后续由 set_plugin_runtime 注入（v1.2+）
        )

    def _init_decision_layer(self) -> None:
        self.delayed_queue = DelayedResponseQueue()
        self.rhythm_analyzer = RhythmAnalyzer()

    def _init_model_router(self) -> None:
        self._other_ai_names = list(self.config.get("other_ai_names", []))
        self.style_adapter = StyleAdapter()
        task_overrides: dict[str, dict[str, Any]] = {}
        for task, model in self._task_models.items():
            override: dict[str, Any] = {"model_name": model}
            if isinstance(self._orch_task_temperatures, dict):
                t = self._orch_task_temperatures.get(task)
                if isinstance(t, (int, float)):
                    override["temperature"] = float(t)
            if isinstance(self._orch_task_max_tokens, dict):
                m = self._orch_task_max_tokens.get(task)
                if isinstance(m, int):
                    override["max_tokens"] = m
            if isinstance(self._orch_task_timeout, dict):
                to = self._orch_task_timeout.get(task)
                if isinstance(to, (int, float)):
                    override["timeout"] = float(to)
            if isinstance(self._orch_task_fallback_model, dict):
                fb = self._orch_task_fallback_model.get(task)
                if isinstance(fb, str) and fb.strip():
                    override["fallback_model"] = fb.strip()
            task_overrides[task] = override
        if self.config.get("task_model_overrides"):
            for task, patch in self.config["task_model_overrides"].items():
                if task in task_overrides:
                    task_overrides[task].update(patch)
                else:
                    task_overrides[task] = dict(patch)
        self.model_router = ModelRouter(
            overrides=task_overrides,
        )

    def _init_brain(self) -> None:
        self._token_records: list[Any] = []
        self.brain = Brain(
            provider_async=self.provider_async,
            model_router=self.model_router,
            persona=self.persona,
            rhythm_analyzer=self.rhythm_analyzer,
            style_adapter=self.style_adapter,
            config=self.config,
            token_usage_records=self._token_records,
            other_ai_names=self._other_ai_names,
        )
        self.brain.set_context_fns(
            recent_messages_fn=self._get_recent_messages,
            classify_exception_fn=self._classify_exception,
        )
        self.cognition_analyzer.brain = self.brain

    def _init_event_bus_and_persistence(self, work_path: Any) -> None:
        from sirius_pulse.core.engine_persistence import EngineStateStore

        self._state_store = EngineStateStore(work_path)

        baseline = self.persona.emotional_baseline
        self.assistant_emotion = AssistantEmotionState(
            valence=baseline.get("valence", 0.2),
            arousal=baseline.get("arousal", 0.3),
        )

        self.event_bus = SessionEventBus()

        from sirius_pulse.config import TokenUsageRecord

        self.token_usage_records: list[TokenUsageRecord] = self._token_records
        self.token_store: Any | None = None  # injected by EngineRuntime

        from sirius_pulse.memory.cognition_store import CognitionEventStore

        self.cognition_store = CognitionEventStore(
            Path(work_path) / "cognition_events.db",
            conn=self._persona_db_conn,
            batch_size=1,
        )

    def _init_skill_plugin_and_runtime(self) -> None:
        self._group_last_message_at: dict[str, str] = {}
        self._transcripts: dict[str, Transcript] = {}
        self._last_reply_at: dict[str, float] = {}
        self._last_reply_depth: dict[str, int] = {}

        self._skill_registry: Any | None = None
        self._skill_executor: Any | None = None
        self._passive_skill_tasks: dict[str, asyncio.Task] = {}
        self._passive_skill_triggers: dict[str, list[Any]] = {}
        self._passive_skill_unloaders: list[tuple[Any, Any]] = []

        self._plugin_registry: Any | None = None
        self._plugin_executor: Any | None = None
        self._plugin_dispatcher: Any | None = None
        self._plugin_intent_matcher: Any | None = None
        self._plugin_intent_verifier: Any | None = None

        self._active_plan_sessions: dict[str, Any] = {}
        self._sticker_names: list[str] = []
        self._sticker_oppositions: dict[str, list[str]] = {}

        self._bg_tasks: set[asyncio.Task] = set()
        self._bg_running = False

        self._delayed_event_emitted: dict[str, set[str]] = {}

        self._pending_reminders: dict[str, list[dict[str, Any]]] = {}
        self._current_adapter_type: str = ""
        # Bot 在各平台的 UID（如 {"qq_native_sirius_pulse": "123456"}）
        self._bot_platform_uids: dict[str, str] = {}
        self._qq_group_members: dict[str, tuple[float, list[dict[str, str]]]] = {}
        self._qq_bot_group_admin: dict[str, tuple[bool, float]] = {}

        self._recent_sent_replies: dict[str, list[tuple[float, str]]] = {}
        self._reply_dedup_window = self.config.get(
            "reply_dedup_window_seconds", REPLY_DEDUP_WINDOW_SECONDS
        )
        self._reply_dedup_threshold = self.config.get("reply_dedup_threshold", 0.85)

        self._active_private_groups: set[str] = set()

        self._topic_window: dict[str, list[set[str]]] = {}
        self._topic_window_max_size = 10

    def _init_helpers(self) -> None:
        """初始化 Helpers 组件（组合模式）。"""
        self._helpers = Helpers(self)

    def _init_bg_tasks(self) -> None:
        """初始化 BackgroundTasks 组件（组合模式）。"""
        self._bg_tasks_mgr = BackgroundTasks(self)

    def _init_pipeline(self) -> None:
        """初始化 Pipeline 组件（组合模式）。"""
        self._pipeline = Pipeline(self)

    def _init_persistence(self) -> None:
        """初始化 Persistence 组件（组合模式）。"""
        self._persistence = EnginePersistence(self)

    def _init_sticker(self) -> None:
        """初始化 Sticker 组件（组合模式）。"""
        self._sticker = EngineSticker(self)

    # ==================================================================
    # 向后兼容的委托方法（委托给 Helpers 组件）
    # ==================================================================

    def set_skill_runtime(
        self,
        *,
        skill_registry: Any | None = None,
        skill_executor: Any | None = None,
    ) -> None:
        """Attach SKILL registry and executor to the engine."""
        self._helpers.set_skill_runtime(
            skill_registry=skill_registry,
            skill_executor=skill_executor,
        )

    def update_qq_group_members(
        self, group_id: str, members: list[dict[str, Any]]
    ) -> None:
        """Cache QQ group members for prompt-time @ mention hints."""
        from sirius_pulse.core.qq_mentions import normalize_qq_member

        gid = str(group_id or "").strip()
        if not gid:
            return
        normalized = [
            normalize_qq_member(member)
            for member in members
            if isinstance(member, dict)
        ]
        self._qq_group_members[gid] = (
            time.monotonic(),
            [m for m in normalized if m["user_id"]],
        )

    def get_qq_group_members_for_prompt(
        self,
        group_id: str,
        *,
        max_age_seconds: float = 300.0,
    ) -> list[dict[str, str]]:
        gid = str(group_id or "").strip()
        cached = self._qq_group_members.get(gid)
        if not cached:
            return []
        updated_at, members = cached
        if time.monotonic() - updated_at > max_age_seconds:
            return []
        return list(members)

    def update_qq_bot_group_admin(self, group_id: str, is_admin: bool) -> None:
        gid = str(group_id or "").strip()
        if gid:
            self._qq_bot_group_admin[gid] = (bool(is_admin), time.monotonic())

    def is_qq_bot_group_admin(
        self, group_id: str, *, max_age_seconds: float = 300.0
    ) -> bool:
        gid = str(group_id or "").strip()
        cached = self._qq_bot_group_admin.get(gid)
        if not cached:
            return False
        is_admin, updated_at = cached
        if time.monotonic() - updated_at > max_age_seconds:
            return False
        return bool(is_admin)

    def set_plugin_runtime(
        self,
        *,
        plugin_registry: Any | None = None,
        plugin_executor: Any | None = None,
        plugin_dispatcher: Any | None = None,
    ) -> None:
        """Attach Plugin registry, executor, and dispatcher to the engine."""
        self._helpers.set_plugin_runtime(
            plugin_registry=plugin_registry,
            plugin_executor=plugin_executor,
            plugin_dispatcher=plugin_dispatcher,
        )

    async def _execute_plugin_command(
        self,
        decision: Any,
        message: Any,
        group_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        """Execute a Plugin command and produce the reply."""
        return await self._helpers.execute_plugin_command(
            decision, message, group_id, user_id
        )

    def _register_passive_skills(self) -> None:
        """Discover passive SKILLs and instantiate their background tasks / triggers."""
        self._helpers._register_passive_skills()

    def _wrap_event_bus_for_triggers(self) -> None:
        """Wrap event_bus.emit so passive SKILL triggers fire on matching events."""
        self._helpers._wrap_event_bus_for_triggers()

    def _get_recent_messages(self, group_id: str, n: int = 10) -> list[dict[str, Any]]:
        """获取最近n条消息。"""
        return self._helpers.get_recent_messages(group_id, n)

    @staticmethod
    def _is_pure_image_message(content: str) -> bool:
        """Check if content contains only image placeholders with no substantive text."""
        return Helpers.is_pure_image_message(content)

    @staticmethod
    def _message_rate_per_minute(recent_msgs: list[dict[str, Any]]) -> float:
        """Estimate messages per minute from recent message timestamps."""
        return Helpers.message_rate_per_minute(recent_msgs)

    @staticmethod
    def _inject_multimodal_into_user_message(
        messages: list[dict[str, Any]],
        multimodal_inputs: list[dict[str, str]] | None,
    ) -> list[dict[str, Any]]:
        """Convert the last user message's string content into OpenAI multimodal list."""
        return Helpers.inject_multimodal_into_user_message(messages, multimodal_inputs)

    def _record_subtask_tokens(
        self,
        task_name: str,
        model_name: str,
        group_id: str,
        request: Any | None = None,
        duration_ms: float = 0.0,
        token_breakdown: dict[str, int] | None = None,
    ) -> None:
        """Record token usage for a sub-task (cognition, diary, etc.)."""
        self._helpers.record_subtask_tokens(
            task_name, model_name, group_id, request, duration_ms, token_breakdown
        )

    def _classify_exception(self, exc: Exception) -> str:
        """Classify an LLM provider exception into a structured error type."""
        return self._helpers.classify_exception(exc)

    def _enhance_topic_relevance(
        self, base_score: float, message: str, group_id: str, user_id: str
    ) -> float:
        """Enhance topic relevance using semantic memory (group + user) + topic window."""
        return self._helpers.enhance_topic_relevance(
            base_score, message, group_id, user_id
        )

    # ==================================================================
    # 向后兼容的委托方法（委托给 BackgroundTasks 组件）
    # ==================================================================

    def start_background_tasks(self) -> None:
        """Start periodic background tasks."""
        self._bg_tasks_mgr.start()

    def stop_background_tasks(self) -> None:
        """Cancel all background tasks."""
        self._bg_tasks_mgr.stop()

    async def tick_delayed_queue(
        self,
        group_id: str,
        on_partial_reply: Any | None = None,
    ) -> list[dict[str, Any]]:
        """Process delayed response queue for a group."""
        return await self._bg_tasks_mgr.tick_delayed_queue(group_id, on_partial_reply)

    def pop_reminders(
        self, group_id: str, adapter_type: str | None = None
    ) -> list[str]:
        """Pop pending reminder messages for a group."""
        return self._bg_tasks_mgr.pop_reminders(group_id, adapter_type)

    # ==================================================================
    # 向后兼容的委托方法（委托给 Pipeline 组件）
    # ==================================================================

    def _perception(
        self,
        group_id: str,
        message: Any,
        participants: Any,
    ) -> str:
        """Perception layer: normalize, register participants, update transcript."""
        return self._pipeline.perception(group_id, message, participants)

    def _compute_signal(
        self,
        content: str,
        user_id: str,
        group_id: str,
        *,
        sender_type: str = "human",
        caller_is_developer: bool = False,
    ) -> Any:
        """Signal computation layer: pure rule-based analysis."""
        return self._pipeline.compute_signal(
            content,
            user_id,
            group_id,
            sender_type=sender_type,
            caller_is_developer=caller_is_developer,
        )

    def _pre_filter(
        self,
        signal: Any,
        content: str,
        user_id: str,
        group_id: str,
        sender_type: str = "human",
    ) -> str:
        """Pre-filter layer: hard guards + threshold check."""
        return self._pipeline.pre_filter(
            signal, content, user_id, group_id, sender_type
        )

    async def _generate(
        self,
        signal: Any,
        message: Any,
        group_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        """Generation layer: unified generation through delayed queue."""
        return await self._pipeline.generate(signal, message, group_id, user_id)

    def _background_update(
        self,
        group_id: str,
        message: Any,
        emotion: Any,
        intent: Any,
        user_id: str,
    ) -> None:
        """Background updates after main pipeline."""
        self._pipeline.background_update(group_id, message, emotion, intent, user_id)

    def _record_intent_scores_for_latest_message(
        self,
        group_id: str,
        message: Any,
        user_id: str,
        signal: Any,
    ) -> None:
        """Attach computed intent/signal scores to the just-recorded user message."""
        recent = self.basic_memory.get_context(group_id, n=1)
        if not recent:
            return

        entry = recent[-1]
        if getattr(entry, "role", "") != "human":
            return

        message_id = getattr(message, "message_id", "") or ""
        entry_message_id = getattr(entry, "platform_message_id", "") or ""
        if message_id and entry_message_id and message_id != entry_message_id:
            return
        if user_id and getattr(entry, "user_id", "") != user_id:
            return

        entry.intent_scores = {
            "social_intent": getattr(signal, "social_intent", ""),
            "directed_score": round(float(getattr(signal, "directed_score", 0.0)), 4),
            "urgency_score": round(float(getattr(signal, "urgency_score", 0.0)), 4),
            "relevance_score": round(float(getattr(signal, "relevance_score", 0.0)), 4),
            "sarcasm_score": round(float(getattr(signal, "sarcasm_score", 0.0)), 4),
            "entitlement_score": round(
                float(getattr(signal, "entitlement_score", 0.0)), 4
            ),
            "turn_gap_readiness": round(
                float(getattr(signal, "turn_gap_readiness", 0.0)), 4
            ),
        }
        try:
            self.basic_store.update_entry(entry)
        except Exception:
            logger.debug("Failed to update archived intent scores", exc_info=True)

    # ==================================================================
    # 向后兼容的委托方法（委托给 Persistence 组件）
    # ==================================================================

    def _persist_group_state(self, group_id: str) -> None:
        """Persist basic memory and timestamps for a single group in real-time."""
        self._persistence.persist_group_state(group_id)

    def _persist_full_state(self) -> None:
        """Persist all runtime state to disk (used on graceful shutdown)."""
        self._persistence.persist_full_state()

    def save_state(self) -> None:
        """Persist all runtime state to disk."""
        self._persistence.save_state()

    def load_state(self) -> None:
        """Restore runtime state from disk."""
        self._persistence.load_state()

    # ==================================================================
    # 向后兼容的委托方法（委托给 Sticker 组件）
    # ==================================================================

    def _init_sticker_system(self) -> None:
        """扫描 stickers 文件夹，获取可用表情包名称列表。"""
        self._sticker._init_sticker_system()

    def _pick_sticker_file(self, names: list[str]) -> Any:
        """从模型选择的名称列表中随机选一个，再匹配对应的图片文件。"""
        return self._sticker._pick_sticker_file(names)

    async def _send_stickers_by_names(
        self,
        group_id: str,
        names: list[str],
    ) -> dict[str, Any]:
        """从模型选中的名称中随机挑一个表情包发送（sub_type=1）。"""
        return await self._sticker._send_stickers_by_names(group_id, names)

    def _register_engine_hooks(self) -> None:
        """向 Brain 注册引擎级别的后处理 hook。

        两级控制：
        1. ChatRequest.post_process=True（主开关，外部/分析调用不触发）
        2. task_filter 由 Brain 在调度时自动过滤（注册时声明）

        task_filter 声明在注册参数中，不在 hook 闭包内检查。
        外部代码注册 hook 时不传 task_filter（默认 None）即始终生效。

        优先级阶梯：
          0  = 对话深度追踪
         20  = 表情包发送
         30  = 回复去重（仅适用: response_generate）
         40  = 记忆记录
         50  = 回复时间戳+持久化
        """
        _engine = self

        _TASKS_CHAT = {"response_generate"}
        _TASKS_CHAT_ALL = {"response_generate", "proactive_generate"}

        # ── priority 0: 对话深度追踪 ──
        def _hook_depth(
            _brain: Any, _req: Any, _result: Any, ctx: dict[str, Any]
        ) -> None:
            gid = _req.group_id
            now_ts = time.time()
            last_ts = _engine._last_reply_at.get(gid, 0)
            _engine._last_reply_depth[gid] = (
                _engine._last_reply_depth.get(gid, 0) + 1
                if now_ts - last_ts < 2 * HEARTBEAT_TIMEOUT_SECONDS
                else 1
            )

        # ── priority 30: 回复去重（仅常规对话）──
        def _hook_dedup(
            _brain: Any, _req: Any, _result: Any, ctx: dict[str, Any]
        ) -> None:
            if not _result.clean_text:
                return
            gid = _req.group_id
            now_ts = datetime.now(timezone.utc).timestamp()
            recent = _engine._recent_sent_replies.get(gid, [])
            window = _engine._reply_dedup_window
            threshold = _engine._reply_dedup_threshold
            recent = [(t, r) for t, r in recent if now_ts - t < window]
            if any(
                _engine._text_similarity(_result.clean_text, r) > threshold
                for _, r in recent
            ):
                logger.debug(
                    "去重抑制: %s (window=%ds, threshold=%.2f): %s...",
                    gid,
                    window,
                    threshold,
                    _result.clean_text[:40],
                )
                _result.clean_text = ""
            else:
                recent.append((now_ts, _result.clean_text))
            _engine._recent_sent_replies[gid] = recent

        # ── priority 40: 记忆记录 ──
        def _hook_memory(
            _brain: Any, _req: Any, _result: Any, ctx: dict[str, Any]
        ) -> None:
            # 确定要记录的内容：只记录实际文本回复。
            record_content = _result.clean_text
            if not record_content:
                return

            # 收集被处理的标签（仅模型输出相关）
            entry_tags: list[dict[str, str]] = []

            gid = _req.group_id
            uid = _req.user_id
            persona_name = _engine.persona.name if _engine.persona else "assistant"
            # 构建完整 LLM 消息链：system + user/assistant 交替
            chain_msgs: list[dict[str, Any]] = []
            if _result.system_prompt:
                chain_msgs.append({"role": "system", "content": _result.system_prompt})
            chain_msgs.extend(_req.messages)
            _entry = _engine.basic_memory.add_entry(
                group_id=gid,
                user_id="assistant",
                role="assistant",
                content=record_content,
                speaker_name=persona_name,
                system_prompt=_result.system_prompt,
                tags=entry_tags,
                conversation_chain=chain_msgs,
            )
            _engine.basic_store.append(_entry)
            try:
                _engine.semantic_memory.record_ai_sent(
                    group_id=gid,
                    target_user_id=uid or "",
                    topic_hint=record_content[:100],
                    response_length=len(record_content),
                )
            except Exception:
                pass

        # ── priority 50: 回复时间戳+持久化 ──
        def _hook_timestamp(
            _brain: Any, _req: Any, _result: Any, ctx: dict[str, Any]
        ) -> None:
            _engine._last_reply_at[_req.group_id] = datetime.now(
                timezone.utc
            ).timestamp()
            _engine._persist_group_state(_req.group_id)

        # ── priority 10: [REPLY:xxx] 引用回复解析 ──
        def _hook_reply_reference(
            _brain: Any, _req: Any, _result: Any, ctx: dict[str, Any]
        ) -> None:
            """解析模型输出中的 [REPLY:xxx] 指令，引用历史消息进行回复。"""
            raw_text = _result.raw_text
            if not raw_text:
                return

            # 匹配 [REPLY:xxx] / [REPLY:msg_id="xxx"] 指令（支持多个）
            reply_pattern = re.compile(
                r'\[REPLY:\s*(?:msg_id\s*=\s*"([^"]+)"|([^\]\s]+))\s*\]'
            )
            reply_matches = [
                msg_id or plain_id
                for msg_id, plain_id in reply_pattern.findall(raw_text)
            ]
            if not reply_matches:
                return

            logger.info("[REPLY] 发现引用指令: %s", reply_matches)
            _result.raw_text = reply_pattern.sub("", _result.raw_text)
            _result.clean_text = reply_pattern.sub("", _result.clean_text or "")

            # 获取所有历史消息（用于查找引用内容）
            gid = _req.group_id
            all_entries = _engine.basic_memory.get_all(gid)
            all_user_entries = [
                entry
                for entry in all_entries
                if getattr(entry, "role", "") != "assistant"
            ]
            if not all_user_entries:
                logger.info("[REPLY] 没有找到用户消息")
                return

            # 构建两个映射：
            # 1. index -> 消息内容（与 prompt 中的倒排 index 保持一致）
            # 2. platform_message_id -> 消息内容
            total = len(all_user_entries)
            index_map: dict[int, dict[str, str]] = {}
            msg_id_map: dict[str, dict[str, str]] = {}
            for i, entry in enumerate(all_user_entries):
                idx = total - i
                msg_id = getattr(entry, "platform_message_id", "") or ""
                msg_data = {
                    "content": getattr(entry, "content", "") or "",
                    "speaker": getattr(entry, "speaker_name", "")
                    or getattr(entry, "user_id", "unknown"),
                    "platform_message_id": msg_id,
                }
                index_map[idx] = msg_data
                if msg_id:
                    msg_id_map[msg_id] = msg_data

            logger.info(
                "[REPLY] 共 %d 条用户消息, %d 条有msg_id",
                len(all_user_entries),
                len(msg_id_map),
            )

            # 处理每个 [REPLY:xxx] 指令，直接存储引用信息
            refs: list[dict[str, str]] = []
            for match in reply_matches:
                ref_id = match
                # 优先通过 platform_message_id 查找
                ref_msg = msg_id_map.get(ref_id)
                if not ref_msg:
                    # 如果找不到，尝试作为索引查找
                    try:
                        ref_index = int(ref_id)
                        ref_msg = index_map.get(ref_index)
                    except ValueError:
                        pass

                if ref_msg:
                    msg_id = ref_msg.get("platform_message_id", "")
                    refs.append(
                        {
                            "msg_id": msg_id,
                            "speaker": ref_msg["speaker"],
                            "content": ref_msg["content"][:100],
                        }
                    )
                    logger.info(
                        "[REPLY] 找到引用消息: msg_id=%s, speaker=%s",
                        msg_id,
                        ref_msg["speaker"],
                    )
                else:
                    logger.info("[REPLY] 未找到 id=%s 对应的消息", ref_id)

            # 存储引用信息到 _result，adapter 直接读取
            _result.reply_references = refs

        # task_filter 交给 Brain 调度时检查，hook 闭包不关心
        _CHAT = _TASKS_CHAT
        _ALL = _TASKS_CHAT_ALL
        self.brain.register_post_hook(_hook_depth, priority=0, task_filter=_ALL)
        self.brain.register_post_hook(
            _hook_reply_reference, priority=10, task_filter=_ALL
        )
        self.brain.register_post_hook(_hook_dedup, priority=30, task_filter=_CHAT)
        self.brain.register_post_hook(_hook_memory, priority=40, task_filter=_ALL)
        self.brain.register_post_hook(_hook_timestamp, priority=50, task_filter=_ALL)

    # ==================================================================
    # Public API
    # ==================================================================

    async def process_message(
        self,
        message: Message,
        participants: list[UnifiedUser],
        group_id: str,
    ) -> dict[str, Any]:
        """Process a single incoming message through the full pipeline.

        Returns a dict with at least:
            - strategy: str (immediate / delayed / silent)
            - reply: str | None
            - emotion: dict
            - intent: dict
        """
        content = message.content
        self._current_adapter_type = message.adapter_type or ""

        # 获取当前发送者的 developer 状态（用于插件权限过滤）
        caller_is_developer = participants[0].is_developer if participants else False

        # 1. Perception (resolves stable user_id for the sender)
        user_id = self._perception(group_id, message, participants)
        speaker = message.speaker or "有人"

        # 多 AI 互动抑制：其他 AI 发言且未 @ 自己时
        if message.sender_type == "other_ai":
            names = [self.persona.name.lower()] + [
                a.lower() for a in self.persona.aliases
            ]
            text = (message.content or "").lower()
            is_mentioned = any(name in text for name in names if name)
            if not is_mentioned:
                # 混合方案：短消息直接静默（省 LLM 调用），长消息走完整 pipeline
                if len(message.content or "") < 30:
                    self._log_inner_thought(
                        f"{speaker} 是另一个 AI，说得很短，我先默默听着～"
                    )
                    return {
                        "strategy": "silent",
                        "reply": None,
                        "emotion": {},
                        "intent": {},
                    }
                self._log_inner_thought(
                    f"{speaker} 是另一个 AI，但说得挺长，让我认真想想..."
                )

        self._log_inner_thought(f"{speaker} 在群里说话了，让我仔细听听看～")
        await self.event_bus.emit(
            SessionEvent(
                type=SessionEventType.PERCEPTION_COMPLETED,
                data={"group_id": group_id, "user_id": user_id},
            )
        )

        # Bot 正在发送多段回复时到达的新消息：不打断当前发送。
        # 只有明确点名当前 bot 的消息才进入 delayed queue，避免立刻抢话。
        if self.config.get("plan_mode_enabled", False):
            active_plan = get_active_plan_session(self, group_id)
            if active_plan is not None:
                mentions_bot = self._message_explicitly_mentions_current_bot(message)
                route = route_message_for_active_plan(
                    active_plan,
                    user_id=user_id,
                    content=content,
                    mentions_current_bot=mentions_bot,
                )
                if route.action == "cancel_plan":
                    append_plan_event(
                        active_plan,
                        user_id=user_id,
                        speaker_name=message.speaker or "",
                        content=content,
                        event_type=route.event_type,
                        platform_message_id=message.message_id or "",
                    )
                    finish_plan_session(self, group_id, status="cancelled")
                    self._background_update(group_id, message, None, None, user_id)
                    self._log_inner_thought(f"{speaker} 取消了当前计划模式任务")
                    return {
                        "strategy": "plan_cancelled",
                        "reply": None,
                        "emotion": {},
                        "intent": {},
                    }
                if route.action == "plan_event":
                    append_plan_event(
                        active_plan,
                        user_id=user_id,
                        speaker_name=message.speaker or "",
                        content=content,
                        event_type=route.event_type,
                        platform_message_id=message.message_id or "",
                    )
                    self._background_update(group_id, message, None, None, user_id)
                    self._log_inner_thought(
                        f"{speaker} 的新消息并入计划模式事件: {route.event_type}"
                    )
                    return {
                        "strategy": "plan_event",
                        "reply": None,
                        "emotion": {},
                        "intent": {},
                    }
                if route.action == "light_chat" and self.config.get(
                    "plan_mode_allow_light_chat", True
                ):
                    self._log_inner_thought(
                        f"{speaker} 的消息不影响当前计划，继续走普通聊天管线"
                    )
                elif route.action == "ignore":
                    self._background_update(group_id, message, None, None, user_id)
                    return {
                        "strategy": "plan_ignored",
                        "reply": None,
                        "emotion": {},
                        "intent": {},
                    }
                else:
                    self._background_update(group_id, message, None, None, user_id)
                    return {
                        "strategy": "active_plan_silent",
                        "reply": None,
                        "emotion": {},
                        "intent": {},
                    }

        if getattr(message, "received_during_bot_send", False):
            self._background_update(group_id, message, None, None, user_id)
            if self._message_explicitly_mentions_current_bot(message):
                decision = StrategyDecision(
                    strategy=ResponseStrategy.DELAYED,
                    score=1.0,
                    threshold=0.0,
                    urgency=60.0,
                    relevance=1.0,
                    reason="received_during_bot_send_mention",
                )
                self.delayed_queue.enqueue(
                    group_id=group_id,
                    user_id=user_id,
                    message_content=content,
                    strategy_decision=decision,
                    candidate_memories=[],
                    channel=message.channel,
                    channel_user_id=message.channel_user_id,
                    multimodal_inputs=message.multimodal_inputs,
                    adapter_type=message.adapter_type,
                    heat_level="warm",
                    pace="steady",
                    speaker_name=message.speaker or "",
                    platform_message_id=message.message_id or "",
                )
                self._persist_group_state(group_id)
                self._log_inner_thought(f"{speaker} 在我发送时点名我，先排进延迟回复～")
                return {
                    "strategy": "delayed",
                    "reply": None,
                    "emotion": {},
                    "intent": {},
                }

            self._log_inner_thought(f"{speaker} 在我发送时插话，我先不打断当前回复～")
            return {
                "strategy": "silent",
                "reply": None,
                "emotion": {},
                "intent": {},
            }

        # ── 管线短路：已有 pending 队列项时，直接合并消息，跳过认知/决策 ──
        # 插件命令需要走完整管线，不参与短路合并
        if self.delayed_queue.has_pending(group_id):
            is_plugin_cmd, plugin_result = await self._check_plugin_intent(
                content, group_id
            )
            if is_plugin_cmd:
                # 向量匹配 + LLM 验证通过，直接执行插件（跳过完整管线）
                return await self._execute_verified_plugin(
                    plugin_result, message, group_id, user_id
                )
            else:
                closed = self.delayed_queue.close_pending_if_acknowledged(
                    group_id=group_id,
                    user_id=user_id,
                    message_content=content,
                )
                if closed:
                    self._log_inner_thought(
                        f"{speaker} 像是在收束刚才的话题，取消这条待回复～"
                    )
                    self._background_update(group_id, message, None, None, user_id)
                    self._persist_group_state(group_id)
                    return {
                        "strategy": "closed_pending",
                        "reply": None,
                        "emotion": {},
                        "intent": {},
                    }

                # 非插件请求，短路合并
                merged = self.delayed_queue.merge_incoming(
                    group_id=group_id,
                    user_id=user_id,
                    message_content=content,
                    speaker_name=message.speaker or "",
                    channel=message.channel,
                    channel_user_id=message.channel_user_id,
                    multimodal_inputs=message.multimodal_inputs,
                    platform_message_id=getattr(message, "message_id", "") or "",
                )
                if merged:
                    self._log_inner_thought(
                        f"已有待回复的消息，把 {speaker} 的话也合进去～"
                    )
                    self._background_update(group_id, message, None, None, user_id)
                    return {
                        "strategy": "merged",
                        "reply": None,
                        "emotion": {},
                        "intent": {},
                    }

        # Pure image message (no substantive text) -> generate caption via cognition,
        # save to context, but skip decision/execution. The later text message will
        # pull the caption from basic memory via XML history.
        if message.multimodal_inputs and self._is_pure_image_message(message.content):
            has_sticker = any(
                m.get("sub_type") == "1" for m in (message.multimodal_inputs or [])
            )
            label = "动画表情" if has_sticker else "图片"

            # 优化：对于纯动画表情消息，如果已有缓存，直接使用缓存的caption，
            # 跳过 _cognition() 调用，避免不必要的LLM意图识别
            cached_sticker_caption = ""
            if has_sticker:
                for m in message.multimodal_inputs or []:
                    if m.get("type") == "image" and m.get("sub_type") == "1":
                        path = str(m.get("value", ""))
                        cache_key = self.cognition_analyzer._image_cache_key(path)
                        cache = self.cognition_analyzer._image_caption_cache
                        if cache_key and cache_key in cache:
                            cached_sticker_caption = cache[cache_key]
                            break

            if cached_sticker_caption:
                # 缓存命中：直接使用缓存的caption，跳过_cognition()
                self._log_inner_thought(
                    f"{speaker} 发了一张{label}，缓存命中，直接记录～"
                )
                caption = cached_sticker_caption
                recent = self.basic_memory.get_context(group_id, n=1)
                if recent:
                    last_entry = recent[0]
                    # 动画表情：去掉无意义的文件哈希，替换为描述
                    stripped = re.sub(
                        r"(?:\[动画表情[：:][^\]]*\]|【动画表情：[^】]+】)",
                        "",
                        last_entry.content or "",
                    ).strip()
                    sticker_tag = f"[动画表情：{caption}]"
                    last_entry.content = (
                        f"{stripped} {sticker_tag}" if stripped else sticker_tag
                    )
                    if last_entry.multimodal_inputs:
                        for m in last_entry.multimodal_inputs:
                            if m.get("type") == "image":
                                m["caption"] = caption
                return {
                    "strategy": "silent",
                    "reply": None,
                    "emotion": {},
                    "intent": {},
                }

            self._log_inner_thought(f"{speaker} 发了一张{label}，我先默默记下来～")
            caption = await self.cognition_analyzer.describe_image(
                message.multimodal_inputs,
                is_sticker=has_sticker,
            )
            # 回写图片/表情描述到 basic_memory
            # 优先使用 sticker_caption（动画表情缓存），否则使用 image_caption
            if caption:
                recent = self.basic_memory.get_context(group_id, n=1)
                if recent:
                    last_entry = recent[0]
                    if has_sticker:
                        # 动画表情：去掉无意义的文件哈希，替换为描述
                        stripped = re.sub(
                            r"(?:\[动画表情[：:][^\]]*\]|【动画表情：[^】]+】)",
                            "",
                            last_entry.content or "",
                        ).strip()
                        sticker_tag = f"[动画表情：{caption}]"
                        last_entry.content = (
                            f"{stripped} {sticker_tag}" if stripped else sticker_tag
                        )
                    else:
                        last_entry.content = f"[图片] [图片描述：{caption}]"
                    if last_entry.multimodal_inputs:
                        for m in last_entry.multimodal_inputs:
                            if m.get("type") == "image":
                                m["caption"] = caption
            return {
                "strategy": "silent",
                "reply": None,
                "emotion": {},
                "intent": {"image_caption": caption} if caption else {},
            }

        # ── 插件命令快速拦截（规则匹配，零 LLM 成本） ──
        # 精确命令（如 /ca analyse）应在进入意图识别之前被拦截，
        # 避免被 LLM 当作自然语言处理。
        is_plugin_cmd, plugin_result = await self._check_plugin_intent(
            content, group_id
        )
        if is_plugin_cmd:
            plugin_exec_result = await self._execute_verified_plugin(
                plugin_result, message, group_id, user_id
            )
            # 感知层已记录消息，更新后台状态
            self._background_update(group_id, message, None, None, user_id)
            return plugin_exec_result

        # 2. Signal computation (pure rules, zero LLM)
        signal = self._compute_signal(
            content,
            user_id,
            group_id,
            sender_type=message.sender_type,
            caller_is_developer=caller_is_developer,
        )
        self._record_intent_scores_for_latest_message(
            group_id, message, user_id, signal
        )

        # 内心活动：理解消息后的感受
        self._log_cognition_thought(speaker, signal, signal.emotion)
        await self.event_bus.emit(
            SessionEvent(
                type=SessionEventType.COGNITION_COMPLETED,
                data={
                    "group_id": group_id,
                    "user_id": user_id,
                    "signal": signal.to_dict(),
                    "emotion": signal.emotion.to_dict() if signal.emotion else {},
                },
            )
        )

        # 3. Pre-filter (hard guards + threshold)
        filter_result = self._pre_filter(
            signal, content, user_id, group_id, message.sender_type or "human"
        )

        if filter_result == "reject":
            self._persist_group_state(group_id)
            self._background_update(group_id, message, signal.emotion, None, user_id)
            return {
                "strategy": "silent",
                "reply": None,
                "emotion": signal.emotion.to_dict() if signal.emotion else {},
                "signal": signal.to_dict(),
            }

        await self.event_bus.emit(
            SessionEvent(
                type=SessionEventType.DECISION_COMPLETED,
                data={
                    "group_id": group_id,
                    "strategy": "pass",
                    "directed_score": signal.directed_score,
                },
            )
        )

        # 4. Generate (unified path through delayed queue, model decides reply/stop)
        self.diary_manager.ensure_group_loaded(group_id)
        result = await self._generate(signal, message, group_id, user_id)
        await self.event_bus.emit(
            SessionEvent(
                type=SessionEventType.EXECUTION_COMPLETED,
                data={
                    "group_id": group_id,
                    "strategy": result.get("strategy"),
                    "has_reply": result.get("reply") is not None,
                },
            )
        )

        # 5. Track all private chats so the delivery loop can tick their delayed queue
        if group_id.startswith("private_"):
            self._active_private_groups.add(group_id)

        # 6. Background memory updates
        self._background_update(group_id, message, signal.emotion, None, user_id)

        return result

    # ------------------------------------------------------------------
    # Plugin intent detection (for pipeline short-circuit)
    # ------------------------------------------------------------------

    async def _check_plugin_intent(
        self, content: str, group_id: str
    ) -> tuple[bool, Any | None]:
        """检测消息是否可能是插件请求。

        三层检测（逐步升级）：
            1. 规则匹配（PluginRegistry.match_message）— 覆盖精确指令和关键词模板
            2. 嵌入向量相似度（PluginIntentMatcher）— 覆盖自然语言请求
            3. 轻量 LLM 验证（PluginIntentVerifier）— 确认意图并提取参数

        Args:
            content: 用户消息内容
            group_id: 群组 ID（用于获取上下文消息）

        Returns:
            (is_plugin, result) 元组：
            - is_plugin=True 表示是插件请求，result 为 PluginIntentResult
            - is_plugin=False 表示不是插件请求，result 为 None
        """
        # 第一层：规则匹配（最快，覆盖 /命令 和关键词模板）
        plugin_reg = getattr(self.cognition_analyzer, "plugin_registry", None)
        if plugin_reg is not None:
            try:
                match_result = plugin_reg.match_message(content)
                if match_result is not None:
                    # 规则匹配直接命中，构造结果
                    from sirius_pulse.core.plugin_intent_verifier import (
                        PluginIntentResult,
                    )

                    return True, PluginIntentResult(
                        is_plugin=True,
                        plugin_name=match_result.plugin_name,
                        confidence=1.0,
                        reason="rule_match",
                    )
            except Exception:
                pass

        # 第二层：嵌入向量相似度（覆盖自然语言插件请求）
        matcher = getattr(self, "_plugin_intent_matcher", None)
        candidate_plugins: list[str] = []
        if matcher is not None:
            try:
                candidate_plugins = matcher.match_plugin_candidates(content)
                if not candidate_plugins:
                    return False, None
            except Exception:
                return False, None
        else:
            # 无匹配器，跳过向量检测
            return False, None

        # 第三层：轻量 LLM 验证（向量匹配通过后确认意图并提取参数）
        # 使用候选插件列表缩小范围，并提供上下文消息辅助识别
        verifier = getattr(self, "_plugin_intent_verifier", None)
        if verifier is not None:
            try:
                # 获取最近的上下文消息（XML 格式，复用 ContextAssembler）
                context_xml = self._get_context_for_plugin_verify(group_id)

                # 获取人格信息
                persona_name = self.persona.name if self.persona else "AI"
                persona_aliases = list(self.persona.aliases) if self.persona else []

                result = await verifier.verify(
                    content,
                    candidate_plugins=candidate_plugins,
                    context_xml=context_xml,
                    persona_name=persona_name,
                    persona_aliases=persona_aliases,
                )
                if result.is_plugin:
                    logger.info(
                        "插件意图验证通过: plugin=%s confidence=%.2f reason=%s",
                        result.plugin_name,
                        result.confidence,
                        result.reason,
                    )
                    return True, result
                else:
                    logger.debug("插件意图验证未通过: %s", result.reason)
            except Exception as exc:
                logger.debug("插件意图验证异常: %s", exc)

        return False, None

    def _get_context_for_plugin_verify(self, group_id: str, n: int = 5) -> str:
        """获取最近的上下文消息（XML 格式），用于插件意图验证。

        复用 ContextAssembler.build_history_xml() 保持格式一致。

        Args:
            group_id: 群组 ID
            n: 获取最近 n 条消息

        Returns:
            XML 格式的历史消息字符串，失败返回空字符串。
        """
        try:
            return self.context_assembler.build_history_xml(group_id, n=n)
        except Exception:
            return ""

    async def _execute_verified_plugin(
        self,
        plugin_result: Any,
        message: Any,
        group_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        """执行已验证的插件（从轻量 LLM 验证器获取的结果）。

        Args:
            plugin_result: PluginIntentResult 包含插件名称和参数
            message: 原始消息对象
            group_id: 群组 ID
            user_id: 用户 ID

        Returns:
            执行结果字典。
        """
        from sirius_pulse.models.response_strategy import (
            ResponseStrategy,
            StrategyDecision,
        )

        plugin_name = plugin_result.plugin_name
        plugin_slots = plugin_result.slots or {}

        # 检查插件是否存在
        plugin_reg = getattr(self, "_plugin_registry", None)
        if plugin_reg is None:
            return {
                "strategy": "plugin_verified",
                "reply": None,
                "emotion": {},
                "intent": {},
                "error": "插件注册表未初始化",
            }

        definition = plugin_reg.get(plugin_name)
        if definition is None:
            return {
                "strategy": "plugin_verified",
                "reply": None,
                "emotion": {},
                "intent": {},
                "error": f"插件 '{plugin_name}' 未找到",
            }

        # 构造 StrategyDecision
        decision = StrategyDecision(
            strategy=ResponseStrategy.PLUGIN,
            score=plugin_result.confidence,
            threshold=0.0,
            urgency=0.0,
            relevance=0.0,
            reason=f"verified_plugin_intent:{plugin_name}",
            plugin_intent=plugin_name,
            plugin_slots=plugin_slots,
            plugin_render_mode=definition.render.mode if definition else "direct",
        )

        # 执行插件
        try:
            plugin_exec_result = await self._execute_plugin_command(
                decision=decision,
                message=message,
                group_id=group_id,
                user_id=user_id,
            )
            if plugin_exec_result.get("reply") and not plugin_exec_result.get("error"):
                self._log_inner_thought(f"轻量验证后直接执行了插件 {plugin_name}～")
                return {
                    "strategy": "plugin_verified",
                    "reply": plugin_exec_result["reply"],
                    "emotion": {},
                    "intent": {},
                    "plugin_intent": plugin_name,
                }
            return {
                "strategy": "plugin_verified",
                "reply": None,
                "emotion": {},
                "intent": {},
                "error": plugin_exec_result.get("error", "插件执行失败"),
            }
        except Exception as exc:
            logger.debug("已验证插件 '%s' 执行失败: %s", plugin_name, exc)
            return {
                "strategy": "plugin_verified",
                "reply": None,
                "emotion": {},
                "intent": {},
                "error": str(exc),
            }

    # ------------------------------------------------------------------
    # Inner thought helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _text_similarity(a: str, b: str) -> float:
        """Simple similarity metric based on character bigram Jaccard
        and prefix overlap. Returns 0.0-1.0."""
        a, b = a.strip(), b.strip()
        if not a or not b:
            return 0.0
        if a == b:
            return 1.0

        # Prefix overlap ratio
        prefix_match = 0
        for ca, cb in zip(a, b):
            if ca == cb:
                prefix_match += 1
            else:
                break
        prefix_ratio = prefix_match / max(len(a), len(b))

        # Character bigram Jaccard
        def _bigrams(s: str):
            return {s[i : i + 2] for i in range(len(s) - 1)}

        ba, bb = _bigrams(a), _bigrams(b)
        jaccard = len(ba & bb) / len(ba | bb) if ba and bb else 0.0
        return max(prefix_ratio, jaccard)

    def _message_directed_at_other_ai(self, content: str | None) -> bool:
        """检测消息是否明确指向其他 AI（提到其他 AI 名字且当前 AI 不是主被问者）。"""
        other_names = self.config.get("other_ai_names", [])
        if not other_names:
            return False
        my_names = [self.persona.name.lower()] + [
            a.lower() for a in self.persona.aliases
        ]
        text = (content or "").lower()
        mentions_me = any(name in text for name in my_names if name)
        mentions_other = any(name.lower() in text for name in other_names if name)
        if not mentions_other:
            return False
        if not mentions_me:
            # 只提到其他 AI，没提到自己 -> 抑制
            return True
        # 同时提到自己和其他 AI -> 检查谁是第一个被提到的
        # 如果自己不是第一个被提到的 -> 抑制（让第一个被提到的 AI 主导回复）
        all_names = [(n, "me") for n in my_names if n] + [
            (n.lower(), "other") for n in other_names if n
        ]
        first_pos = len(text)
        first_who = None
        for name, who in all_names:
            pos = text.find(name)
            if pos != -1 and pos < first_pos:
                first_pos = pos
                first_who = who
        return first_who == "other"

    def _message_explicitly_mentions_current_bot(self, message: Message) -> bool:
        """Return True when a message explicitly names or @mentions this bot."""
        if getattr(message, "mentions_current_bot", False):
            return True
        text = (getattr(message, "content", "") or "").strip()
        if not text or not getattr(self, "persona", None):
            return False
        names = [self.persona.name, *getattr(self.persona, "aliases", [])]
        return any(self._text_mentions_name(text, name) for name in names if name)

    @staticmethod
    def _text_mentions_name(text: str, name: str) -> bool:
        needle = (name or "").strip().lower()
        if not needle:
            return False
        haystack = (text or "").lower()
        if any("\u4e00" <= ch <= "\u9fff" for ch in needle):
            return needle in haystack
        pattern = rf"(?<![a-z0-9_])@?{re.escape(needle)}(?![a-z0-9_])"
        return re.search(pattern, haystack) is not None

    def _log_inner_thought(self, thought: str, intensity: float = 0.5) -> None:
        """Log an inner thought for observability."""
        logger.info("[内心] %s", thought)

    def _log_cognition_thought(
        self, speaker: str, signal: Any, emotion: EmotionState | None
    ) -> None:
        """Log cognition-phase inner thought."""
        intent_type = getattr(signal, "social_intent", "")
        basic_emotion = (
            getattr(getattr(emotion, "basic_emotion", None), "name", "")
            if emotion
            else ""
        )
        thought = (
            f"{speaker} 的消息让我感觉 {basic_emotion or '平静'}，"
            f"意图是 {intent_type or '未知'}，"
            f" directed_score={getattr(signal, 'directed_score', 0):.2f}"
        )
        self._log_inner_thought(thought)

    def _emotion_desc(self, emotion: EmotionState) -> str:
        """Return a short Chinese description of an emotion state."""
        basic = getattr(getattr(emotion, "basic_emotion", None), "name", "")
        valence = getattr(emotion, "valence", 0.0)
        arousal = getattr(emotion, "arousal", 0.3)
        if basic:
            return basic
        if valence > 0.3 and arousal > 0.5:
            return "兴奋"
        if valence > 0.3 and arousal <= 0.5:
            return "愉快"
        if valence < -0.3 and arousal > 0.5:
            return "愤怒"
        if valence < -0.3 and arousal <= 0.5:
            return "悲伤"
        if arousal > 0.6:
            return "紧张"
        return "平静"
