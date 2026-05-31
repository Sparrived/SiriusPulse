"""EmotionalGroupChatEngine core: base class definition, __init__, public API, persistence.

This module contains the _EmotionalGroupChatEngineBase class.
Other methods are mixed in from companion modules via multiple inheritance
in emotional_engine.py to form the complete EmotionalGroupChatEngine.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sirius_pulse.core.bg_tasks import BackgroundTasks
from sirius_pulse.core.brain import Brain
from sirius_pulse.core.engine_persistence import EnginePersistence
from sirius_pulse.core.engine_sticker import EngineSticker
from sirius_pulse.core.pinned_message import PinnedMessageManager
from sirius_pulse.core.pipeline import Pipeline
from sirius_pulse.core.constants import HEARTBEAT_TIMEOUT_SECONDS, REPLY_DEDUP_WINDOW_SECONDS
from sirius_pulse.core.cognition import CognitionAnalyzer
from sirius_pulse.core.delayed_response_queue import DelayedResponseQueue
from sirius_pulse.core.events import SessionEvent, SessionEventBus, SessionEventType
from sirius_pulse.core.helpers import Helpers
from sirius_pulse.core.identity_resolver import IdentityResolver
from sirius_pulse.core.model_router import ModelRouter
from sirius_pulse.core.proactive_trigger import ProactiveTrigger
from sirius_pulse.core.prompt_factory import StyleAdapter
from sirius_pulse.core.response_strategy import ResponseStrategyEngine
from sirius_pulse.core.rhythm import RhythmAnalyzer
from sirius_pulse.core.threshold_engine import ThresholdEngine

# New v2 memory system (refactor)
from sirius_pulse.memory.basic import BasicMemoryFileStore, BasicMemoryManager
from sirius_pulse.memory.biography.view import BiographyView
from sirius_pulse.memory.cold_detector import ColdDetector
from sirius_pulse.memory.context_assembler import ContextAssembler
from sirius_pulse.memory.diary import DiaryManager
from sirius_pulse.memory.evolution.chain import EvolutionChain
from sirius_pulse.memory.glossary import GlossaryManager
from sirius_pulse.memory.semantic.manager import SemanticMemoryManager
from sirius_pulse.memory.situation.extractor import SituationExtractor
from sirius_pulse.memory.situation.store import SituationStore
from sirius_pulse.memory.storage import MemoryStorage
from sirius_pulse.memory.user.unified_manager import UnifiedUserManager
from sirius_pulse.models.emotion import AssistantEmotionState, EmotionState
from sirius_pulse.models.intent_v3 import IntentAnalysisV3
from sirius_pulse.models.models import Message, Transcript, UnifiedUser
from sirius_pulse.models.response_strategy import StrategyDecision

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
    ) -> None:
        self.config = dict(config or {})
        self.provider_async = provider_async
        self.work_path = work_path
        self._vector_store = vector_store
        self._embedding_client = embedding_client
        self._adapter: Any = None  # 由 add_skill_bridge() 注入，plugin 直接取用
        self._persona_db_conn = persona_db_conn

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
        self._init_pinned_messages()
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
            "proactive_generate": chat_model,
            "passive_skill": chat_model,
            "github_monitor_notify": chat_model,
            "diary_generate": memory_model,
            "diary_consolidate": memory_model,
            "biography_distill": memory_model,
            "biography_update": memory_model,
            "plugin_generate": plugin_model,
            "plugin_analyze": plugin_model,
            "plugin_render": plugin_model,
            "plugin_raw": plugin_model,
        }
        orch_task_models = orch.get("task_models")
        if isinstance(orch_task_models, dict):
            for task, model in orch_task_models.items():
                if isinstance(model, str) and model.strip() and model.strip() != "__inherit__":
                    self._task_models[task] = model.strip()
        self._task_models.update(self.config.get("task_models", {}))
        self._orch_task_temperatures = orch.get("task_temperatures")
        self._orch_task_max_tokens = orch.get("task_max_tokens")

    def _init_memory_system(self) -> None:
        # 共享同一个 SQLite 存储（persona.db）
        self._memory_storage = MemoryStorage(
            self.work_path / "persona.db",
            conn=self._persona_db_conn,
        )

        self.semantic_memory = SemanticMemoryManager(self.work_path, storage=self._memory_storage)

        self.basic_memory = BasicMemoryManager()
        self.basic_store = BasicMemoryFileStore(self.work_path)
        self.diary_manager = DiaryManager(
            self.work_path,
            vector_store=self._vector_store,
            embedding_client=self._embedding_client,
            memory_storage=self._memory_storage,
        )
        # ── 新记忆体系组件（共享 persona.db 连接）──
        self.evolution_chain = EvolutionChain(
            conn=self._persona_db_conn,
            embedding_client=self._embedding_client,
        )
        self.user_manager = UnifiedUserManager(
            self.work_path,
            persona_name=self.persona.name,
            persona_aliases=self.persona.aliases,
            conn=self._persona_db_conn,
            evolution_chain=self.evolution_chain,
        )
        self.identity_resolver = IdentityResolver()
        self.situation_store = SituationStore(
            conn=self._persona_db_conn,
        )
        self.situation_extractor = SituationExtractor()
        self.biography_view = BiographyView(
            self.evolution_chain,
            user_manager=self.user_manager,
        )
        self.cold_detector = ColdDetector()

        # DiarySlice 存储和三路召回
        from sirius_pulse.memory.diary.slice_retriever import DiarySliceRetriever
        from sirius_pulse.memory.diary.slice_store import DiarySliceStore
        from sirius_pulse.memory.diary.slice_vector_store import DiarySliceVectorStore
        self.slice_store = DiarySliceStore(self.work_path)
        self.slice_vector_store = DiarySliceVectorStore(
            persist_dir=self.work_path / "chroma_slices",
            model_name=getattr(self._embedding_client, "model_name", ""),
        )
        self.slice_retriever = DiarySliceRetriever(
            embedding_client=self._embedding_client,
            vector_store=self.slice_vector_store,
        )

        # 启动时加载历史切片到检索器
        all_slices = self.slice_store.load_all()
        for s in all_slices:
            self.slice_retriever.add(s)
        if all_slices:
            logger.info("已加载 %d 个历史日记切片", len(all_slices))

        self.context_assembler = ContextAssembler(
            self.basic_memory,
            self.diary_manager._retriever,
            situation_store=self.situation_store,
            biography_view=self.biography_view,
            slice_retriever=self.slice_retriever,
        )
        self.glossary_manager = GlossaryManager(self.work_path, persona_name=self.persona.name)

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
        self.threshold_engine = ThresholdEngine()
        self.strategy_engine = ResponseStrategyEngine()
        self.delayed_queue = DelayedResponseQueue()
        self.proactive_trigger = ProactiveTrigger(
            silence_threshold_minutes=self.config.get("proactive_silence_minutes", 60),
            active_start_hour=self.config.get("proactive_active_start_hour", 8),
            active_end_hour=self.config.get("proactive_active_end_hour", 23),
        )
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
            tone_alignment_fn=self._get_tone_alignment,
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
        )

    def _init_skill_plugin_and_runtime(self) -> None:
        self._group_last_message_at: dict[str, str] = {}
        self._transcripts: dict[str, Transcript] = {}
        self._last_reply_at: dict[str, float] = {}
        self._last_reply_depth: dict[str, int] = {}
        self._proactive_enabled_groups: set[str] = set()
        self._proactive_disabled_groups: set[str] = set()
        self._last_proactive_at: dict[str, str] = {}

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

        self._sticker_names: list[str] = []

        self._bg_tasks: set[asyncio.Task] = set()
        self._bg_running = False

        self._delayed_event_emitted: dict[str, set[str]] = {}

        self._developer_private_groups: set[str] = set()
        self._pending_developer_chats: dict[str, list[str]] = {}
        self._last_developer_chat_at: dict[str, float] = {}

        self._pending_reminders: dict[str, list[dict[str, Any]]] = {}
        self._current_adapter_type: str = ""
        # Bot 在各平台的 UID（如 {"qq_native_sirius_pulse": "123456"}）
        self._bot_platform_uids: dict[str, str] = {}

        self._recent_sent_replies: dict[str, list[tuple[float, str]]] = {}
        self._reply_dedup_window = self.config.get("reply_dedup_window_seconds", REPLY_DEDUP_WINDOW_SECONDS)
        self._reply_dedup_threshold = self.config.get("reply_dedup_threshold", 0.85)

        self._active_private_groups: set[str] = set()

        self._topic_window: dict[str, list[set[str]]] = {}

        self._pending_biography: dict[str, Any] = {}
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

    def _init_pinned_messages(self) -> None:
        """初始化消息钉住管理器。"""
        from sirius_pulse.persona_config import PersonaExperienceConfig

        # 从 experience 配置中获取最大携带次数
        experience_path = Path(self.work_path) / "experience.json"
        experience = PersonaExperienceConfig.load(experience_path)
        max_carry_count = experience.pinned_message_max_carry_count

        self._pinned_manager = PinnedMessageManager(max_carry_count=max_carry_count)

        # 注入钉住消息回调到 Brain
        if hasattr(self, 'brain'):
            self.brain.set_context_fns(
                pinned_messages_fn=self.get_pinned_messages_for_prompt,
            )

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

    def _get_tone_alignment(self, group_id: str) -> str:
        """Detect current group tone from atmosphere history for style alignment."""
        return self._helpers.get_tone_alignment(group_id)

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
        return self._helpers.enhance_topic_relevance(base_score, message, group_id, user_id)

    # ==================================================================
    # 向后兼容的委托方法（委托给 BackgroundTasks 组件）
    # ==================================================================

    def start_background_tasks(self) -> None:
        """Start periodic background tasks."""
        self._bg_tasks_mgr.start()

    def stop_background_tasks(self) -> None:
        """Cancel all background tasks."""
        self._bg_tasks_mgr.stop()

    async def proactive_check(
        self,
        group_id: str,
        *,
        _now: Any | None = None,
    ) -> dict[str, Any] | None:
        """Check if proactive trigger should fire for a group."""
        return await self._bg_tasks_mgr.proactive_check(group_id, _now=_now)

    async def tick_delayed_queue(
        self,
        group_id: str,
        on_partial_reply: Any | None = None,
    ) -> list[dict[str, Any]]:
        """Process delayed response queue for a group."""
        return await self._bg_tasks_mgr.tick_delayed_queue(group_id, on_partial_reply)

    def pop_developer_chats(self, group_id: str) -> list[str]:
        """Pop pending proactive developer chats for a group."""
        return self._bg_tasks_mgr.pop_developer_chats(group_id)

    def pop_reminders(self, group_id: str, adapter_type: str | None = None) -> list[str]:
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

    async def _cognition(
        self,
        content: str,
        user_id: str,
        group_id: str,
        *,
        sender_type: str = "human",
        multimodal_inputs: list[dict[str, str]] | None = None,
        caller_is_developer: bool = False,
    ) -> tuple[Any, Any, list[Any], Any]:
        """Cognitive layer: unified emotion + intent + empathy + memory retrieval."""
        return await self._pipeline.cognition(
            content, user_id, group_id,
            sender_type=sender_type,
            multimodal_inputs=multimodal_inputs,
            caller_is_developer=caller_is_developer,
        )

    def _decision(
        self,
        intent: Any,
        emotion: Any,
        group_id: str,
        user_id: str,
        sender_type: str = "human",
        content: str = "",
    ) -> Any:
        """Decision layer: strategy selection with threshold and rhythm."""
        return self._pipeline.decision(intent, emotion, group_id, user_id, sender_type, content)

    async def _execution(
        self,
        decision: Any,
        message: Any,
        intent: Any,
        emotion: Any,
        memories: list[Any],
        group_id: str,
        empathy: Any,
        user_id: str,
    ) -> dict[str, Any]:
        """Execution layer: generate or queue reply."""
        return await self._pipeline.execution(
            decision, message, intent, emotion, memories, group_id, empathy, user_id
        )

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

    def _save_proactive_state(self) -> None:
        """Persist proactive enabled/disabled groups and last trigger timestamps."""
        self._persistence.save_proactive_state()

    def _load_proactive_state(self) -> None:
        """Restore proactive state from disk."""
        self._persistence.load_proactive_state()

    def set_proactive_enabled(self, group_id: str, enabled: bool) -> None:
        """Enable or disable proactive triggers for a specific group."""
        self._persistence.set_proactive_enabled(group_id, enabled)

    def is_proactive_enabled(self, group_id: str) -> bool:
        """Check if proactive triggers are enabled for a group."""
        return self._persistence.is_proactive_enabled(group_id)

    # ==================================================================
    # 消息钉住 API（委托给 PinnedMessageManager）
    # ==================================================================

    def pin_message(
        self,
        content: str,
        speaker: str = "",
        group_id: str = "default",
        reason: str = "",
        ttl_hours: float | None = None,
        max_carry_count: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """钉住一条消息。

        Args:
            content: 消息内容
            speaker: 发言者名称
            group_id: 所属群组 ID
            reason: 钉住原因
            ttl_hours: 消息存活时间（小时）
            max_carry_count: 最大携带次数（超过后自动取消）
            metadata: 额外元数据

        Returns:
            钉住的消息信息
        """
        pinned = self._pinned_manager.pin_message(
            content=content,
            speaker=speaker,
            group_id=group_id,
            reason=reason,
            ttl_hours=ttl_hours,
            max_carry_count=max_carry_count,
            metadata=metadata,
        )

        return pinned.to_dict()

    def unpin_message(self, message_id: str) -> bool:
        """取消钉住一条消息。

        Args:
            message_id: 消息 ID

        Returns:
            是否成功取消
        """
        return self._pinned_manager.unpin_message(message_id)

    def unpin_by_reason(self, reason: str) -> int:
        """根据原因取消钉住消息。

        Args:
            reason: 钉住原因

        Returns:
            取消的数量
        """
        return self._pinned_manager.unpin_by_reason(reason)

    def get_pinned_messages(
        self,
        group_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """获取钉住的消息列表。

        Args:
            group_id: 过滤指定群组的消息，None 表示所有群组

        Returns:
            钉住的消息列表
        """
        messages = self._pinned_manager.get_pinned_messages(group_id=group_id)
        return [msg.to_dict() for msg in messages]

    def get_pinned_messages_for_prompt(self, group_id: str) -> list[Any]:
        """获取钉住的消息列表（用于 prompt 注入），并增加携带计数。

        每次调用此方法，所有返回的消息的携带计数都会增加。
        当携带计数超过最大携带次数时，消息会被自动取消钉住。

        Args:
            group_id: 群组 ID

        Returns:
            钉住的消息对象列表
        """
        return self._pinned_manager.get_pinned_messages_for_prompt(group_id=group_id)

    def get_pinned_statistics(self) -> dict[str, Any]:
        """获取钉住消息的统计信息。

        Returns:
            统计信息字典
        """
        return self._pinned_manager.get_statistics()

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
          0  = 对话深度追踪（适用: response_generate / proactive_generate）
         20  = 表情包发送（适用: response_generate / proactive_generate）
         30  = 回复去重（仅适用: response_generate）
         40  = 记忆记录（适用: response_generate / proactive_generate）
         50  = 回复时间戳+持久化（适用: response_generate / proactive_generate）
        """
        _engine = self

        _TASKS_CHAT = {"response_generate"}
        _TASKS_CHAT_PROACTIVE = {"response_generate", "proactive_generate"}

        # ── priority 0: 对话深度追踪 ──
        def _hook_depth(
            _brain: Any, _req: Any, _result: Any, ctx: dict[str, Any]
        ) -> None:
            gid = _req.group_id
            now_ts = time.time()
            last_ts = _engine._last_reply_at.get(gid, 0)
            _engine._last_reply_depth[gid] = (
                _engine._last_reply_depth.get(gid, 0) + 1 if now_ts - last_ts < 2 * HEARTBEAT_TIMEOUT_SECONDS else 1
            )

        # ── priority 15: 钉住/取消钉住指令解析 ──
        def _hook_pin_messages(
            _brain: Any, _req: Any, _result: Any, ctx: dict[str, Any]
        ) -> None:
            from sirius_pulse.core.pinned_message import (
                parse_pin_messages, parse_unpin_messages, strip_pin_messages
            )

            raw_text = _result.raw_text
            pin_calls = parse_pin_messages(raw_text)
            unpin_calls = parse_unpin_messages(raw_text)

            # 处理取消钉住指令
            if unpin_calls:
                gid = _req.group_id
                for call in unpin_calls:
                    try:
                        if call.get("all"):
                            # 取消所有钉住
                            count = _engine._pinned_manager.unpin_all(group_id=gid)
                            logger.info("模型取消所有钉住: %d 条", count)
                        elif call.get("reason"):
                            # 根据原因取消钉住
                            count = _engine._pinned_manager.unpin_by_reason(call["reason"])
                            logger.info("模型根据原因取消钉住: %s, %d 条", call["reason"], count)
                        elif call.get("content"):
                            # 根据内容关键词取消钉住
                            count = _engine._pinned_manager.unpin_by_content(call["content"])
                            logger.info("模型根据内容取消钉住: %s, %d 条", call["content"], count)
                    except Exception as exc:
                        logger.warning("取消钉住失败: %s", exc)

            # 处理钉住指令
            if pin_calls:
                gid = _req.group_id

                # 获取最近的消息历史（用于引用）
                recent_messages = _engine._get_recent_messages(gid, n=10)

                for call in pin_calls:
                    try:
                        content = call.get("content", "")
                        index = call.get("index", 0)

                        # 如果没有指定内容，根据 index 获取原始消息
                        if not content:
                            if index == 0:
                                # 默认钉住当前用户消息
                                content = _req.messages[-1].get("content", "") if _req.messages else ""
                            elif recent_messages:
                                # 使用 index 引用历史消息（负数表示从后往前）
                                msg_index = index if index < 0 else index
                                if abs(msg_index) <= len(recent_messages):
                                    content = recent_messages[msg_index].get("content", "")

                        if not content:
                            logger.warning("无法获取要钉住的消息内容")
                            continue

                        # 获取消息的发言者信息
                        speaker = ""
                        user_id = ""
                        if index == 0 and _req.messages:
                            # 当前用户消息
                            last_msg = _req.messages[-1]
                            speaker = last_msg.get("speaker", "")
                            user_id = last_msg.get("user_id", "")
                        elif recent_messages and abs(index) <= len(recent_messages):
                            msg = recent_messages[index]
                            user_id = msg.get("user_id", "")

                        _engine.pin_message(
                            content=content,
                            speaker=speaker or "用户",
                            group_id=gid,
                            reason=call.get("reason", ""),
                            metadata={"user_id": user_id} if user_id else None,
                        )
                        logger.info("模型主动钉住消息: %s", content[:50])
                    except Exception as exc:
                        logger.warning("钉住消息失败: %s", exc)

            # 从 clean_text 中移除钉住/取消钉住指令标记
            _result.clean_text = strip_pin_messages(_result.clean_text)

        # ── priority 20: 表情包发送 ──
        def _hook_stickers(
            _brain: Any, _req: Any, _result: Any, ctx: dict[str, Any]
        ) -> None:
            if not _result.sticker_names:
                return
            asyncio.create_task(
                _engine._send_stickers_by_names(_req.group_id, _result.sticker_names)
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
                    gid, window, threshold, _result.clean_text[:40],
                )
                _result.clean_text = ""
            else:
                recent.append((now_ts, _result.clean_text))
            _engine._recent_sent_replies[gid] = recent

        # ── priority 40: 记忆记录 ──
        def _hook_memory(
            _brain: Any, _req: Any, _result: Any, ctx: dict[str, Any]
        ) -> None:
            # 确定要记录的内容：优先使用 clean_text，
            # 若为空但有表情包则记录表情包标签（确保纯表情包回复也被记录）
            record_content = _result.clean_text
            if not record_content:
                if _result.sticker_names:
                    record_content = f"[STICKERS: {', '.join(_result.sticker_names)}]"
                else:
                    return

            # 收集被处理的标签（仅模型输出相关）
            entry_tags: list[dict[str, str]] = []

            # 模型输出的表情包标签
            if _result.sticker_names:
                names_str = ", ".join(_result.sticker_names[:3])
                entry_tags.append({
                    "type": "sticker",
                    "label": f"表情包: {names_str}" if _result.sticker_names else "表情包"
                })

            # 模型输出的钉住/取消钉住指令
            from sirius_pulse.core.pinned_message import (
                parse_pin_messages, parse_unpin_messages
            )
            raw_text = _result.raw_text
            pin_calls = parse_pin_messages(raw_text)
            unpin_calls = parse_unpin_messages(raw_text)
            if pin_calls:
                entry_tags.append({"type": "pin", "label": f"钉住消息 ×{len(pin_calls)}"})
            if unpin_calls:
                entry_tags.append({"type": "unpin", "label": f"取消钉住 ×{len(unpin_calls)}"})

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
            _engine._last_reply_at[_req.group_id] = (
                datetime.now(timezone.utc).timestamp()
            )
            _engine._persist_group_state(_req.group_id)

        # task_filter 交给 Brain 调度时检查，hook 闭包不关心
        self.brain.register_post_hook(_hook_depth, priority=0, task_filter=_TASKS_CHAT_PROACTIVE)
        self.brain.register_post_hook(_hook_pin_messages, priority=15, task_filter=_TASKS_CHAT_PROACTIVE)
        self.brain.register_post_hook(_hook_stickers, priority=20, task_filter=_TASKS_CHAT_PROACTIVE)
        self.brain.register_post_hook(_hook_dedup, priority=30, task_filter=_TASKS_CHAT)
        self.brain.register_post_hook(_hook_memory, priority=40, task_filter=_TASKS_CHAT_PROACTIVE)
        self.brain.register_post_hook(_hook_timestamp, priority=50, task_filter=_TASKS_CHAT_PROACTIVE)

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
            - strategy: str (immediate / delayed / silent / proactive)
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
            names = [self.persona.name.lower()] + [a.lower() for a in self.persona.aliases]
            text = (message.content or "").lower()
            is_mentioned = any(name in text for name in names if name)
            if not is_mentioned:
                # 混合方案：短消息直接静默（省 LLM 调用），长消息走完整 pipeline
                if len(message.content or "") < 30:
                    self._log_inner_thought(f"{speaker} 是另一个 AI，说得很短，我先默默听着～")
                    return {
                        "strategy": "silent",
                        "reply": None,
                        "emotion": {},
                        "intent": {},
                    }
                self._log_inner_thought(f"{speaker} 是另一个 AI，但说得挺长，让我认真想想...")

        self._log_inner_thought(f"{speaker} 在群里说话了，让我仔细听听看～")
        await self.event_bus.emit(
            SessionEvent(
                type=SessionEventType.PERCEPTION_COMPLETED,
                data={"group_id": group_id, "user_id": user_id},
            )
        )

        # ── 管线短路：已有 pending 队列项时，直接合并消息，跳过认知/决策 ──
        # 插件命令需要走完整管线，不参与短路合并
        if self.delayed_queue.has_pending(group_id):
            is_plugin_cmd, plugin_result = await self._check_plugin_intent(content, group_id)
            if is_plugin_cmd:
                # 向量匹配 + LLM 验证通过，直接执行插件（跳过完整管线）
                return await self._execute_verified_plugin(
                    plugin_result, message, group_id, user_id
                )
            else:
                # 非插件请求，短路合并
                merged = self.delayed_queue.merge_incoming(
                    group_id=group_id,
                    user_id=user_id,
                    message_content=content,
                    speaker_name=message.speaker or "",
                    channel=message.channel,
                    channel_user_id=message.channel_user_id,
                    multimodal_inputs=message.multimodal_inputs,
                )
                if merged:
                    self._log_inner_thought(f"已有待回复的消息，把 {speaker} 的话也合进去～")
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
            has_sticker = any(m.get("sub_type") == "1" for m in (message.multimodal_inputs or []))
            label = "动画表情" if has_sticker else "图片"
            self._log_inner_thought(f"{speaker} 发了一张{label}，我先默默记下来～")
            intent, emotion, memories, empathy = await self._cognition(
                content,
                user_id,
                group_id,
                sender_type=message.sender_type,
                multimodal_inputs=message.multimodal_inputs,
                caller_is_developer=caller_is_developer,
            )
            # 回写图片/表情描述到 basic_memory
            # 优先使用 sticker_caption（动画表情缓存），否则使用 image_caption
            caption = intent.image_caption or getattr(intent, 'sticker_caption', '')
            if caption:
                recent = self.basic_memory.get_context(group_id, n=1)
                if recent:
                    last_entry = recent[0]
                    if has_sticker:
                        # 动画表情：去掉无意义的文件哈希，替换为描述
                        stripped = re.sub(
                            r"(?:\[动画表情[：:][^\]]*\]|【动画表情：[^】]+】)",
                            "", last_entry.content or "",
                        ).strip()
                        sticker_tag = f"【动画表情：{caption}】"
                        last_entry.content = (
                            f"{stripped} {sticker_tag}" if stripped else sticker_tag
                        )
                    else:
                        last_entry.content = f"【图片】【图片描述：{caption}】"
                    if last_entry.multimodal_inputs:
                        for m in last_entry.multimodal_inputs:
                            if m.get("type") == "image":
                                m["caption"] = caption
            return {
                "strategy": "silent",
                "reply": None,
                "emotion": emotion.to_dict() if emotion else {},
                "intent": intent.to_dict() if intent else {},
            }

        # ── 插件命令快速拦截（规则匹配，零 LLM 成本） ──
        # 精确命令（如 /ca analyse）应在进入意图识别之前被拦截，
        # 避免被 LLM 当作自然语言处理。
        is_plugin_cmd, plugin_result = await self._check_plugin_intent(content, group_id)
        if is_plugin_cmd:
            plugin_exec_result = await self._execute_verified_plugin(
                plugin_result, message, group_id, user_id
            )
            # 感知层已记录消息，更新后台状态
            self._background_update(group_id, message, None, None, user_id)
            return plugin_exec_result

        # 2. Cognition (unified emotion + intent)
        intent, emotion, memories, empathy = await self._cognition(
            content,
            user_id,
            group_id,
            sender_type=message.sender_type,
            multimodal_inputs=message.multimodal_inputs,
            caller_is_developer=caller_is_developer,
        )

        # 如果 cognition 生成了图片描述，回写到 basic_memory 最后一条 entry
        # 优先使用 sticker_caption（动画表情缓存），否则使用 image_caption
        caption = intent.image_caption or getattr(intent, 'sticker_caption', '')
        if caption:
            recent = self.basic_memory.get_context(group_id, n=1)
            if recent:
                last_entry = recent[0]
                original_content = last_entry.content or ""

                # 判断是否是动画表情（sub_type=1）
                # 动画表情的【动画表情：xxx.jpg】文件哈希对模型无意义，
                # 有缓存 caption 后应替换为描述文字
                is_sticker = False
                if last_entry.multimodal_inputs:
                    for m in last_entry.multimodal_inputs:
                        if m.get("type") == "image" and m.get("sub_type") == "1":
                            is_sticker = True
                            break

                if is_sticker:
                    # 去掉无意义的文件哈希，替换为有意义的描述
                    # 兼容适配器的半角方括号 [动画表情："xxx"] 和认知模块的全角方括号 【动画表情：xxx】
                    stripped = re.sub(
                        r"(?:\[动画表情[：:][^\]]*\]|【动画表情：[^】]+】)", "", original_content
                    ).strip()
                    sticker_tag = f"【动画表情：{caption}】"
                    last_entry.content = (
                        f"{stripped} {sticker_tag}" if stripped else sticker_tag
                    )
                    # 也存入 multimodal_inputs 供 sticker learning 管道使用
                    for m in last_entry.multimodal_inputs:
                        if m.get("type") == "image":
                            m["caption"] = caption
                else:
                    if self._is_pure_image_message(original_content):
                        last_entry.content = f"【图片】【图片描述：{caption}】"
                    else:
                        last_entry.content = f"{original_content} 【图片描述：{caption}】"
                    if last_entry.multimodal_inputs:
                        for m in last_entry.multimodal_inputs:
                            if m.get("type") == "image":
                                m["caption"] = caption
        # 内心活动：理解消息后的感受
        self._log_cognition_thought(speaker, intent, emotion)
        await self.event_bus.emit(
            SessionEvent(
                type=SessionEventType.COGNITION_COMPLETED,
                data={
                    "group_id": group_id,
                    "user_id": user_id,
                    "intent": intent.to_dict(),
                    "emotion": emotion.to_dict(),
                },
            )
        )

        # Semantic: passive group norm learning from message content
        self.semantic_memory.learn_from_message(
            group_id=group_id,
            speaker_id=user_id,
            content=content or "",
        )

        # 3. Decision
        decision = self._decision(
            intent, emotion, group_id, user_id, message.sender_type or "human", content
        )
        await self.event_bus.emit(
            SessionEvent(
                type=SessionEventType.DECISION_COMPLETED,
                data={
                    "group_id": group_id,
                    "strategy": decision.strategy.value,
                    "priority": getattr(decision, "priority", None),
                },
            )
        )

        # 4. Execution
        # Warm up diary index for this group (lazy-loads from disk on first call)
        self.diary_manager.ensure_group_loaded(group_id)
        result = await self._execution(
            decision, message, intent, emotion, memories, group_id, empathy, user_id
        )
        # 内心活动：执行后的反馈
        self._log_execution_thought(speaker, decision, result)
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

        # 6. Track developer private chats for proactive memory conversations
        if group_id.startswith("private_") and participants:
            from sirius_pulse.developer_profiles import metadata_declares_developer

            if metadata_declares_developer(participants[0].metadata):
                self._developer_private_groups.add(group_id)

        # 7. Background memory updates
        self._background_update(group_id, message, emotion, intent, user_id)

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
                    from sirius_pulse.core.plugin_intent_verifier import PluginIntentResult

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
                        result.plugin_name, result.confidence, result.reason,
                    )
                    return True, result
                else:
                    logger.debug("插件意图验证未通过: %s", result.reason)
            except Exception as exc:
                logger.debug("插件意图验证异常: %s", exc)

        return False, None

    def _get_context_for_plugin_verify(
        self, group_id: str, n: int = 5
    ) -> str:
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
        my_names = [self.persona.name.lower()] + [a.lower() for a in self.persona.aliases]
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

    def _log_inner_thought(self, thought: str, intensity: float = 0.5) -> None:
        """Log an inner thought for observability."""
        logger.info("[内心] %s", thought)

    def _log_cognition_thought(
        self, speaker: str, intent: IntentAnalysisV3, emotion: EmotionState
    ) -> None:
        """Log cognition-phase inner thought."""
        intent_type = getattr(getattr(intent, "social_intent", None), "value", "")
        basic_emotion = getattr(getattr(emotion, "basic_emotion", None), "name", "")
        thought = (
            f"{speaker} 的消息让我感觉 {basic_emotion or '平静'}，"
            f"意图是 {intent_type or '未知'}，"
            f" directed_score={getattr(intent, 'directed_score', 0):.2f}"
        )
        self._log_inner_thought(thought)

    def _log_decision_thought(self, intent: IntentAnalysisV3, decision: StrategyDecision) -> None:
        """Log decision-phase inner thought."""
        strategy = (
            decision.strategy.value
            if hasattr(decision.strategy, "value")
            else str(decision.strategy)
        )
        thought = (
            f"决策结果: {strategy}，"
            f"score={getattr(decision, 'score', 0):.2f}，"
            f"threshold={getattr(decision, 'threshold', 0):.2f}"
        )
        self._log_inner_thought(thought)

    def _log_execution_thought(
        self, speaker: str, decision: StrategyDecision, result: dict[str, Any]
    ) -> None:
        """Log execution-phase inner thought."""
        reply = result.get("reply")
        strategy = result.get("strategy", "unknown")
        if reply:
            thought = f"回复了 {speaker}: {reply[:40]}..."
        else:
            thought = f"对 {speaker} 选择 {strategy}，没有回复"
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
