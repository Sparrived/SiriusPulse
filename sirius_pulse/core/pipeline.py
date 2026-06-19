"""Pipeline stages for EmotionalGroupChatEngine.

Perception → Cognition → Decision → Execution → BackgroundUpdate

重构为组合模式：Pipeline 类通过引擎实例访问属性，
基类通过委托方法保持 API 兼容。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from sirius_pulse.core.identity_resolver import IdentityContext
from sirius_pulse.core.cognition import extract_keywords
from sirius_pulse.memory.semantic.models import AtmosphereSnapshot
from sirius_pulse.models.emotion import EmotionState
from sirius_pulse.models.intent_v3 import IntentAnalysisV3, SocialIntent
from sirius_pulse.models.models import Message, UnifiedUser
from sirius_pulse.models.response_strategy import ResponseStrategy, StrategyDecision

if TYPE_CHECKING:
    from sirius_pulse.core.engine_core import _EmotionalGroupChatEngineBase

logger = logging.getLogger(__name__)


class Pipeline:
    """提供 pipeline 阶段方法的组件类。

    通过引擎实例访问属性，实现组合模式。
    """

    def __init__(self, engine: _EmotionalGroupChatEngineBase) -> None:
        self._engine = engine

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

    async def cognition(
        self,
        content: str,
        user_id: str,
        group_id: str,
        *,
        sender_type: str = "human",
        multimodal_inputs: list[dict[str, str]] | None = None,
        caller_is_developer: bool = False,
    ) -> tuple[IntentAnalysisV3, EmotionState, list[dict[str, Any]], Any]:
        """Cognitive layer: unified emotion + intent + empathy + memory retrieval."""
        engine = self._engine
        # Build context from recent working memory (exclude current message)
        recent = engine._helpers.get_recent_messages(group_id, n=6)
        if recent and recent[-1].get("content") == content:
            context_messages = recent[:-1]
        else:
            context_messages = recent

        # Joint cognition (emotion + intent + empathy in one pass)

        # 获取别名信息，帮助 LLM 区分 AI 和其他用户的别称
        group_aliases: dict[str, str] | None = None
        try:
            group_aliases = engine.user_manager.get_aliases_for_group(group_id) or None
        except Exception:
            pass

        emotion, intent, empathy = await engine.cognition_analyzer.analyze(
            content,
            user_id,
            group_id,
            context_messages,
            sender_type=sender_type,
            multimodal_inputs=multimodal_inputs,
            caller_is_developer=caller_is_developer,
            group_aliases=group_aliases,
        )

        # Rhythm context for persistence
        try:
            rhythm = engine.rhythm_analyzer.analyze(group_id or "", recent)
            turn_gap_readiness = getattr(rhythm, "turn_gap_readiness", 0.5)
        except Exception:
            logger.warning("计算 turn_gap_readiness 失败，使用默认值 0.5", exc_info=True)
            turn_gap_readiness = 0.5

        # Build directed_signals JSON from 12-dimension scores
        directed_signals = {
            "mention_score": getattr(intent, "mention_score", 0.0),
            "reference_score": getattr(intent, "reference_score", 0.0),
            "name_match_score": getattr(intent, "name_match_score", 0.0),
            "second_person_score": getattr(intent, "second_person_score", 0.0),
            "question_score": getattr(intent, "question_score", 0.0),
            "imperative_score": getattr(intent, "imperative_score", 0.0),
            "topic_relevance_score": getattr(intent, "topic_relevance_score", 0.0),
            "emotional_disclosure_score": getattr(intent, "emotional_disclosure_score", 0.0),
            "attention_seeking_score": getattr(intent, "attention_seeking_score", 0.0),
            "recency_score": getattr(intent, "recency_score", 0.0),
            "turn_taking_score": getattr(intent, "turn_taking_score", 0.0),
        }

        # Persist cognition event for emotional timeline analysis
        try:
            engine.cognition_store.add(
                group_id=group_id or "",
                user_id=user_id or "",
                valence=getattr(emotion, "valence", 0.0),
                arousal=getattr(emotion, "arousal", 0.3),
                basic_emotion=getattr(getattr(emotion, "basic_emotion", None), "name", "")
                if getattr(emotion, "basic_emotion", None)
                else "",
                intensity=getattr(emotion, "intensity", 0.5),
                social_intent=getattr(getattr(intent, "social_intent", None), "value", "")
                if getattr(intent, "social_intent", None)
                else getattr(intent, "intent_type", ""),
                urgency_score=getattr(intent, "urgency_score", 0.0),
                relevance_score=getattr(intent, "relevance_score", 0.5),
                confidence=getattr(intent, "confidence", 0.8),
                directed_score=getattr(intent, "directed_score", 0.0),
                sarcasm_score=getattr(intent, "sarcasm_score", 0.0),
                entitlement_score=getattr(intent, "entitlement_score", 0.0),
                turn_gap_readiness=turn_gap_readiness,
                directed_signals=directed_signals,
            )
        except Exception:
            pass

        # Enhance topic relevance with semantic memory
        intent.topic_relevance_score = engine._helpers.enhance_topic_relevance(
            intent.topic_relevance_score, content, group_id, user_id
        )

        # v1.3+: 维护短期话题窗口（滑动窗口，保留最近 N 条消息的关键词快照）
        try:
            msg_kw = extract_keywords(content)
            window = engine._topic_window.setdefault(group_id or "", [])
            window.append(msg_kw)
            # 只保留最近的 max_size 条
            max_size = getattr(engine, "_topic_window_max_size", 10)
            if len(window) > max_size:
                window[:] = window[-max_size:]
        except Exception:
            logger.warning("裁剪话题窗口失败", exc_info=True)
            pass

        # Memory retrieval now happens in execution via ContextAssembler
        memories: list[dict[str, Any]] = []

        return intent, emotion, memories, empathy

    def decision(
        self,
        intent: IntentAnalysisV3,
        emotion: EmotionState,
        group_id: str,
        user_id: str,
        sender_type: str = "human",
        content: str = "",
    ) -> StrategyDecision:
        """Decision layer: strategy selection with threshold and rhythm."""
        engine = self._engine

        # === 消息前缀过滤 ===
        prefixes = engine.config.get("message_prefixes", [])
        if prefixes and content:
            text_stripped = content.lstrip()
            if any(text_stripped.startswith(p) for p in prefixes if p):
                logger.debug("消息以配置前缀开头，跳过回复流程: %s", text_stripped[:50])
                return StrategyDecision(
                    strategy=ResponseStrategy.SILENT,
                    score=0.0,
                    threshold=1.0,
                    urgency=0.0,
                    relevance=0.0,
                    reason="message_prefix_filtered",
                )

        # Rhythm context
        recent_msgs = engine._helpers.get_recent_messages(group_id, n=10)
        rhythm = engine.rhythm_analyzer.analyze(group_id, recent_msgs)

        # Compute dynamic threshold via ThresholdEngine
        user_profile = engine.semantic_memory.get_user_profile(group_id, user_id)

        # Message rate (per minute) from recent messages
        msg_rate = engine._helpers.message_rate_per_minute(recent_msgs)

        threshold = engine.threshold_engine.compute(
            sensitivity=engine.config.get("sensitivity", 0.5),
            heat_level=rhythm.heat_level,
            messages_per_minute=msg_rate,
            user_profile=user_profile,
            sender_type=sender_type,
        )

        # Persona reply frequency bias
        freq = engine.persona.reply_frequency
        if freq == "high":
            threshold *= 0.8
        elif freq == "low":
            threshold *= 1.3
        elif freq == "selective":
            # Only reply when strongly directed (>=threshold) or high urgency
            if (
                intent.directed_score < engine.expressiveness.directed_threshold
                and intent.urgency_score < 70
            ):
                threshold *= 2.0

        # Entitlement suppression: if AI is not qualified for this topic, raise threshold
        if intent.entitlement_score < engine.expressiveness.entitlement_threshold:
            threshold *= 1.5
            engine._log_inner_thought("这个话题我好像不太擅长...先谨慎一点吧")

        # 传记亲和力调节：仅当 LLM 曾输出过传记（last_updated_at 非空）时才生效
        biography_card = None
        affinity = 0.0
        # 使用 UnifiedUserManager 获取用户信息（包含亲和力分数）
        user_info = engine.user_manager.get_user(user_id)
        if user_info is not None and user_info.last_updated_at:
            # 用 EMA 平滑后的 affinity_score，不完全信任单次 LLM 输出
            affinity = user_info.affinity_score
            if affinity > 0.3:
                factor = 1.0 - min(0.25, affinity * 0.15)
                threshold *= factor
            elif affinity < -0.3:
                factor = 1.0 + min(0.40, abs(affinity) * 0.25)
                threshold *= factor
            if abs(affinity) > 0.3:
                engine._log_inner_thought(f"对ta的认知是affinity={affinity:.2f}，响应门槛调整了")

        intent.threshold = threshold
        intent.activity_factor = engine.threshold_engine._activity_factor(
            rhythm.heat_level, msg_rate
        )
        intent.time_factor = engine.threshold_engine._time_factor(None)
        if user_profile:
            intent.engagement_factor = engine.threshold_engine._engagement_factor(user_profile)

        sensitivity = engine.config.get("sensitivity", 0.5)
        directed_gate = engine.expressiveness.directed_threshold + (1.0 - sensitivity) * 0.15
        is_mentioned = intent.directed_score >= directed_gate

        decision_result = engine.strategy_engine.decide(
            intent,
            is_mentioned=is_mentioned,
            weak_directed_threshold=engine.expressiveness.weak_directed_threshold,
            heat_level=rhythm.heat_level,
            sender_type=sender_type,
        )

        # Reply cooldown suppression: delayed responses are throttled,
        # but immediate responses (e.g. direct mentions) bypass cooldown.
        now = datetime.now(timezone.utc).timestamp()
        last_reply = engine._last_reply_at.get(group_id, 0)
        seconds_since_reply = now - last_reply
        cooldown = engine.config.get(
            "reply_cooldown_seconds", engine.expressiveness.cooldown_seconds
        )
        if seconds_since_reply < cooldown and decision_result.strategy == ResponseStrategy.DELAYED:
            decision_result = StrategyDecision(
                strategy=ResponseStrategy.SILENT,
                score=0.0,
                threshold=decision_result.threshold,
                urgency=decision_result.urgency,
                relevance=decision_result.relevance,
                reason=f"cooldown_{int(seconds_since_reply)}s",
            )
            engine._log_inner_thought("群里正聊得火热呢，我刚回完不久，先闭嘴看看...")

        # Private-chat floor: never stay completely silent in 1-on-1
        if group_id.startswith("private_") and decision_result.strategy == ResponseStrategy.SILENT:
            decision_result = StrategyDecision(
                strategy=ResponseStrategy.DELAYED,
                score=decision_result.score,
                threshold=decision_result.threshold,
                urgency=max(decision_result.urgency, 25.0),
                relevance=max(decision_result.relevance, 0.5),
                reason=f"private_chat_floor:{decision_result.reason}",
            )

        # 内心活动：决策后的思考
        engine._log_decision_thought(intent, decision_result)

        # 结构化日志：记录关键决策参数到后台
        logger.info(
            "[决策参数] group=%s user=%s strategy=%s score=%.3f threshold=%.3f "
            "directed_score=%.3f directed_gate=%.3f directed=%s urgency=%.1f "
            "entitlement=%.3f sarcasm=%.3f affinity=%.2f "
            "heat_level=%s msg_rate=%.2f cooldown=%.1fs since_reply=%.1fs "
            "expressiveness=%.2f sensitivity=%.2f reason=%s",
            group_id,
            user_id,
            decision_result.strategy.value
            if hasattr(decision_result.strategy, "value")
            else str(decision_result.strategy),
            decision_result.score,
            decision_result.threshold,
            intent.directed_score,
            directed_gate,
            intent.directed_at_current_ai,
            intent.urgency_score,
            intent.entitlement_score,
            intent.sarcasm_score,
            affinity,
            rhythm.heat_level,
            msg_rate,
            cooldown,
            seconds_since_reply,
            engine.expressiveness.expressiveness if engine.expressiveness else 0.5,
            engine.config.get("sensitivity", 0.5),
            getattr(decision_result, "reason", ""),
        )

        # 持久化决策事件到 cognition_events.db（供 WebUI 分析）
        try:
            strategy_val = (
                decision_result.strategy.value
                if hasattr(decision_result.strategy, "value")
                else str(decision_result.strategy)
            )
            engine.cognition_store.add_decision(
                group_id=group_id or "",
                user_id=user_id or "",
                strategy=strategy_val,
                score=decision_result.score,
                threshold=decision_result.threshold,
                reason=getattr(decision_result, "reason", ""),
                directed_score=getattr(intent, "directed_score", 0.0),
                urgency=getattr(intent, "urgency_score", 0.0),
                entitlement=getattr(intent, "entitlement_score", 0.0),
                sarcasm=getattr(intent, "sarcasm_score", 0.0),
                heat_level=rhythm.heat_level,
                msg_rate=msg_rate,
                cooldown=cooldown,
                since_reply=seconds_since_reply,
                expressiveness=engine.expressiveness.expressiveness
                if engine.expressiveness
                else 0.5,
                sensitivity=engine.config.get("sensitivity", 0.5),
                affinity=affinity,
            )
        except Exception:
            pass

        # Update assistant emotion
        engine.assistant_emotion.update_from_interaction(emotion, user_id)

        # Semantic: record atmosphere snapshot, resolve feedback, record interaction
        recent_msgs = engine._helpers.get_recent_messages(group_id, n=10)
        now_iso = datetime.now(timezone.utc).isoformat()
        snapshot = AtmosphereSnapshot(
            timestamp=now_iso,
            group_valence=emotion.valence,
            group_arousal=emotion.arousal,
            active_participants=len({m.get("user_id") for m in recent_msgs}),
        )
        engine.semantic_memory.record_atmosphere(
            group_id=group_id,
            snapshot=snapshot,
        )
        if user_id:
            engine.semantic_memory.settle_engagement(
                group_id=group_id,
                user_id=user_id,
                directed_score=getattr(intent, "directed_score", 0.0),
                timestamp=now_iso,
            )
            engine.semantic_memory.record_interaction(
                group_id=group_id, user_id=user_id, timestamp=now_iso
            )

        return decision_result

    async def execution(
        self,
        decision: StrategyDecision,
        message: Message,
        intent: IntentAnalysisV3,
        emotion: EmotionState,
        memories: list[dict[str, Any]],
        group_id: str,
        empathy: Any,
        user_id: str,
    ) -> dict[str, Any]:
        """Execution layer: generate or queue reply."""
        engine = self._engine

        # === ✅ Plugin 命令执行路径（v1.2+）===
        if decision.strategy == ResponseStrategy.PLUGIN and decision.plugin_intent:
            if hasattr(engine, "_execute_plugin_command"):
                plugin_result = await engine._execute_plugin_command(
                    decision=decision,
                    message=message,
                    group_id=group_id,
                    user_id=user_id,
                )
                # 插件成功返回了有效回复 → 直接返回
                if plugin_result.get("reply") and not plugin_result.get("error"):
                    return plugin_result
                # 插件执行失败 → 回退到普通意图流程
                logger.info(
                    "插件 %s 执行失败（error=%s），降级为普通意图流程",
                    decision.plugin_intent,
                    plugin_result.get("error", "未知"),
                )
            # 不强制覆盖策略，让 decision 自然流过后续的 rhythm/gap 检查
            # 消息本身有高 directed_score，如果阈值条件满足会自然入队延迟队列

        # Rhythm context for style adaptation
        recent_msgs = engine._helpers.get_recent_messages(group_id, n=10)
        rhythm = engine.rhythm_analyzer.analyze(group_id, recent_msgs)

        # 收集人物传记信息（零 LLM，供后续 prompt 组装使用）
        self._collect_biography_section(group_id, user_id, message.content or "")

        # Turn gap suppression: don't interrupt conversation in full flow
        if (
            rhythm.turn_gap_readiness < engine.expressiveness.gap_readiness_threshold
            and intent.directed_score < engine.expressiveness.directed_threshold + 0.2
            and decision.strategy == ResponseStrategy.IMMEDIATE
        ):
            decision = StrategyDecision(
                strategy=ResponseStrategy.DELAYED,
                score=decision.score * 0.8,
                threshold=decision.threshold,
                urgency=decision.urgency,
                relevance=decision.relevance,
                reason=f"gap_not_ready:{decision.reason}",
            )
            engine._log_inner_thought("大家正聊得起劲呢，我先不插话了，等个合适的时机...")

        # Short filler suppression: pure punctuation / ultra-short messages
        # should not trigger immediate replies even if LLM overestimates directedness
        if (
            len(message.content or "") <= 2
            and not __import__("re").search(r"[\u4e00-\u9fff]", message.content or "")
            and decision.strategy == ResponseStrategy.IMMEDIATE
        ):
            decision = StrategyDecision(
                strategy=ResponseStrategy.DELAYED,
                score=decision.score * 0.5,
                threshold=decision.threshold,
                urgency=decision.urgency * 0.3,
                relevance=decision.relevance,
                reason=f"short_filler:{decision.reason}",
            )
            engine._log_inner_thought("就发个标点符号...先等等看有没有下文吧")

        # overheated + burst + not directed → downgrade to SILENT
        is_directed = intent.directed_score >= engine.expressiveness.directed_threshold
        if (
            rhythm.heat_level == "overheated"
            and rhythm.burst_detected
            and not is_directed
            and decision.strategy in (ResponseStrategy.IMMEDIATE, ResponseStrategy.DELAYED)
        ):
            engine._log_inner_thought("群聊太热闹了，我先不插话了...")
            engine._persist_group_state(group_id)
            return {
                "strategy": "silent",
                "reply": None,
                "emotion": emotion.to_dict(),
                "intent": intent.to_dict(),
            }

        if decision.strategy == ResponseStrategy.IMMEDIATE:
            engine._log_inner_thought("让我先稍等片刻，看看有没有后续消息...")
            return self._queue_response(
                decision=decision,
                message=message,
                intent=intent,
                emotion=emotion,
                memories=memories,
                group_id=group_id,
                user_id=user_id,
                rhythm=rhythm,
            )

        if decision.strategy == ResponseStrategy.DELAYED:
            return self._queue_response(
                decision=decision,
                message=message,
                intent=intent,
                emotion=emotion,
                memories=memories,
                group_id=group_id,
                user_id=user_id,
                rhythm=rhythm,
            )

        engine._persist_group_state(group_id)

        return {
            "strategy": decision.strategy.value,
            "reply": None,
            "emotion": emotion.to_dict(),
            "intent": intent.to_dict(),
        }

    def _queue_response(
        self,
        *,
        decision: StrategyDecision,
        message: Message,
        intent: IntentAnalysisV3,
        emotion: EmotionState,
        memories: list[dict[str, Any]],
        group_id: str,
        user_id: str,
        rhythm: Any,
    ) -> dict[str, Any]:
        engine = self._engine
        emotion_state = emotion.to_dict()
        engine.delayed_queue.enqueue(
            group_id=group_id,
            user_id=user_id,
            message_content=message.content,
            strategy_decision=decision,
            emotion_state=emotion_state,
            candidate_memories=[m.get("content", "") for m in memories],
            channel=message.channel,
            channel_user_id=message.channel_user_id,
            multimodal_inputs=message.multimodal_inputs,
            adapter_type=message.adapter_type,
            heat_level=rhythm.heat_level,
            pace=rhythm.pace,
            speaker_name=message.speaker or "",
            platform_message_id=message.message_id or "",
        )
        engine._persist_group_state(group_id)
        result = {
            "strategy": decision.strategy.value,
            "reply": None,
            "emotion": emotion_state,
            "intent": intent.to_dict(),
        }
        if decision.strategy == ResponseStrategy.IMMEDIATE:
            result["thought"] = ""
            result["partial_replies"] = []
        return result

    def _collect_biography_section(
        self,
        group_id: str,
        user_id: str,
        message_content: str,
    ) -> None:
        """收集人物传记信息，供 PromptFactory 使用。

        使用 BiographyView 从演化链派生传记，并从 UnifiedUserManager
        同步已确认别名到传记对象中，使 PromptFactory 能注入别名提示。
        结果缓存在 engine._pending_biography 字典中。
        """
        engine = self._engine
        bio_view = engine.biography_view
        mgr = engine.user_manager

        # 当前发言者传记（从演化链派生）
        speaker_bio = bio_view.get_biography(user_id) if user_id else None
        # 同步已确认别名（仅来自 _alias_index）
        if speaker_bio and user_id:
            alias_set: set[str] = set()
            # 也从 _alias_index 中收集该用户的所有别称
            for alias_key, entries in mgr._alias_index.items():
                for e in entries:
                    if e.user_id == user_id:
                        alias_set.add(alias_key)
                        break
            speaker_bio.aliases = sorted(alias_set)

        # 被提及者：从文本别名中收集
        mentioned: dict[str, float] = {}
        if message_content:
            for alias, entries in mgr._alias_index.items():
                if len(alias) < 2 or alias not in message_content:
                    continue
                uid, conf, _ = mgr.resolve_alias(
                    alias,
                    group_id=group_id,
                )
                if uid and uid != user_id:
                    mentioned[uid] = max(mentioned.get(uid, 0), conf)

        # 获取被提及者的传记并同步别名
        mentioned_bios: dict[str, Any] = {}
        for uid in mentioned.keys():
            bio = bio_view.get_biography(uid)
            alias_set_mentioned: set[str] = set()
            for alias_key, entries in mgr._alias_index.items():
                for e in entries:
                    if e.user_id == uid:
                        alias_set_mentioned.add(alias_key)
                        break
            bio.aliases = sorted(alias_set_mentioned)
            mentioned_bios[uid] = bio

        engine._pending_biography = {
            "speaker_card": speaker_bio,
            "mentioned_cards": list(mentioned_bios.values()),
            "confidence": mentioned,
            "affinity_score": 0.0,
        }

    def background_update(
        self,
        group_id: str,
        message: Message,
        emotion: EmotionState | None,
        intent: IntentAnalysisV3 | None,
        user_id: str,
    ) -> None:
        """Background updates after main pipeline.

        emotion / intent 可为 None（管线短路合并场景），
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
