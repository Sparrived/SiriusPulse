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
from typing import TYPE_CHECKING, Any

from sirius_pulse.core.brain import Brain
from sirius_pulse.core.cognition import CognitionAnalyzer
from sirius_pulse.core.delayed_response_queue import DelayedResponseQueue, _parse_iso
from sirius_pulse.core.events import SessionEvent, SessionEventBus, SessionEventType
from sirius_pulse.core.identity_resolver import IdentityContext, IdentityResolver
from sirius_pulse.core.model_router import ModelRouter, TaskConfig
from sirius_pulse.core.proactive_trigger import ProactiveTrigger
from sirius_pulse.core.prompt_factory import StyleAdapter, StyleParams
from sirius_pulse.core.response_strategy import ResponseStrategyEngine
from sirius_pulse.core.rhythm import RhythmAnalyzer
from sirius_pulse.core.threshold_engine import ThresholdEngine

# New v2 memory system (refactor)
from sirius_pulse.memory.basic import BasicMemoryFileStore, BasicMemoryManager
from sirius_pulse.memory.biography import BiographyManager
from sirius_pulse.memory.context_assembler import ContextAssembler
from sirius_pulse.memory.diary import DiaryManager
from sirius_pulse.memory.glossary import GlossaryManager, GlossaryTerm
from sirius_pulse.memory.semantic.manager import SemanticMemoryManager
from sirius_pulse.memory.user.simple import UserManager
from sirius_pulse.models.emotion import AssistantEmotionState, EmotionState
from sirius_pulse.models.intent_v3 import IntentAnalysisV3
from sirius_pulse.models.models import Message, Participant, Transcript
from sirius_pulse.models.response_strategy import ResponseStrategy, StrategyDecision

logger = logging.getLogger(__name__)


class _EmotionalGroupChatEngineBase:
    """Next-generation engine for emotional group chat."""

    # ─── Mixin 方法桩声明（运行时由 PipelineMixin / BackgroundTasksMixin / HelpersMixin 提供）───

    if TYPE_CHECKING:

        def _perception(self, group_id: str, message: Any, participants: Any) -> str: ...
        async def _cognition(
            self,
            content: str,
            user_id: str,
            group_id: str,
            *,
            sender_type: str = "human",
            multimodal_inputs: list[dict[str, str]] | None = None,
            caller_is_developer: bool = False,
        ) -> tuple[Any, Any, list[Any], Any]: ...
        def _decision(
            self, intent: Any, emotion: Any, group_id: str, user_id: str, sender_type: str = "human"
        ) -> Any: ...
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
        ) -> dict[str, Any]: ...
        def _background_update(
            self, group_id: str, message: Any, emotion: Any, intent: Any, user_id: str
        ) -> None: ...
        def _get_recent_messages(self, group_id: str, n: int = 10) -> list[dict[str, Any]]: ...
        def _get_tone_alignment(self, group_id: str) -> str: ...
        @staticmethod
        def _strip_conversation_history_xml(text: str) -> str: ...
        @staticmethod
        def _is_pure_image_message(content: str) -> bool: ...
        def _record_subtask_tokens(
            self,
            task_name: str,
            model_name: str,
            group_id: str,
            request: Any | None = None,
            duration_ms: float = 0.0,
            token_breakdown: dict[str, int] | None = None,
        ) -> None: ...
        def _classify_exception(self, exc: Exception) -> str: ...
        async def _execute_plugin_command(
            self,
            decision: Any,
            message: Any,
            group_id: str,
            user_id: str,
        ) -> dict[str, Any]: ...
        def _enhance_topic_relevance(
            self, base_score: float, message: str, group_id: str, user_id: str
        ) -> float: ...
        @staticmethod
        def _message_rate_per_minute(recent_msgs: list[dict[str, Any]]) -> float: ...
        @staticmethod
        def _inject_multimodal_into_user_message(
            messages: list[dict[str, Any]], multimodal_inputs: list[dict[str, str]] | None
        ) -> list[dict[str, Any]]: ...
        def _register_passive_skills(self) -> None: ...
        def _wrap_event_bus_for_triggers(self) -> None: ...

    def __init__(
        self,
        *,
        work_path: Any,
        provider_async: Any | None = None,
        config: dict[str, Any] | None = None,
        persona: Any | None = None,
        vector_store: Any | None = None,
        embedding_client: Any | None = None,
    ) -> None:
        self.config = dict(config or {})
        self.provider_async = provider_async
        self.work_path = work_path
        self._vector_store = vector_store
        self._embedding_client = embedding_client
        self._adapter: Any = None  # 由 add_skill_bridge() 注入，plugin 直接取用

        # Expressiveness regulator (single-knob)
        from sirius_pulse.config.models import ExpressivenessConfig

        expr_cfg = self.config.get("expressiveness", {})
        if isinstance(expr_cfg, (int, float)):
            expr_cfg = {"expressiveness": float(expr_cfg)}
        self.expressiveness = ExpressivenessConfig.from_dict(
            expr_cfg if isinstance(expr_cfg, dict) else {}
        )

        # Persona loading
        from sirius_pulse.core.persona_generator import PersonaGenerator
        from sirius_pulse.core.persona_store import PersonaStore
        from sirius_pulse.models.persona import PersonaProfile

        if persona is not None:
            self.persona = (
                persona
                if isinstance(persona, PersonaProfile)
                else PersonaProfile.from_dict(dict(persona))
            )
        else:
            # Try load from disk
            loaded = PersonaStore.load(work_path)
            if loaded:
                self.persona = loaded
            else:
                raise ValueError(
                    "No persona provided and no saved persona found. "
                    "Please create a persona first (via PersonaStore.save)."
                )

        # Load orchestration config (unified model configuration)
        from sirius_pulse.core.orchestration_store import OrchestrationStore

        orch = OrchestrationStore.load(work_path)
        if not orch:
            orch = {
                "analysis_model": "gpt-4o-mini",
                "chat_model": "gpt-4o",
                "memory_model": "gpt-4o-mini",
                "plugin_model": "gpt-4o-mini",
            }
            OrchestrationStore.save(work_path, orch)
        analysis_model = orch.get("analysis_model", "gpt-4o-mini")
        chat_model = orch.get("chat_model", "gpt-4o")
        memory_model = orch.get("memory_model", "gpt-4o-mini")
        plugin_model = orch.get("plugin_model", "gpt-4o-mini")
        self._default_model = analysis_model
        self._task_models = {
            # 分析类
            "cognition_analyze": analysis_model,
            "memory_extract": analysis_model,
            # 生成类（主动发言、被动技能、GitHub 通知均跟随对话模型）
            "response_generate": chat_model,
            "proactive_generate": chat_model,
            "passive_skill": chat_model,
            "github_monitor_notify": chat_model,
            # 记忆维护
            "diary_generate": memory_model,
            "diary_consolidate": memory_model,
            "biography_distill": memory_model,
            "biography_update": memory_model,
            # 插件与技能
            "plugin_generate": plugin_model,
            "plugin_analyze": plugin_model,
            "plugin_render": plugin_model,
            "plugin_raw": plugin_model,
        }
        # 优先使用 orchestration.json 中的 task_models 细粒度覆盖
        orch_task_models = orch.get("task_models")
        if isinstance(orch_task_models, dict):
            for task, model in orch_task_models.items():
                if isinstance(model, str) and model.strip():
                    self._task_models[task] = model.strip()
        # 允许外部通过 config 直接覆盖具体任务模型（最高优先级）
        self._task_models.update(self.config.get("task_models", {}))

        # Memory foundation
        self.semantic_memory = SemanticMemoryManager(work_path)

        self.basic_memory = BasicMemoryManager(
            hard_limit=self.config.get("basic_memory_hard_limit", 30),
            context_window=self.config.get("basic_memory_context_window", 5),
        )
        self.basic_store = BasicMemoryFileStore(work_path)
        self.diary_manager = DiaryManager(
            work_path,
            vector_store=self._vector_store,
            embedding_client=self._embedding_client,
        )
        self.user_manager = UserManager()
        self.identity_resolver = IdentityResolver()
        self.biography_manager = BiographyManager(
            work_path,
            persona_name=self.persona.name,
            persona_aliases=self.persona.aliases,
        )
        self.context_assembler = ContextAssembler(
            self.basic_memory,
            self.diary_manager._retriever,
        )

        # Cognitive layer (unified emotion + intent)
        self.cognition_analyzer = CognitionAnalyzer(
            provider_async=provider_async,
            model_name=self._task_models.get("cognition_analyze", self._default_model),
            ai_name=self.persona.name,
            ai_aliases=self.persona.aliases,
            persona=self.persona,
            plugin_registry=None,  # 后续由 set_plugin_runtime 注入（v1.2+）
        )
        # Decision layer
        self.threshold_engine = ThresholdEngine()
        self.strategy_engine = ResponseStrategyEngine()
        self.delayed_queue = DelayedResponseQueue()
        self.proactive_trigger = ProactiveTrigger(
            silence_threshold_minutes=self.config.get("proactive_silence_minutes", 60),
            active_start_hour=self.config.get("proactive_active_start_hour", 8),
            active_end_hour=self.config.get("proactive_active_end_hour", 23),
        )
        self.rhythm_analyzer = RhythmAnalyzer()

        # Execution layer (persona-injected)
        self._other_ai_names = list(self.config.get("other_ai_names", []))
        self.style_adapter = StyleAdapter()
        task_overrides: dict[str, dict[str, Any]] = {}
        orch_task_temperatures = orch.get("task_temperatures")
        orch_task_max_tokens = orch.get("task_max_tokens")
        for task, model in self._task_models.items():
            override: dict[str, Any] = {"model_name": model}
            if isinstance(orch_task_temperatures, dict):
                t = orch_task_temperatures.get(task)
                if isinstance(t, (int, float)):
                    override["temperature"] = float(t)
            if isinstance(orch_task_max_tokens, dict):
                m = orch_task_max_tokens.get(task)
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

        # Brain：LLM 交互中枢（先创建，token_usage_records 后续同步引用）
        self._token_records: list[Any] = []
        self.brain = Brain(
            provider_async=provider_async,
            model_router=self.model_router,
            persona=self.persona,
            rhythm_analyzer=self.rhythm_analyzer,
            style_adapter=self.style_adapter,
            config=self.config,
            token_usage_records=self._token_records,
            other_ai_names=self._other_ai_names,
        )
        # 延迟注入引擎上下文函数
        self.brain.set_context_fns(
            recent_messages_fn=self._get_recent_messages,
            tone_alignment_fn=self._get_tone_alignment,
            classify_exception_fn=self._classify_exception,
        )
        # 将 Brain 注入 CognitionAnalyzer，使其使用统一的 raw_call 通道
        self.cognition_analyzer.brain = self.brain

        # Persistence
        from sirius_pulse.core.engine_persistence import EngineStateStore

        self._state_store = EngineStateStore(work_path)

        # Assistant state (persona emotional baseline)
        baseline = self.persona.emotional_baseline
        self.assistant_emotion = AssistantEmotionState(
            valence=baseline.get("valence", 0.2),
            arousal=baseline.get("arousal", 0.3),
        )

        # Group runtime state
        self._group_last_message_at: dict[str, str] = {}
        self._transcripts: dict[str, Transcript] = {}
        self._last_reply_at: dict[str, float] = {}  # group_id -> unix timestamp
        self._last_reply_depth: dict[str, int] = {}  # group_id -> consecutive reply depth
        self._proactive_enabled_groups: set[str] = set()  # empty = all enabled (backward compat)
        self._proactive_disabled_groups: set[str] = set()  # blacklist: groups explicitly disabled
        self._last_proactive_at: dict[str, str] = {}  # group_id -> ISO timestamp

        # Event bus
        self.event_bus = SessionEventBus()

        # Token usage tracking（与 Brain 共享同一个列表引用）
        from sirius_pulse.config import TokenUsageRecord

        self.token_usage_records: list[TokenUsageRecord] = self._token_records
        self.token_store: Any | None = None  # injected by EngineRuntime

        # Cognition event tracking
        from sirius_pulse.memory.cognition_store import CognitionEventStore

        self.cognition_store = CognitionEventStore(Path(work_path) / "cognition_events.db")

        # SKILL system
        self._skill_registry: Any | None = None
        self._skill_executor: Any | None = None
        self._passive_skill_tasks: dict[str, asyncio.Task] = {}
        self._passive_skill_triggers: dict[str, list[Any]] = {}
        self._passive_skill_unloaders: list[tuple[Any, Any]] = []

        # Plugin system（v1.2+）
        self._plugin_registry: Any | None = None
        self._plugin_executor: Any | None = None
        self._plugin_dispatcher: Any | None = None

        # 表情包名称列表（从 stickers 文件夹扫描，不含扩展名）
        self._sticker_names: list[str] = []

        self.glossary_manager = GlossaryManager(work_path, persona_name=self.persona.name)

        # Background tasks
        self._bg_tasks: set[asyncio.Task] = set()
        self._bg_running = False

        # Track which delayed-queue items have already emitted trigger events
        # per group_id to avoid duplicate events across smart-sleep ticks.
        self._delayed_event_emitted: dict[str, set[str]] = {}

        # Developer proactive chat state
        self._developer_private_groups: set[str] = set()
        self._pending_developer_chats: dict[str, list[str]] = {}
        self._last_developer_chat_at: dict[str, float] = {}

        # Reminder state
        self._pending_reminders: dict[str, list[dict[str, Any]]] = {}
        self._current_adapter_type: str = ""

        # Reply deduplication
        self._recent_sent_replies: dict[str, list[tuple[float, str]]] = {}
        self._reply_dedup_window = self.config.get("reply_dedup_window_seconds", 300)
        self._reply_dedup_threshold = self.config.get("reply_dedup_threshold", 0.85)

        # Active private groups for delayed queue ticking
        self._active_private_groups: set[str] = set()

        # v1.3+: 短期话题窗口 —— 每个群最近 N 条消息的关键词快照
        # 用于跨轮次话题关联增强，key=group_id, value=[set(keywords), ...]
        self._topic_window: dict[str, list[set[str]]] = {}
        self._topic_window_max_size = 10

        # ── 注册引擎 post-hooks 到 Brain ──
        self._register_engine_hooks()

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
                _engine._last_reply_depth.get(gid, 0) + 1 if now_ts - last_ts < 60 else 1
            )

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
            if not _result.clean_text:
                return
            gid = _req.group_id
            uid = _req.user_id
            persona_name = _engine.persona.name if _engine.persona else "assistant"
            _engine.basic_memory.add_entry(
                group_id=gid,
                user_id="assistant",
                role="assistant",
                content=_result.clean_text,
                speaker_name=persona_name,
            )
            try:
                _engine.semantic_memory.record_response_sent(
                    group_id=gid,
                    user_id=uid or "",
                    topic_hint=_result.clean_text[:100],
                    response_length=len(_result.clean_text),
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
        participants: list[Participant],
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
            # 回写图片描述到 basic_memory
            if intent.image_caption:
                recent = self.basic_memory.get_context(group_id, n=1)
                if recent:
                    last_entry = recent[0]
                    last_entry.content = f"【图片】【图片描述：{intent.image_caption}】"
                    if last_entry.multimodal_inputs:
                        for m in last_entry.multimodal_inputs:
                            if m.get("type") == "image":
                                m["caption"] = intent.image_caption
            return {
                "strategy": "silent",
                "reply": None,
                "emotion": emotion.to_dict() if emotion else {},
                "intent": intent.to_dict() if intent else {},
            }

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
        if intent.image_caption:
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
                    stripped = re.sub(
                        r"【动画表情：[^】]+】", "", original_content
                    ).strip()
                    sticker_tag = f"【动画表情：{intent.image_caption}】"
                    last_entry.content = (
                        f"{stripped} {sticker_tag}" if stripped else sticker_tag
                    )
                    # 也存入 multimodal_inputs 供 sticker learning 管道使用
                    for m in last_entry.multimodal_inputs:
                        if m.get("type") == "image":
                            m["caption"] = intent.image_caption
                else:
                    if self._is_pure_image_message(original_content):
                        last_entry.content = f"【图片】【图片描述：{intent.image_caption}】"
                    else:
                        last_entry.content = f"{original_content} 【图片描述：{intent.image_caption}】"
                    if last_entry.multimodal_inputs:
                        for m in last_entry.multimodal_inputs:
                            if m.get("type") == "image":
                                m["caption"] = intent.image_caption
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

        # Semantic: passive group norm learning from message content + intent
        social_intent = getattr(intent, "social_intent", None)
        self.semantic_memory.learn_from_message(
            group_id=group_id,
            content=content or "",
            social_intent=str(social_intent) if social_intent else "",
        )

        # 3. Decision
        decision = self._decision(
            intent, emotion, group_id, user_id, message.sender_type or "human"
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

    # ==================================================================
    # Persistence
    # ==================================================================

    def _persist_group_state(self, group_id: str) -> None:
        """Persist basic memory and timestamps for a single group in real-time."""
        entries = self.basic_memory.get_all(group_id)[-100:]
        self._state_store.save_working_memory(
            group_id,
            [
                {
                    "user_id": e.user_id,
                    "role": e.role,
                    "content": e.content,
                    "timestamp": e.timestamp,
                }
                for e in entries
            ],
        )
        self._state_store.save_group_timestamps(dict(self._group_last_message_at))

    def _persist_full_state(self) -> None:
        """Persist all runtime state to disk (used on graceful shutdown)."""
        working_memories: dict[str, list[dict[str, Any]]] = {}
        for group_id in self.basic_memory.list_groups():
            entries = self.basic_memory.get_all(group_id)[-100:]
            working_memories[group_id] = [
                {
                    "user_id": e.user_id,
                    "role": e.role,
                    "content": e.content,
                    "timestamp": e.timestamp,
                }
                for e in entries
            ]

        import dataclasses

        self._state_store.save_all(
            working_memories=working_memories,
            assistant_emotion=dataclasses.asdict(self.assistant_emotion),
            delayed_queue=[],
            group_timestamps=dict(self._group_last_message_at),
            token_usage_records=[r.to_dict() for r in self.token_usage_records],
            basic_memory=self.basic_memory.to_dict(),
            diary_state={
                "diarized_sources": {
                    gid: list(sids) for gid, sids in self.diary_manager._diarized_sources.items()
                }
            },
        )

        # Save proactive state
        self._save_proactive_state()

        # Save persona
        from sirius_pulse.core.persona_store import PersonaStore

        PersonaStore.save(self.work_path, self.persona)

    def save_state(self) -> None:
        """Persist all runtime state to disk."""
        self._persist_full_state()

    def load_state(self) -> None:
        """Restore runtime state from disk."""
        try:
            state = self._state_store.load_all()

            # Basic memory
            basic_mem_data = state.get("basic_memory")
            if basic_mem_data:
                try:
                    self.basic_memory = BasicMemoryManager.from_dict(basic_mem_data)
                except Exception as exc:
                    logger.warning("基础记忆恢复失败，使用空实例: %s", exc)
                    self.basic_memory = BasicMemoryManager(
                        hard_limit=self.config.get("basic_memory_hard_limit", 30),
                        context_window=self.config.get("basic_memory_context_window", 5),
                    )

            # Assistant emotion
            ae = state.get("assistant_emotion")
            if ae:
                for key, value in ae.items():
                    if hasattr(self.assistant_emotion, key):
                        setattr(self.assistant_emotion, key, value)

            # Group timestamps
            self._group_last_message_at = dict(state.get("group_timestamps", {}))

            # Reset timestamps to now so the proactive silence timer starts fresh
            # after engine restart; otherwise offline time would be mis-counted as
            # group silence.
            now_iso = datetime.now(timezone.utc).isoformat()
            for gid in list(self._group_last_message_at.keys()):
                self._group_last_message_at[gid] = now_iso

            # Diary state
            diary_state = state.get("diary_state")
            if diary_state:
                try:
                    sources = diary_state.get("diarized_sources", {})
                    self.diary_manager._diarized_sources = {
                        gid: set(sids) for gid, sids in sources.items()
                    }
                except Exception as exc:
                    logger.warning("日记状态恢复失败: %s", exc)

            # User manager (with cross-group global profiles)
            user_mgr_data = state.get("user_manager")
            if user_mgr_data:
                try:
                    self.user_manager = UserManager.from_dict(user_mgr_data)
                except Exception as exc:
                    logger.warning("用户管理器恢复失败，使用空实例: %s", exc)

            # Re-bind context assembler to restored basic_memory
            self.context_assembler = ContextAssembler(
                self.basic_memory,
                self.diary_manager._retriever,
            )

            # Token usage records
            from sirius_pulse.config import TokenUsageRecord

            for rec_data in state.get("token_usage_records", []):
                try:
                    self.token_usage_records.append(TokenUsageRecord.from_dict(rec_data))
                except Exception:
                    pass

            # Load persona
            from sirius_pulse.core.persona_store import PersonaStore

            loaded = PersonaStore.load(self.work_path)
            if loaded:
                self.persona = loaded
                logger.info("我的人设已经加载好了，我是 %s～", loaded.name)

            logger.info(
                "之前的记忆都找回来啦，一共 %d 个群的上下文我都记得。",
                len(self.basic_memory.list_groups()),
            )

            # Initialize sticker system
            self._init_sticker_system()
        except Exception as exc:
            logger.warning("状态恢复部分出错，继续尝试加载 proactive 状态: %s", exc)
        finally:
            # Proactive state must always be attempted regardless of other failures
            self._load_proactive_state()

    def _init_sticker_system(self) -> None:
        """扫描 stickers 文件夹，获取可用表情包名称列表。

        支持 `__` 分隔符命名：`喜欢__可爱.jpg`、`喜欢__生气.jpg`
        都属于"喜欢"表情包，AI 发送 [STICKERS: "喜欢"] 时从中随机选一张。
        """
        stickers_dir = Path(self.work_path) / "stickers"
        if not stickers_dir.is_dir():
            logger.info("表情包目录不存在，跳过初始化: %s", stickers_dir)
            self._sticker_names = []
            self.brain.sticker_names = []
            return

        image_extensions = {".gif", ".png", ".jpg", ".jpeg", ".webp", ".bmp"}
        names: set[str] = set()
        for f in stickers_dir.iterdir():
            if f.is_file() and f.suffix.lower() in image_extensions:
                stem = f.stem
                # 含 __ 的文件取前缀作为表情包名称（如 "喜欢__可爱.jpg" → "喜欢"）
                if "__" in stem:
                    names.add(stem.split("__", 1)[0])
                else:
                    names.add(stem)
        self._sticker_names = sorted(names)
        self.brain.sticker_names = self._sticker_names
        logger.info(
            "表情包系统初始化完成: 共 %d 个表情包名称，来自 %d 个文件",
            len(self._sticker_names),
            sum(1 for _ in stickers_dir.iterdir() if _.is_file() and _.suffix.lower() in image_extensions),
        )

    # ------------------------------------------------------------------
    # Brain 委托：_generate 已删除，调用方请直接用 self.brain.chat() 或
    # self.brain.generate_text()。外部模块（plugin/skill context）使用
    # engine.brain.generate_text(system_prompt, messages, group_id)。
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # 表情包系统：从模型回复中解析 [STICKERS: ...] 标签并发送
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_sticker_tags(text: str) -> tuple[str, list[str]]:
        """从回复文本中解析 [STICKERS: "name1", "name2"] 格式的标签。

        Returns:
            (清理后的文本, 选中的表情包名称列表)
        """
        import re

        # 匹配 [STICKERS: ...] 块，内部内容宽松抓取
        pattern = r'\[STICKERS:\s*(.+?)\s*\]'
        match = re.search(pattern, text)
        if not match:
            return text, []

        # 按逗号拆分，去除可选引号（支持 ASCII " ' 、中文 "" '' 「」）
        raw = match.group(1)
        names: list[str] = []
        for part in re.split(r'\s*,\s*', raw):
            part = part.strip()
            # 剥除首尾引号字符
            while part and part[0] in '\'"\u201c\u2018\u300c':
                part = part[1:]
            while part and part[-1] in '\'"\u201d\u2019\u300d':
                part = part[:-1]
            if part:
                names.append(part)

        chosen = names[:3]

        # 移除标签区域，清理多余空白
        prefix = text[: match.start()].rstrip()
        suffix = text[match.end():].lstrip()
        cleaned_text = f"{prefix} {suffix}".strip() if prefix and suffix else (prefix + suffix)
        return cleaned_text, chosen

    def _pick_sticker_file(self, names: list[str]) -> Path | None:
        """从模型选择的名称列表中随机选一个，再匹配对应的图片文件。

        模型选 1-3 个名称，本地从中随机选 1 个发送。
        匹配规则：
        - 精确匹配：`喜欢.jpg`
        - 包匹配：`喜欢__可爱.jpg`、`喜欢__生气.jpg`（`__` 前缀属于同一包）
        从所有匹配文件中随机选一个。
        """
        if not names:
            return None

        stickers_dir = Path(self.work_path) / "stickers"
        if not stickers_dir.is_dir():
            return None

        image_extensions = {".gif", ".png", ".jpg", ".jpeg", ".webp", ".bmp"}
        import random

        # 从模型选的名称中随机挑一个
        chosen_name = random.choice(names[:3])

        candidates: list[Path] = []

        # 1. 精确匹配：{name}.{ext}
        for ext in image_extensions:
            candidate = stickers_dir / f"{chosen_name}{ext}"
            if candidate.is_file():
                candidates.append(candidate)

        # 2. 包匹配：{name}__*.{ext}（支持同包多文件随机选一）
        for f in stickers_dir.iterdir():
            if f.is_file() and f.suffix.lower() in image_extensions:
                if f.stem.startswith(f"{chosen_name}__"):
                    candidates.append(f)

        return random.choice(candidates) if candidates else None

    async def _send_stickers_by_names(
        self,
        group_id: str,
        names: list[str],
    ) -> dict[str, Any]:
        """从模型选中的名称中随机挑一个表情包发送（sub_type=1）。"""
        # 概率跳过检查
        import random
        skip_rate = self.config.get("sticker_skip_probability", 0.33)
        if random.random() < skip_rate:
            logger.debug("表情包发送被概率跳过 (skip_rate=%.2f)", skip_rate)
            return {"success": False, "error": "概率跳过"}

        fp = self._pick_sticker_file(names)
        if fp is None:
            return {"success": False, "error": "没有匹配的表情包文件"}

        adapter = getattr(self, "_adapter", None)
        if adapter is None:
            return {"success": False, "error": "没有可用的 adapter"}

        try:
            msg = [{"type": "image", "data": {"file": str(fp), "sub_type": "1"}}]
            if group_id.startswith("private_"):
                await adapter.send_private_msg(group_id.replace("private_", ""), msg)
            else:
                await adapter.send_group_msg(group_id, msg)

            logger.info("表情包已发送: %s -> %s", fp.name, group_id)
            return {
                "success": True,
                "sticker_name": fp.stem,
                "file_path": str(fp),
            }
        except Exception as exc:
            logger.warning("表情包发送失败: %s %s", fp.name, exc)
            return {"success": False, "error": str(exc), "file_path": str(fp)}

    # ------------------------------------------------------------------
    # Proactive state persistence
    # ------------------------------------------------------------------

    def _save_proactive_state(self) -> None:
        """Persist proactive enabled/disabled groups and last trigger timestamps."""
        path = Path(self.work_path) / "engine_state" / "proactive_state.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        data = {
            "enabled_groups": sorted(self._proactive_enabled_groups),
            "disabled_groups": sorted(self._proactive_disabled_groups),
            "last_proactive_at": dict(self._last_proactive_at),
        }
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(path)

    def _load_proactive_state(self) -> None:
        """Restore proactive state from disk."""
        path = Path(self.work_path) / "engine_state" / "proactive_state.json"
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                logger.warning("Proactive state file is not a dict, skipping")
                return
            # Force str keys to avoid int/str mismatch
            self._proactive_enabled_groups = {str(g) for g in data.get("enabled_groups", [])}
            self._proactive_disabled_groups = {str(g) for g in data.get("disabled_groups", [])}
            self._last_proactive_at = {
                str(k): str(v) for k, v in dict(data.get("last_proactive_at", {})).items()
            }
            # Sync into ProactiveTrigger
            self.proactive_trigger._last_proactive = dict(self._last_proactive_at)
            logger.info(
                "Proactive state loaded: %d enabled, %d disabled groups",
                len(self._proactive_enabled_groups),
                len(self._proactive_disabled_groups),
            )
        except Exception as exc:
            logger.warning("Proactive state 加载失败: %s", exc)

    def set_proactive_enabled(self, group_id: str, enabled: bool) -> None:
        """Enable or disable proactive triggers for a specific group."""
        gid = str(group_id)
        if enabled:
            self._proactive_enabled_groups.add(gid)
            self._proactive_disabled_groups.discard(gid)
        else:
            self._proactive_enabled_groups.discard(gid)
            self._proactive_disabled_groups.add(gid)
        self._save_proactive_state()

    def is_proactive_enabled(self, group_id: str) -> bool:
        """Check if proactive triggers are enabled for a group.

        Priority:
        1. If group is in disabled list -> False
        2. If enabled_groups is not empty and group not in it -> False
        3. Otherwise -> True
        """
        gid = str(group_id)
        if gid in self._proactive_disabled_groups:
            return False
        if self._proactive_enabled_groups:
            return gid in self._proactive_enabled_groups
        return True
