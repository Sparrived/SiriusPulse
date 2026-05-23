"""Pipeline stages for EmotionalGroupChatEngine.

Perception → Cognition → Decision → Execution → BackgroundUpdate
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from sirius_pulse.core.engine_core import _EmotionalGroupChatEngineBase
from sirius_pulse.core.identity_resolver import IdentityContext

_Base = _EmotionalGroupChatEngineBase

from sirius_pulse.core.cognition import extract_keywords
from sirius_pulse.models.emotion import EmotionState
from sirius_pulse.models.intent_v3 import IntentAnalysisV3, SocialIntent
from sirius_pulse.models.models import Message, Participant
from sirius_pulse.models.response_strategy import ResponseStrategy, StrategyDecision

logger = logging.getLogger(__name__)


class PipelineMixin(_Base):
    """Mixin providing pipeline stage methods for EmotionalGroupChatEngine."""

    # ==================================================================
    # Pipeline stages
    # ==================================================================

    def _perception(
        self,
        group_id: str,
        message: Message,
        participants: list[Participant],
    ) -> str:
        """Perception layer: normalize, register participants, update transcript."""
        # New: Register participants via identity resolver and user manager
        for p in participants:
            ctx = IdentityContext(
                speaker_name=p.name,
                user_id=p.user_id,
                platform_uid=p.identities.get(message.channel) if message.channel else None,
                platform=message.channel,
                is_developer=p.is_developer,
            )
            self.identity_resolver.resolve(ctx, self.user_manager, group_id)

        # Resolve current sender to a stable user_id (may reuse UUID from
        # participants or fall back to speaker name / platform_uid lookup).
        sender_ctx = IdentityContext(
            speaker_name=message.speaker or "unknown",
            user_id=None,
            platform_uid=message.channel_user_id,
            platform=message.channel,
            is_developer=False,
        )
        sender_profile = self.identity_resolver.resolve(sender_ctx, self.user_manager, group_id)
        resolved_user_id = sender_profile.user_id
        resolved_speaker_name = sender_profile.name

        # Add to basic memory and archive to disk
        entry = self.basic_memory.add_entry(
            group_id=group_id,
            user_id=resolved_user_id,
            speaker_name=resolved_speaker_name,
            role="human",
            content=message.content,
            channel_user_id=message.channel_user_id or "",
            multimodal_inputs=(
                [dict(item) for item in message.multimodal_inputs]
                if message.multimodal_inputs
                else None
            ),
        )
        self.basic_store.append(entry)

        # Update group last message time
        from sirius_pulse.core.utils import now_iso

        self._group_last_message_at[group_id] = now_iso()
        self._persist_group_state(group_id)
        return resolved_user_id

    async def _cognition(
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
        # Build context from recent working memory (exclude current message)
        recent = self._get_recent_messages(group_id, n=6)
        if recent and recent[-1].get("content") == content:
            context_messages = recent[:-1]
        else:
            context_messages = recent

        # Joint cognition (emotion + intent + empathy in one pass)
        import time

        # 获取传记系统别名信息，帮助 LLM 区分 AI 和其他用户的别称
        group_aliases: dict[str, str] | None = None
        bio_mgr = getattr(self, "biography_manager", None)
        if bio_mgr is not None:
            try:
                group_aliases = bio_mgr.get_aliases_for_group(group_id) or None
            except Exception:
                pass

        t0 = time.perf_counter()
        emotion, intent, empathy = await self.cognition_analyzer.analyze(
            content, user_id, group_id, context_messages,
            sender_type=sender_type,
            multimodal_inputs=multimodal_inputs,
            caller_is_developer=caller_is_developer,
            group_aliases=group_aliases,
        )
        cognition_duration_ms = round((time.perf_counter() - t0) * 1000, 2)
        if self.cognition_analyzer._last_request is not None:
            self._record_subtask_tokens(
                task_name="cognition_analyze",
                model_name=self._task_models.get("cognition_analyze", self._default_model),
                group_id=group_id or "",
                request=self.cognition_analyzer._last_request,
                duration_ms=cognition_duration_ms,
            )

        # Rhythm context for persistence
        try:
            rhythm = self.rhythm_analyzer.analyze(group_id or "", recent)
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
            self.cognition_store.add(
                group_id=group_id or "",
                user_id=user_id or "",
                valence=getattr(emotion, "valence", 0.0),
                arousal=getattr(emotion, "arousal", 0.3),
                basic_emotion=getattr(getattr(emotion, "basic_emotion", None), "name", "") if getattr(emotion, "basic_emotion", None) else "",
                intensity=getattr(emotion, "intensity", 0.5),
                social_intent=getattr(getattr(intent, "social_intent", None), "value", "") if getattr(intent, "social_intent", None) else getattr(intent, "intent_type", ""),
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
        intent.topic_relevance_score = self._enhance_topic_relevance(
            intent.topic_relevance_score, content, group_id, user_id
        )

        # v1.3+: 维护短期话题窗口（滑动窗口，保留最近 N 条消息的关键词快照）
        try:
            msg_kw = extract_keywords(content)
            window = self._topic_window.setdefault(group_id or "", [])
            window.append(msg_kw)
            # 只保留最近的 max_size 条
            max_size = getattr(self, "_topic_window_max_size", 10)
            if len(window) > max_size:
                window[:] = window[-max_size:]
        except Exception:
            logger.warning("裁剪话题窗口失败", exc_info=True)
            pass

        # Memory retrieval now happens in execution via ContextAssembler
        memories = []

        return intent, emotion, memories, empathy

    def _decision(
        self,
        intent: IntentAnalysisV3,
        emotion: EmotionState,
        group_id: str,
        user_id: str,
        sender_type: str = "human",
    ) -> StrategyDecision:
        """Decision layer: strategy selection with threshold and rhythm."""

        # === ✅ Plugin 命令快速路径（v1.2+）===
        # 指令语义已明确，跳过 threshold/strategy，直接返回 PLUGIN 策略
        if intent.social_intent == SocialIntent.PLUGIN_COMMAND:
            return StrategyDecision(
                strategy=ResponseStrategy.PLUGIN,
                score=1.0,
                threshold=0.0,  # 无需门槛
                urgency=intent.urgency_score,
                relevance=intent.relevance_score,
                reason=f"plugin_command:{intent.plugin_intent}",
                plugin_intent=intent.plugin_intent,
                plugin_slots=dict(intent.plugin_slots),
                plugin_render_mode=intent.plugin_render_mode,
            )

        # Rhythm context
        recent_msgs = self._get_recent_messages(group_id, n=10)
        rhythm = self.rhythm_analyzer.analyze(group_id, recent_msgs)

        # Compute dynamic threshold via ThresholdEngine
        user_profile = self.semantic_memory.get_user_profile(group_id, user_id)

        # Message rate (per minute) from recent messages
        msg_rate = self._message_rate_per_minute(recent_msgs)

        threshold = self.threshold_engine.compute(
            sensitivity=self.config.get("sensitivity", 0.5),
            heat_level=rhythm.heat_level,
            messages_per_minute=msg_rate,
            user_profile=user_profile,
            sender_type=sender_type,
        )

        # Persona reply frequency bias
        freq = self.persona.reply_frequency
        if freq == "high":
            threshold *= 0.8
        elif freq == "low":
            threshold *= 1.3
        elif freq == "selective":
            # Only reply when strongly directed (>=threshold) or high urgency
            if intent.directed_score < self.expressiveness.directed_threshold and intent.urgency_score < 70:
                threshold *= 2.0

        # Entitlement suppression: if AI is not qualified for this topic, raise threshold
        if intent.entitlement_score < self.expressiveness.entitlement_threshold:
            threshold *= 1.5
            self._log_inner_thought("这个话题我好像不太擅长...先谨慎一点吧")

        # 传记亲和力调节：仅当 LLM 曾输出过传记（last_updated_at 非空）时才生效
        biography_card = None
        affinity = 0.0
        if getattr(self, "biography_manager", None) is not None:
            biography_card = self.biography_manager.get_card(user_id)
            if biography_card is not None and biography_card.last_updated_at:
                # 用 EMA 平滑后的 affinity_score，不完全信任单次 LLM 输出
                affinity = biography_card.affinity_score
                if affinity > 0.3:
                    factor = 1.0 - min(0.25, affinity * 0.15)
                    threshold *= factor
                elif affinity < -0.3:
                    factor = 1.0 + min(0.40, abs(affinity) * 0.25)
                    threshold *= factor
                if abs(affinity) > 0.3:
                    self._log_inner_thought(
                        f"对ta的认知是affinity={affinity:.2f}，响应门槛调整了"
                    )

        intent.threshold = threshold
        intent.activity_factor = self.threshold_engine._activity_factor(rhythm.heat_level, msg_rate)
        intent.time_factor = self.threshold_engine._time_factor(None)
        if user_profile:
            intent.engagement_factor = self.threshold_engine._engagement_factor(
                user_profile
            )

        sensitivity = self.config.get("sensitivity", 0.5)
        directed_gate = self.expressiveness.directed_threshold + (1.0 - sensitivity) * 0.15
        is_mentioned = intent.directed_score >= directed_gate

        decision = self.strategy_engine.decide(
            intent,
            is_mentioned=is_mentioned,
            weak_directed_threshold=self.expressiveness.weak_directed_threshold,
            heat_level=rhythm.heat_level,
            sender_type=sender_type,
        )

        # Reply cooldown suppression: delayed responses are throttled,
        # but immediate responses (e.g. direct mentions) bypass cooldown.
        now = datetime.now(timezone.utc).timestamp()
        last_reply = self._last_reply_at.get(group_id, 0)
        seconds_since_reply = now - last_reply
        cooldown = self.config.get("reply_cooldown_seconds", self.expressiveness.cooldown_seconds)
        if seconds_since_reply < cooldown and decision.strategy == ResponseStrategy.DELAYED:
            decision = StrategyDecision(
                strategy=ResponseStrategy.SILENT,
                score=0.0,
                threshold=decision.threshold,
                urgency=decision.urgency,
                relevance=decision.relevance,
                reason=f"cooldown_{int(seconds_since_reply)}s",
            )
            self._log_inner_thought(f"群里正聊得火热呢，我刚回完不久，先闭嘴看看...")

        # Private-chat floor: never stay completely silent in 1-on-1
        if group_id.startswith("private_") and decision.strategy == ResponseStrategy.SILENT:
            decision = StrategyDecision(
                strategy=ResponseStrategy.DELAYED,
                score=decision.score,
                threshold=decision.threshold,
                urgency=max(decision.urgency, 25.0),
                relevance=max(decision.relevance, 0.5),
                reason=f"private_chat_floor:{decision.reason}",
            )

        # 内心活动：决策后的思考
        self._log_decision_thought(intent, decision)

        # 结构化日志：记录关键决策参数到后台
        logger.info(
            "[决策参数] group=%s user=%s strategy=%s score=%.3f threshold=%.3f "
            "directed_score=%.3f directed_gate=%.3f directed=%s urgency=%.1f "
            "entitlement=%.3f sarcasm=%.3f affinity=%.2f "
            "heat_level=%s msg_rate=%.2f cooldown=%.1fs since_reply=%.1fs "
            "expressiveness=%.2f sensitivity=%.2f reason=%s",
            group_id,
            user_id,
            decision.strategy.value if hasattr(decision.strategy, "value") else str(decision.strategy),
            decision.score,
            decision.threshold,
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
            self.expressiveness.expressiveness if self.expressiveness else 0.5,
            self.config.get("sensitivity", 0.5),
            getattr(decision, "reason", ""),
        )

        # Update assistant emotion
        self.assistant_emotion.update_from_interaction(emotion, user_id)

        # Semantic: record atmosphere snapshot, resolve feedback, record interaction
        recent_msgs = self._get_recent_messages(group_id, n=10)
        self.semantic_memory.record_atmosphere(
            group_id=group_id,
            valence=emotion.valence,
            arousal=emotion.arousal,
            active_participants=len({m.get("user_id") for m in recent_msgs}),
        )
        if user_id:
            self.semantic_memory.resolve_pending_feedback(
                group_id=group_id,
                user_id=user_id,
                directed_score=getattr(intent, "directed_score", 0.0),
            )
            self.semantic_memory.record_user_interaction(group_id=group_id, user_id=user_id)

        return decision

    async def _execution(
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

        # === ✅ Plugin 命令执行路径（v1.2+）===
        if decision.strategy == ResponseStrategy.PLUGIN and decision.plugin_intent:
            if hasattr(self, '_execute_plugin_command'):
                plugin_result = await self._execute_plugin_command(
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
        recent_msgs = self._get_recent_messages(group_id, n=10)
        rhythm = self.rhythm_analyzer.analyze(group_id, recent_msgs)

        # 收集人物传记信息（零 LLM，供后续 prompt 组装使用）
        self._collect_biography_section(group_id, user_id, message.content or "")

        # Turn gap suppression: don't interrupt conversation in full flow
        if (
            rhythm.turn_gap_readiness < self.expressiveness.gap_readiness_threshold
            and intent.directed_score < self.expressiveness.directed_threshold + 0.2
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
            self._log_inner_thought("大家正聊得起劲呢，我先不插话了，等个合适的时机...")

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
            self._log_inner_thought("就发个标点符号...先等等看有没有下文吧")

        # overheated + burst + not directed → downgrade to SILENT
        is_directed = intent.directed_score >= self.expressiveness.directed_threshold
        if (
            rhythm.heat_level == "overheated"
            and rhythm.burst_detected
            and not is_directed
            and decision.strategy in (ResponseStrategy.IMMEDIATE, ResponseStrategy.DELAYED)
        ):
            self._log_inner_thought("群聊太热闹了，我先不插话了...")
            self._persist_group_state(group_id)
            return {
                "strategy": "silent",
                "reply": None,
                "emotion": emotion.to_dict(),
                "intent": intent.to_dict(),
            }

        emotion_state = emotion.to_dict()

        if decision.strategy == ResponseStrategy.IMMEDIATE:
            self._log_inner_thought("让我先稍等片刻，看看有没有后续消息...")
            self.delayed_queue.enqueue(
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
            )
            self._persist_group_state(group_id)
            return {
                "strategy": "immediate",
                "reply": None,
                "emotion": emotion.to_dict(),
                "intent": intent.to_dict(),
                "thought": "",
                "partial_replies": [],
            }

        if decision.strategy == ResponseStrategy.DELAYED:
            self.delayed_queue.enqueue(
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
            )
            self._persist_group_state(group_id)
            return {
                "strategy": "delayed",
                "reply": None,
                "emotion": emotion.to_dict(),
                "intent": intent.to_dict(),
            }

        self._persist_group_state(group_id)

        return {
            "strategy": decision.strategy.value,
            "reply": None,
            "emotion": emotion.to_dict(),
            "intent": intent.to_dict(),
        }

    def _collect_biography_section(
        self,
        group_id: str,
        user_id: str,
        message_content: str,
    ) -> None:
        """收集人物传记信息，供 PromptFactory 使用。

        结果缓存在 self._pending_biography 字典中，
        供 _build_delayed_prompt 等后续 prompt 组装阶段取用。
        """
        mgr = getattr(self, "biography_manager", None)
        if mgr is None:
            self._pending_biography = {}
            return

        # 当前发言者传记
        speaker_card = mgr.get_card(user_id) if user_id else None

        # 被提及者：从文本别名中收集
        mentioned: dict[str, float] = {}
        if message_content:
            for alias, entries in mgr._alias_index.items():
                if len(alias) < 2 or alias not in message_content:
                    continue
                uid, conf, _ = mgr.resolve_alias(
                    alias, group_id=group_id,
                )
                if uid and uid != user_id:
                    mentioned[uid] = max(mentioned.get(uid, 0), conf)
                elif conf == 0.0:
                    for entry in entries:
                        if group_id in entry.groups and entry.user_id != user_id:
                            mentioned[entry.user_id] = 0.0

        mentioned_cards = mgr.get_cards_for_users(list(mentioned.keys()))
        all_aliases = mgr.get_aliases_for_group(group_id)

        self._pending_biography = {
            "speaker_card": speaker_card,
            "mentioned_cards": mentioned_cards,
            "confidence": mentioned,
            "aliases": all_aliases,
            "affinity_score": speaker_card.affinity_score if speaker_card else 0.0,
        }

    def _background_update(
        self,
        group_id: str,
        message: Message,
        emotion: EmotionState,
        intent: IntentAnalysisV3,
        user_id: str,
    ) -> None:
        """Background updates after main pipeline."""
        # Update group sentiment cache for emotion island detection
        self.cognition_analyzer.update_group_sentiment(group_id, emotion)

        # Update assistant emotion based on interaction
        self.assistant_emotion.update_from_interaction(emotion, user_id)

        # Save display name (QQ name + group nickname) and accumulate content
        speaker_name = getattr(message, "speaker", "") or ""
        nickname = getattr(message, "nickname", "") or ""
        if user_id:
            if nickname:
                display_name = nickname
                if speaker_name and speaker_name != nickname:
                    display_name = f"{nickname}({speaker_name})"
                self.semantic_memory.set_user_profile_fields(
                    group_id, user_id, name=display_name
                )
            elif speaker_name:
                self.semantic_memory.set_user_profile_fields(
                    group_id, user_id, name=speaker_name
                )

