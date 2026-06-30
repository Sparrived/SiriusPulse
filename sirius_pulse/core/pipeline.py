"""Pipeline stages for EmotionalGroupChatEngine.

Perception → Signal → PreFilter → Generate → BackgroundUpdate

规则计算层产生信号，粗筛决定是否调用主模型，主模型用 stop 工具决定回不回。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from sirius_pulse.core.identity_resolver import IdentityContext
from sirius_pulse.core.cognition import extract_keywords
from sirius_pulse.core.participation import ParticipationPolicy, get_reply_time_coefficient
from sirius_pulse.models.emotion import EmotionState
from sirius_pulse.models.models import Message, UnifiedUser
from sirius_pulse.models.response_strategy import PersonaProfilePromptContext, ResponseStrategy, StrategyDecision
from sirius_pulse.models.signal import SignalAnalysis

if TYPE_CHECKING:
    from sirius_pulse.core.engine_core import _EmotionalGroupChatEngineBase

logger = logging.getLogger(__name__)


class Pipeline:
    """提供 pipeline 阶段方法的组件类。

    通过引擎实例访问属性，实现组合模式。
    """

    def __init__(self, engine: _EmotionalGroupChatEngineBase) -> None:
        self._engine = engine
        self._participation_policy = ParticipationPolicy()

    def _get_recent_speakers(self, group_id: str, n: int = 6) -> list[str]:
        """获取最近发言的用户 ID 列表（用于身份解析的上下文推断）。

        Args:
            group_id: 群组 ID
            n: 获取最近 n 条消息的发言者

        Returns:
            用户 ID 列表（按时间倒序）
        """
        engine = self._engine
        recent_messages = engine._helpers.get_recent_messages(group_id, n=n)
        speakers = []
        seen = set()
        for msg in recent_messages:
            user_id = msg.get("user_id")
            if user_id and user_id not in seen:
                speakers.append(user_id)
                seen.add(user_id)
        return speakers

    # ==================================================================
    # Pipeline stages
    # ==================================================================

    def perception(
        self,
        group_id: str,
        message: Message,
        participants: list[UnifiedUser],
    ) -> str:
        """Perception layer: normalize, register participants, update transcript."""
        engine = self._engine

        # 获取最近发言者列表（用于上下文推断）
        recent_speakers = self._get_recent_speakers(group_id)

        # Register participants via identity resolver and user manager
        for p in participants:
            ctx = IdentityContext(
                speaker_name=p.name,
                user_id=p.user_id,
                platform_uid=p.identities.get(message.channel) if message.channel else None,
                platform=message.channel,
                is_developer=p.is_developer,
            )
            engine.identity_resolver.resolve(ctx, engine.user_manager, group_id)

        # Resolve current sender
        sender_ctx = IdentityContext(
            speaker_name=message.speaker or "unknown",
            user_id=None,
            platform_uid=message.channel_user_id,
            platform=message.channel,
            is_developer=False,
        )

        # 使用增强版解析（UnifiedUserManager 内置别名索引）
        resolution = engine.identity_resolver.resolve_with_alias(
            sender_ctx,
            engine.user_manager,
            group_id,
            recent_speakers=recent_speakers,
            profile_manager=getattr(engine, "profile_manager", None),
        )

        # 记录解析结果（低置信度时发出警告）
        if resolution.confidence < 0.5 and resolution.source != "unresolved":
            logger.debug(
                "身份解析低置信度: speaker=%s user_id=%s confidence=%.2f source=%s",
                message.speaker,
                resolution.user_id,
                resolution.confidence,
                resolution.source,
            )

        resolved_user_id = resolution.user_id
        sender_user = engine.user_manager.get_user(resolved_user_id, group_id)
        resolved_speaker_name = sender_user.name if sender_user else message.speaker or "unknown"

        # 构建用户消息的标签
        entry_tags: list[dict[str, str]] = []
        mm_inputs = (
            [dict(item) for item in message.multimodal_inputs] if message.multimodal_inputs else []
        )
        if mm_inputs:
            # 区分表情包和普通图片
            sticker_count = sum(1 for m in mm_inputs if m.get("sub_type") == "1")
            image_count = len(mm_inputs) - sticker_count
            if sticker_count > 0:
                entry_tags.append({"type": "sticker", "label": f"动画表情 ×{sticker_count}"})
            if image_count > 0:
                entry_tags.append({"type": "image", "label": f"图片 ×{image_count}"})

        # Add to basic memory and archive to disk
        entry = engine.basic_memory.add_entry(
            group_id=group_id,
            user_id=resolved_user_id,
            speaker_name=resolved_speaker_name,
            role="human",
            content=message.content,
            channel_user_id=message.channel_user_id or "",
            platform_message_id=message.message_id or "",
            multimodal_inputs=mm_inputs if mm_inputs else None,
            tags=entry_tags if entry_tags else None,
        )
        engine.basic_store.append(entry)

        # Update group last message time
        from sirius_pulse.core.utils import now_iso

        engine._group_last_message_at[group_id] = now_iso()
        engine._persist_group_state(group_id)
        return resolved_user_id

    # ------------------------------------------------------------------
    # 信号计算（纯规则，无 LLM）
    # ------------------------------------------------------------------

    def compute_signal(
        self,
        content: str,
        user_id: str,
        group_id: str,
        *,
        sender_type: str = "human",
        caller_is_developer: bool = False,
    ) -> SignalAnalysis:
        """纯规则信号计算。替代原 cognition() + decision()。

        Returns:
            SignalAnalysis 包含所有规则计算结果。
        """
        engine = self._engine

        # 上下文消息
        recent = engine._helpers.get_recent_messages(group_id, n=6)
        if recent and recent[-1].get("content") == content:
            context_messages = recent[:-1]
        else:
            context_messages = recent

        # 节奏分析
        recent_msgs = engine._helpers.get_recent_messages(group_id, n=10)
        rhythm = engine.rhythm_analyzer.analyze(group_id, recent_msgs)

        # 别名信息
        group_aliases: dict[str, str] | None = None
        try:
            group_aliases = engine.profile_manager.get_aliases_for_group(group_id) or None
        except Exception:
            pass

        # 纯规则信号计算
        signal = engine.cognition_analyzer.compute_signal(
            content,
            user_id,
            group_id,
            context_messages,
            sender_type=sender_type,
            caller_is_developer=caller_is_developer,
            group_aliases=group_aliases,
            rhythm=rhythm,
        )

        # 话题窗口维护
        try:
            msg_kw = extract_keywords(content)
            window = engine._topic_window.setdefault(group_id or "", [])
            window.append(msg_kw)
            max_size = getattr(engine, "_topic_window_max_size", 10)
            if len(window) > max_size:
                window[:] = window[-max_size:]
        except Exception:
            logger.warning("裁剪话题窗口失败", exc_info=True)

        # 持久化认知事件
        try:
            emotion = signal.emotion
            engine.cognition_store.add(
                group_id=group_id or "",
                user_id=user_id or "",
                valence=getattr(emotion, "valence", 0.0) if emotion else 0.0,
                arousal=getattr(emotion, "arousal", 0.3) if emotion else 0.3,
                basic_emotion=getattr(getattr(emotion, "basic_emotion", None), "name", "")
                if emotion and getattr(emotion, "basic_emotion", None)
                else "",
                intensity=getattr(emotion, "intensity", 0.5) if emotion else 0.5,
                social_intent=signal.social_intent,
                urgency_score=signal.urgency_score,
                relevance_score=signal.relevance_score,
                confidence=0.8,
                directed_score=signal.directed_score,
                sarcasm_score=signal.sarcasm_score,
                entitlement_score=signal.entitlement_score,
                turn_gap_readiness=signal.turn_gap_readiness,
                directed_signals={},
            )
        except Exception:
            pass

        return signal

    # ------------------------------------------------------------------
    # 粗筛（硬性守卫 + 阈值）
    # ------------------------------------------------------------------

    def pre_filter(
        self,
        signal: SignalAnalysis,
        content: str,
        user_id: str,
        group_id: str,
        sender_type: str = "human",
    ) -> str:
        """粗筛：决定是否调用主模型。

        Returns:
            "pass" — 进入主模型生成
            "reject" — 跳过，不调用 LLM
        """
        engine = self._engine

        # 1. 消息前缀过滤
        prefixes = engine.config.get("message_prefixes", [])
        if prefixes and content:
            text_stripped = content.lstrip()
            if any(text_stripped.startswith(p) for p in prefixes if p):
                logger.debug("消息以配置前缀开头，跳过回复流程: %s", text_stripped[:50])
                return "reject"

        # 2. 极短消息过滤
        if len(content or "") <= 2 and not __import__("re").search(r"[一-鿿]", content or ""):
            if not signal.is_mentioned:
                return "reject"

        # 3. 过热 + 爆发 + 未指向 → 跳过
        if (
            signal.heat_level == "overheated"
            and signal.burst_detected
            and not signal.is_mentioned
        ):
            engine._log_inner_thought("群聊太热闹了，我先不插话了...")
            return "reject"

        # 4. @了别人且没@自己 → 跳过（由 compute_signal 的 other_mention 保护）
        # directed_score 已经处理了这个逻辑（other_mention >= 0.5 时 directed_score 被压低）

        # 5. 规则参与评分
        now = datetime.now(timezone.utc).timestamp()
        last_reply = engine._last_reply_at.get(group_id, 0)
        seconds_since_reply = now - last_reply
        cooldown = engine.config.get(
            "reply_cooldown_seconds", engine.expressiveness.cooldown_seconds
        )

        sensitivity = engine.config.get("sensitivity", 0.5)
        directed_gate = engine.expressiveness.directed_threshold + (1.0 - sensitivity) * 0.15
        reply_time_coefficient = 1.0
        if engine.config.get("reply_time_curve_enabled", False):
            reply_time_coefficient = get_reply_time_coefficient(
                engine.config.get("reply_time_curve_points", []),
                datetime.now().time(),
            )

        # 亲和度调节
        user_profile = engine.semantic_memory.get_user_profile(group_id, user_id)
        affinity = 0.0
        if user_profile:
            affinity = getattr(user_profile, "affinity_score", 0.0)

        # 私聊兜底：1v1 不完全沉默
        is_private = group_id.startswith("private_")
        decision = self._participation_policy.evaluate(
            signal=signal,
            content=content,
            is_private=is_private,
            sender_type=sender_type,
            seconds_since_reply=seconds_since_reply,
            cooldown_seconds=cooldown,
            directed_gate=directed_gate,
            entitlement_threshold=engine.expressiveness.entitlement_threshold,
            reply_frequency=engine.persona.reply_frequency,
            affinity_score=affinity,
            reply_time_coefficient=reply_time_coefficient,
        )
        signal.participation = decision.to_dict()

        if signal.entitlement_score < engine.expressiveness.entitlement_threshold:
            engine._log_inner_thought("这个话题我好像不太擅长...先谨慎一点吧")

        if not decision.should_reply:
            logger.info(
                "[参与] reject group=%s user=%s reason=%s score=%.3f threshold=%.3f "
                "address=%.3f need=%.3f social=%.3f fit=%.3f suppress=%.3f "
                "directed=%.3f gate=%.3f urgency=%.1f heat=%s pace=%s",
                group_id,
                user_id,
                decision.reason,
                decision.score,
                decision.threshold,
                decision.addressing_score,
                decision.reply_need_score,
                decision.social_opportunity_score,
                decision.conversation_fit_score,
                decision.suppression_score,
                signal.directed_score,
                directed_gate,
                signal.urgency_score,
                signal.heat_level,
                signal.pace,
            )
            return "reject"

        # 结构化日志
        logger.info(
            "[参与] pass group=%s user=%s strategy=%s reason=%s score=%.3f threshold=%.3f "
            "delay=%.1f address=%.3f need=%.3f social=%.3f fit=%.3f suppress=%.3f "
            "directed=%.3f gate=%.3f mentioned=%s urgency=%.1f entitlement=%.3f heat=%s pace=%s",
            group_id,
            user_id,
            decision.strategy.value,
            decision.reason,
            decision.score,
            decision.threshold,
            decision.delay_seconds,
            decision.addressing_score,
            decision.reply_need_score,
            decision.social_opportunity_score,
            decision.conversation_fit_score,
            decision.suppression_score,
            signal.directed_score,
            directed_gate,
            signal.is_mentioned,
            signal.urgency_score,
            signal.entitlement_score,
            signal.heat_level,
            signal.pace,
        )

        return "pass"

    # ------------------------------------------------------------------
    # 统一生成（所有通过粗筛的消息走这条路）
    # ------------------------------------------------------------------

    async def generate(
        self,
        signal: SignalAnalysis,
        message: Message,
        group_id: str,
        user_id: str,
    ) -> dict[str, Any]:
        """统一生成路径：将消息入队延迟队列，由主模型决定回复或 stop。

        所有通过 pre_filter 的消息都走这条路。
        """
        engine = self._engine

        # 收集人物传记
        persona_profile_context = self._collect_persona_profile_section(
            group_id, user_id, message.content or "",
        )

        # 构造 StrategyDecision 供 delayed_queue 使用
        participation = signal.participation or {}
        strategy_value = participation.get("strategy")
        try:
            strategy = ResponseStrategy(strategy_value) if strategy_value else ResponseStrategy.SILENT
        except ValueError:
            strategy = ResponseStrategy.SILENT
        if strategy == ResponseStrategy.SILENT:
            if signal.is_mentioned or signal.urgency_score >= 80:
                delay_seconds = 0.0
                strategy = ResponseStrategy.IMMEDIATE
            elif signal.urgency_score >= 50:
                delay_seconds = 15.0
                strategy = ResponseStrategy.DELAYED
            else:
                delay_seconds = 30.0
                strategy = ResponseStrategy.DELAYED
        else:
            delay_seconds = float(participation.get("delay_seconds", 30.0))

        is_private = group_id.startswith("private_")
        reason = str(participation.get("reason") or "signal_passed")
        hard_immediate = bool(
            strategy == ResponseStrategy.IMMEDIATE
            and (signal.is_mentioned or is_private or signal.urgency_score >= 85)
        )
        freshness_ttl = self._freshness_ttl_for_signal(
            signal,
            reason,
            is_private=is_private,
        )

        decision = StrategyDecision(
            strategy=strategy,
            score=float(participation.get("score", signal.urgency_score / 100.0)),
            threshold=float(participation.get("threshold", 0.5)),
            urgency=signal.urgency_score,
            relevance=signal.relevance_score,
            reason=reason,
            context={
                "participation": participation,
                "hard_immediate": hard_immediate,
                "freshness_ttl_seconds": freshness_ttl,
            },
        )
        decision.estimated_delay_seconds = delay_seconds

        # 入队延迟队列
        engine.delayed_queue.enqueue(
            group_id=group_id,
            user_id=user_id,
            message_content=message.content,
            strategy_decision=decision,
            candidate_memories=[],
            channel=message.channel,
            channel_user_id=message.channel_user_id,
            multimodal_inputs=message.multimodal_inputs,
            adapter_type=message.adapter_type,
            heat_level=signal.heat_level,
            pace=signal.pace,
            speaker_name=message.speaker or "",
            platform_message_id=message.message_id or "",
            persona_profile_context=persona_profile_context,
            signal_prompt=signal.to_prompt_text(),
        )
        engine._persist_group_state(group_id)

        # 更新 assistant emotion 和语义记忆
        emotion = signal.emotion
        if emotion:
            engine.assistant_emotion.update_from_interaction(emotion, user_id)

        now_iso = datetime.now(timezone.utc).isoformat()
        if user_id:
            engine.semantic_memory.settle_engagement(
                group_id=group_id,
                user_id=user_id,
                directed_score=signal.directed_score,
                timestamp=now_iso,
            )
            engine.semantic_memory.record_interaction(
                group_id=group_id, user_id=user_id, timestamp=now_iso,
            )

        return {
            "strategy": strategy.value,
            "reply": None,
            "emotion": {},
            "signal": signal.to_dict(),
        }

    @staticmethod
    def _freshness_ttl_for_signal(
        signal: SignalAnalysis,
        reason: str,
        *,
        is_private: bool,
    ) -> float:
        if is_private or signal.is_mentioned:
            return 60.0
        if signal.social_intent == "help_seeking":
            return 40.0
        if reason == "reply_needed":
            return 30.0
        if reason == "natural_join":
            return 14.0
        if signal.social_intent == "emotional":
            return 24.0
        return 18.0

    def _queue_response(
        self,
        *,
        decision: StrategyDecision,
        message: Message,
        intent: Any,
        emotion: EmotionState,
        memories: list[dict[str, Any]],
        group_id: str,
        user_id: str,
        rhythm: Any,
        persona_profile_context: PersonaProfilePromptContext | None = None,
    ) -> dict[str, Any]:
        engine = self._engine
        bio_ctx = persona_profile_context or PersonaProfilePromptContext()
        engine.delayed_queue.enqueue(
            group_id=group_id,
            user_id=user_id,
            message_content=message.content,
            strategy_decision=decision,
            candidate_memories=[m.get("content", "") for m in memories],
            channel=message.channel,
            channel_user_id=message.channel_user_id,
            multimodal_inputs=message.multimodal_inputs,
            adapter_type=message.adapter_type,
            heat_level=rhythm.heat_level,
            pace=rhythm.pace,
            speaker_name=message.speaker or "",
            platform_message_id=message.message_id or "",
            persona_profile_context=bio_ctx,
        )
        engine._persist_group_state(group_id)
        result = {
            "strategy": decision.strategy.value,
            "reply": None,
            "emotion": {},
            "intent": {},
        }
        if decision.strategy == ResponseStrategy.IMMEDIATE:
            result["thought"] = ""
            result["partial_replies"] = []
        return result

    def _collect_persona_profile_section(
        self,
        group_id: str,
        user_id: str,
        message_content: str,
    ) -> PersonaProfilePromptContext:
        """收集人物画像快照，供延迟回复链路持久化使用。"""
        engine = self._engine
        profile_manager = getattr(engine, "profile_manager", None)
        if profile_manager is None:
            return PersonaProfilePromptContext()

        speaker_profile = (
            profile_manager.get_profile(group_id, user_id, create=False) if user_id else None
        )

        mentioned: dict[str, float] = {}
        aliases = profile_manager.list_alias_entries(group_id)
        if message_content:
            for alias in aliases.keys():
                if len(alias) < 2 or alias not in message_content.lower():
                    continue
                uid, conf, _ = profile_manager.resolve_alias(alias, group_id=group_id)
                if uid and uid != user_id:
                    mentioned[uid] = max(mentioned.get(uid, 0.0), conf)

        mentioned_profiles = [
            profile
            for uid in mentioned.keys()
            if (profile := profile_manager.get_profile(group_id, uid, create=False)) is not None
        ]

        return PersonaProfilePromptContext(
            speaker_card=speaker_profile,
            mentioned_cards=mentioned_profiles,
            confidence=dict(mentioned),
        )

    def background_update(
        self,
        group_id: str,
        message: Message,
        emotion: EmotionState | None,
        intent: Any | None,
        user_id: str,
    ) -> None:
        """Background updates after main pipeline.

        emotion / signal 可为 None（管线短路合并场景），
        此时跳过情感相关更新，仅处理用户信息持久化。
        """
        engine = self._engine
        # Update group sentiment cache for emotion island detection
        if emotion is not None:
            engine.cognition_analyzer.update_group_sentiment(group_id, emotion)
            # Update assistant emotion based on interaction
            engine.assistant_emotion.update_from_interaction(emotion, user_id)

        # Save display name (QQ name + group nickname) and accumulate content
        speaker_name = getattr(message, "speaker", "") or ""
        nickname = getattr(message, "nickname", "") or ""
        if user_id:
            if nickname:
                display_name = nickname
                if speaker_name and speaker_name != nickname:
                    display_name = f"{nickname}({speaker_name})"
                engine.semantic_memory.set_user_profile_fields(group_id, user_id, name=display_name)
            elif speaker_name:
                engine.semantic_memory.set_user_profile_fields(group_id, user_id, name=speaker_name)
