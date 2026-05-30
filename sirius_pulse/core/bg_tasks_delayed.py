"""延迟队列相关后台任务。

包含延迟队列轮询、延迟响应处理等功能。
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from sirius_pulse.core.delayed_response_queue import _parse_iso
from sirius_pulse.core.events import SessionEvent, SessionEventType
from sirius_pulse.core.prompt_factory import PromptFactory
from sirius_pulse.core.utils import parse_sticker_tags
from sirius_pulse.skills.executor import strip_skill_calls

if TYPE_CHECKING:
    from sirius_pulse.core.engine_core import _EmotionalGroupChatEngineBase

logger = logging.getLogger(__name__)


class DelayedQueueTasks:
    """延迟队列相关任务组件。"""

    def __init__(self, engine: _EmotionalGroupChatEngineBase) -> None:
        self._engine = engine

    async def delayed_queue_ticker(self) -> None:
        """Smart-sleep ticker for the delayed queue.

        Wakes up at the next pending item's expiry time (or max interval)
        and emits DELAYED_RESPONSE_TRIGGERED events for expired items only.
        Actual reply generation and delivery is handled by the external
        caller via tick_delayed_queue().
        """
        engine = self._engine
        max_interval = engine.config.get("delayed_queue_tick_interval_seconds", 10)
        while engine._bg_running:
            # Compute how long we can sleep until the next item expires
            next_wake = max_interval
            now = datetime.now(timezone.utc)
            for group_id in list(engine._group_last_message_at.keys()):
                for item in engine.delayed_queue.get_pending(group_id):
                    enqueue_dt = _parse_iso(item.enqueue_time)
                    if enqueue_dt:
                        remaining = item.window_seconds - (now - enqueue_dt).total_seconds()
                        if remaining <= 0:
                            next_wake = 0
                            break
                        next_wake = min(next_wake, remaining)
                    if next_wake <= 0:
                        break
                if next_wake <= 0:
                    break

            # Guard against busy-loop when items are already expired but not yet
            # consumed by the external delivery loop.
            if next_wake <= 0:
                next_wake = 1.0

            await asyncio.sleep(next_wake)

            now = datetime.now(timezone.utc)
            for group_id in list(engine._group_last_message_at.keys()):
                try:
                    pending = engine.delayed_queue.get_pending(group_id)
                    # Per-group emitted tracking: only clean up IDs that no longer
                    # exist in this group's pending list.
                    emitted = engine._delayed_event_emitted.setdefault(group_id, set())
                    existing_ids = {i.item_id for i in pending}
                    emitted &= existing_ids

                    expired = []
                    for item in pending:
                        enqueue_dt = _parse_iso(item.enqueue_time)
                        if enqueue_dt and (now - enqueue_dt).total_seconds() >= item.window_seconds:
                            expired.append(item)

                    newly_expired = [i for i in expired if i.item_id not in emitted]
                    if newly_expired:
                        engine._log_inner_thought("之前记下的延迟回复，现在该开口了～")
                        for item in newly_expired:
                            emitted.add(item.item_id)
                            await engine.event_bus.emit(
                                SessionEvent(
                                    type=SessionEventType.DELAYED_RESPONSE_TRIGGERED,
                                    data={
                                        "group_id": group_id,
                                        "item_id": item.item_id,
                                    },
                                )
                            )
                except Exception as exc:
                    logger.warning("Delayed queue tick failed for %s: %s", group_id, exc)

    async def tick_delayed_queue(
        self,
        group_id: str,
        on_partial_reply: Any | None = None,
    ) -> list[dict[str, Any]]:
        """Process delayed response queue for a group.

        If multiple items trigger in the same tick, merge them into a single
        prompt so the model generates only one consolidated reply.
        Supports multi-round SKILL execution similar to immediate responses.

        Args:
            group_id: The group / private chat to tick.
            on_partial_reply: Optional async callable invoked immediately
                when non-skill text is extracted *before* skills are executed.
        """
        engine = self._engine
        recent = engine._helpers.get_recent_messages(group_id, n=10)
        rhythm = engine.rhythm_analyzer.analyze(group_id, recent)
        triggered = engine.delayed_queue.tick(group_id, recent, rhythm)
        if not triggered:
            return []

        # Determine caller from the first triggered item
        caller_profile = None
        item = triggered[0]
        # Defensive: if _queues was corrupted externally, item may be a dict.
        if isinstance(item, dict):
            logger.warning(
                "tick_delayed_queue: triggered[0] is dict (item_id=%s), converting to DelayedResponseItem",
                item.get("item_id", "unknown"),
            )
            from sirius_pulse.models.response_strategy import (
                DelayedResponseItem,
                ResponseStrategy,
                StrategyDecision,
            )

            sd_raw = item.get("strategy_decision", {}) or {}
            try:
                strategy_val = sd_raw.get("strategy", "silent")
                if isinstance(strategy_val, str):
                    strategy_enum = ResponseStrategy(strategy_val)
                else:
                    strategy_enum = ResponseStrategy.SILENT
            except Exception:
                strategy_enum = ResponseStrategy.SILENT
            strategy_decision = StrategyDecision(
                strategy=strategy_enum,
                score=float(sd_raw.get("score", 0.0)),
                threshold=float(sd_raw.get("threshold", 0.5)),
                urgency=float(sd_raw.get("urgency", 0.0)),
                relevance=float(sd_raw.get("relevance", 0.0)),
                reason=str(sd_raw.get("reason", "")),
                estimated_delay_seconds=float(sd_raw.get("estimated_delay_seconds", 0.0)),
                context=dict(sd_raw.get("context", {})),
            )
            item = DelayedResponseItem(
                item_id=item.get("item_id", ""),
                group_id=item.get("group_id", group_id),
                user_id=item.get("user_id", ""),
                channel=item.get("channel"),
                channel_user_id=item.get("channel_user_id"),
                message_content=item.get("message_content", ""),
                strategy_decision=strategy_decision,
                emotion_state=item.get("emotion_state", {}),
                candidate_memories=item.get("candidate_memories", []),
                enqueue_time=item.get("enqueue_time", ""),
                window_seconds=float(item.get("window_seconds", 30.0)),
                status=item.get("status", "pending"),
                multimodal_inputs=item.get("multimodal_inputs", []),
            )
            triggered[0] = item

        resolved_uid: str | None = None
        if item.channel and item.channel_user_id:
            resolved_uid = engine.user_manager.resolve_user_id(
                platform=item.channel,
                external_uid=item.channel_user_id,
            )
            if resolved_uid:
                caller_profile = engine.user_manager.get_user(resolved_uid, group_id)
        if caller_profile is None:
            # Fallback: search by user_id (nickname) across all groups
            resolved_uid = engine.user_manager.resolve_user_id(speaker=item.user_id)
            if resolved_uid:
                caller_profile = engine.user_manager.get_user(resolved_uid, group_id)
        caller_is_developer = bool(caller_profile and caller_profile.is_developer)

        # Engagement rate for SKILL permission control
        caller_engagement = 0.0
        if resolved_uid:
            semantic_profile = engine.semantic_memory.get_user_profile(group_id, resolved_uid)
            if semantic_profile:
                caller_engagement = semantic_profile.engagement_rate

        # Merge all triggered items into one prompt and one generation call
        adapter_type = getattr(triggered[0], "adapter_type", None) if triggered else None
        bundle = self._build_delayed_prompt(
            triggered,
            group_id,
            caller_is_developer=caller_is_developer,
            adapter_type=adapter_type,
        )

        # 读取 speaker_card 用于丰富日记检索 query
        pending_bio: dict = getattr(engine, "_pending_biography", {}) or {}
        speaker_card = pending_bio.get("speaker_card")

        # Use ContextAssembler to build full messages with diary RAG + XML history
        recent_n = engine.config.get("basic_memory_context_window", 5)
        diary_top_k = engine.config.get("diary_top_k", 5)
        diary_token_budget = engine.config.get("diary_token_budget", 800)
        msgs, ca_breakdown = engine.context_assembler.build_messages_with_breakdown(
            group_id=group_id,
            current_query=bundle.user_content,
            system_prompt=bundle.system_prompt,
            search_query=bundle.user_content,
            recent_n=recent_n,
            diary_top_k=diary_top_k,
            diary_token_budget=diary_token_budget,
            include_pending=True,
            biography_card=speaker_card,
        )
        system_prompt = msgs[0]["content"]
        messages = msgs[1:]

        # Merge assembler breakdown into response-assembler breakdown
        token_breakdown = bundle.token_breakdown.to_dict() if bundle.token_breakdown else {}
        for key, val in ca_breakdown.items():
            if key == "diary":
                token_breakdown["memory"] = token_breakdown.get("memory", 0) + val
            else:
                token_breakdown[key] = token_breakdown.get(key, 0) + val

        # Collect multimodal inputs from all triggered items and inject into user message.
        all_multimodal: list[dict[str, str]] = []
        for triggered_item in triggered:
            if getattr(triggered_item, "multimodal_inputs", None):
                for m in triggered_item.multimodal_inputs:
                    if m.get("type") == "image" and m.get("sub_type") == "1":
                        continue
                    all_multimodal.append(m)

        messages = engine._helpers.inject_multimodal_into_user_message(messages, all_multimodal)

        # Multi-round generation with SKILL support
        from sirius_pulse.core.brain import ChatRequest
        from sirius_pulse.skills.executor import parse_skill_calls, strip_skill_calls
        from sirius_pulse.skills.models import SkillInvocationContext

        max_skill_rounds = engine.config.get("max_skill_rounds", 8)
        partial_replies: list[str] = []
        _any_partial_sent = False
        last_round_had_partial = False
        _round = 0
        calls: list[tuple[str, dict[str, Any]]] = []
        reply = ""

        for _round in range(max_skill_rounds + 1):
            chat_result = await engine.brain.chat(
                ChatRequest(
                    group_id=group_id,
                    user_id="",
                    system_prompt=system_prompt,
                    messages=messages,
                    task_name="response_generate",
                    enable_skills=False,
                    post_process=False,
                )
            )
            reply = chat_result.raw_text.strip()
            round_clean = chat_result.clean_text
            round_stickers = chat_result.sticker_names

            calls = parse_skill_calls(reply)
            if not calls or engine._skill_registry is None or engine._skill_executor is None:
                break

            # Determine if every invoked skill is marked silent.
            all_silent = all(
                engine._skill_registry.get(name) is not None and engine._skill_registry.get(name).silent
                for name, _ in calls
            )

            # 使用 chat_result 已解析好的 clean_text 和 sticker_names
            non_skill_text = round_clean
            if non_skill_text and round_stickers:
                asyncio.create_task(
                    engine._send_stickers_by_names(group_id, round_stickers)
                )
                logger.info("partial reply 中解析到表情包: %s", round_stickers)
            last_round_had_partial = False
            if non_skill_text and not all_silent:
                engine._log_inner_thought(f"先跟用户回一声：{non_skill_text[:40]}...")
                last_round_had_partial = True
                _any_partial_sent = True
                if on_partial_reply is not None:
                    try:
                        await on_partial_reply(non_skill_text)
                    except Exception as exc:
                        logger.warning("on_partial_reply failed: %s", exc)
                else:
                    partial_replies.append(non_skill_text)

            # Execute skills and collect results
            skill_results: list[str] = []
            skill_multimodal: list[dict[str, Any]] = []
            from sirius_pulse.memory.user.models import UserProfile

            caller_user_id = item.user_id
            skill_caller = UserProfile(
                user_id=caller_user_id,
                name=caller_profile.name if caller_profile else caller_user_id,
                metadata={"is_developer": caller_is_developer},
            )
            developer_profiles: list[UserProfile] = []
            group_entries = engine.user_manager.entries.get(group_id, {})
            for profile in group_entries.values():
                if profile.is_developer:
                    developer_profiles.append(profile)

            if engine._skill_executor is not None:
                engine._skill_executor.set_chat_context(
                    group_id=group_id, user_id=caller_user_id or ""
                )

            for idx, (skill_name, params) in enumerate(calls):
                skill = engine._skill_registry.get(skill_name)
                if skill is None:
                    err = f"SKILL '{skill_name}' 未找到"
                    logger.warning(err)
                    if not all_silent:
                        skill_results.append(
                            PromptFactory.build_skill_status_message("未找到", skill_name)
                        )
                    continue
                # Engagement-based permission
                if caller_engagement < 0.1 and not caller_is_developer:
                    err = f"SKILL '{skill_name}' 被拒绝：互动不足 (engagement={caller_engagement:.2f})"
                    logger.warning(err)
                    if not all_silent:
                        skill_results.append(
                            PromptFactory.build_skill_status_message(
                                "拒绝", skill_name, "你还不够熟，这个技能暂不可用"
                            )
                        )
                    continue

                if skill.developer_only and not caller_is_developer:
                    err = f"SKILL '{skill_name}' 被拒绝：caller 不是 developer"
                    logger.warning(err)
                    if not all_silent:
                        skill_results.append(
                            PromptFactory.build_skill_status_message(
                                "拒绝", skill_name, "该技能仅 developer 可用"
                            )
                        )
                    continue
                ctx = SkillInvocationContext(
                    caller=skill_caller,
                    developer_profiles=developer_profiles,
                )
                logger.info(
                    "Skill execute: %s(params=%s, caller=%s, group=%s)",
                    skill_name,
                    params,
                    caller_user_id,
                    group_id,
                )
                try:
                    result = await engine._skill_executor.execute_async(
                        skill, params, invocation_context=ctx, max_retries=2
                    )
                    logger.info(
                        "Skill execute success: %s -> %s",
                        skill_name,
                        "success" if result.success else "failed",
                    )
                    if result.success:
                        if not skill.silent:
                            skill_results.append(
                                PromptFactory.build_skill_status_message(
                                    "结果", skill_name, result.to_display_text()
                                )
                            )
                            for block in result.multimodal_blocks:
                                skill_multimodal.append(
                                    {
                                        "type": "image_url",
                                        "image_url": {"url": block.value},
                                    }
                                )
                        # Auto-persist glossary terms from learn_term
                        if skill_name == "learn_term":
                            term = params.get("term", "")
                            definition = params.get("definition", "")
                            if term and definition:
                                from sirius_pulse.memory.glossary import GlossaryTerm

                                engine.glossary_manager.add_or_update(
                                    group_id,
                                    GlossaryTerm(term=term, definition=definition, source="skill"),
                                )
                        # Inject group_id into newly created reminders
                        if (
                            skill_name == "reminder"
                            and params.get("action", "").strip().lower() == "create"
                        ):
                            self._inject_group_id_into_latest_reminder(group_id)
                    else:
                        err = result.error or "未知错误"
                        logger.warning("SKILL '%s' 执行失败: %s", skill_name, err)
                        if not skill.silent:
                            skill_results.append(
                                PromptFactory.build_skill_status_message("失败", skill_name, err)
                            )
                except Exception as exc:
                    logger.error("SKILL '%s' 执行异常: %s", skill_name, exc)
                    if not skill.silent:
                        skill_results.append(
                            PromptFactory.build_skill_status_message("异常", skill_name, str(exc))
                        )

                # 链式调用中间增加延迟，避免回复过快
                if idx < len(calls) - 1:
                    await asyncio.sleep(2)

            # If all skills were silent, skip the follow-up generation round.
            if all_silent:
                break

            # Inject skill results into the conversation for the next round
            assistant_content = strip_skill_calls(reply)
            if _any_partial_sent:
                assistant_content += "\n\n（以上内容已发送给用户）"
            messages.append({"role": "assistant", "content": assistant_content})

            suffix_parts = [
                "\n\n[继续] 请基于以上技能执行结果，继续完成你的回复。",
                "如果结果中包含错误或失败信息，请直接忽略，",
                "不要向用户提及任何关于能力调用或失败的内容，继续自然地对话。",
                "如果当前结果还不够完整，你可以继续调用其他技能来获取补充信息，",
                "形成链式调用。",
                "重要：如果你说要去搜索、查找、读取或执行任何操作，",
                "必须在同一句回复中紧跟对应的 [SKILL_CALL: ...] 标记，绝对不能只说不动。",
                '错误示例（只说不动）："我再去搜索一下" ❌',
                '正确示例（边说边做）："我再去搜索一下 [SKILL_CALL: bing_search | {\\"query\\": \\"xxx\\"}]" ✅',
                "重要：你的每次回复都必须包含自然语言内容，",
                "不能把 SKILL_CALL 标记作为回复的唯一内容。",
            ]
            if _any_partial_sent:
                suffix_parts.append(
                    "注意：上文标记为\"已发送给用户\"的内容已经由你发送给用户，"
                    "现在只需基于技能结果给出简短补充，不要重复之前的确认内容。"
                )
            messages.append(
                {
                    "role": "user",
                    "content": PromptFactory.build_skill_result_content(
                        skill_results,
                        skill_multimodal,
                        suffix="\n\n".join(suffix_parts),
                    ),
                }
            )

            # Persist intermediate skill turns into basic memory
            _entry = engine.basic_memory.add_entry(
                group_id=group_id,
                user_id="assistant",
                role="assistant",
                content=strip_skill_calls(reply),
                speaker_name=engine.persona.name if engine.persona else "assistant",
                system_prompt=system_prompt,
            )
            engine.basic_store.append(_entry)
            if skill_results:
                _MEMORY_SKILL_RESULT_CHAR_LIMIT = 4000
                _raw = "\n".join(skill_results)
                if len(_raw) > _MEMORY_SKILL_RESULT_CHAR_LIMIT:
                    _truncated = _raw[:_MEMORY_SKILL_RESULT_CHAR_LIMIT]
                    _last_nl = _truncated.rfind("\n")
                    if _last_nl > _MEMORY_SKILL_RESULT_CHAR_LIMIT * 0.8:
                        _truncated = _truncated[:_last_nl]
                    _raw = (
                        f"{_truncated}\n\n"
                        f"{PromptFactory.build_memory_skill_truncation(_MEMORY_SKILL_RESULT_CHAR_LIMIT, len(_raw))}"
                    )
                _sys_entry = engine.basic_memory.add_entry(
                    group_id=group_id,
                    user_id="skill_system",
                    role="system",
                    content=PromptFactory.build_memory_skill_result(
                        _raw, _MEMORY_SKILL_RESULT_CHAR_LIMIT
                    ),
                )
                engine.basic_store.append(_sys_entry)

        # If the loop ended because max rounds were exhausted and the last round
        # already sent a partial reply, don't duplicate that text as the final reply.
        ended_because_max_rounds = (
            _round == max_skill_rounds
            and calls
            and engine._skill_registry is not None
            and engine._skill_executor is not None
        )
        if ended_because_max_rounds and last_round_had_partial:
            logger.debug(
                "Chain hit max_skill_rounds=%d; last partial already sent, "
                "clearing clean_reply to avoid duplication",
                max_skill_rounds,
            )
            reply = ""

        # Record assistant reply into basic memory so future turns can see it
        clean_reply = strip_skill_calls(reply).strip()

        # 解析表情包标签 [STICKERS: ...] 并异步发送
        if clean_reply:
            clean_reply, sticker_names = parse_sticker_tags(clean_reply)
            if sticker_names:
                asyncio.create_task(
                    engine._send_stickers_by_names(group_id, sticker_names)
                )
                logger.info("模型请求发送表情包: %s", sticker_names)

        # Deduplication: suppress if nearly identical to a recent reply
        if clean_reply:
            now_ts = datetime.now(timezone.utc).timestamp()
            recent_replies = engine._recent_sent_replies.get(group_id, [])
            recent_replies = [(t, r) for t, r in recent_replies if now_ts - t < engine._reply_dedup_window]
            if any(
                engine._text_similarity(clean_reply, r) > engine._reply_dedup_threshold
                for _, r in recent_replies
            ):
                logger.debug(
                    "Suppressing duplicate reply for %s (window=%ds, threshold=%.2f): %s...",
                    group_id,
                    engine._reply_dedup_window,
                    engine._reply_dedup_threshold,
                    clean_reply[:40],
                )
                clean_reply = ""
            else:
                recent_replies.append((now_ts, clean_reply))
            engine._recent_sent_replies[group_id] = recent_replies

        if clean_reply:
            _reply_entry = engine.basic_memory.add_entry(
                group_id=group_id,
                user_id="assistant",
                role="assistant",
                content=clean_reply,
                speaker_name=engine.persona.name if engine.persona else "assistant",
                system_prompt=system_prompt,
            )
            engine.basic_store.append(_reply_entry)
            # 反馈追踪：AI 发言后记录锚点，等待用户跟进
            target_uid = triggered[0].user_id or ""
            engine.semantic_memory.record_response_sent(
                group_id=group_id,
                user_id=target_uid,
                topic_hint=clean_reply[:100],
                response_length=len(clean_reply),
            )

        # Determine return strategy
        from sirius_pulse.models.response_strategy import ResponseStrategy

        strategy = "delayed"
        if any(i.strategy_decision.strategy == ResponseStrategy.IMMEDIATE for i in triggered):
            strategy = "immediate"

        # Never leak raw SKILL_CALL markers to the user.
        final_reply = clean_reply or (partial_replies[-1] if partial_replies else "")

        # Record reply timestamp for cooldown tracking (once per tick)
        engine._last_reply_at[group_id] = datetime.now(timezone.utc).timestamp()
        engine._persist_group_state(group_id)

        # Emit event with full reply data for external delivery
        await engine.event_bus.emit(
            SessionEvent(
                type=SessionEventType.DELAYED_RESPONSE_TRIGGERED,
                data={
                    "group_id": group_id,
                    "item_id": triggered[0].item_id,
                    "reply": final_reply,
                    "partial_replies": partial_replies,
                },
            )
        )

        return [
            {
                "strategy": strategy,
                "item_id": triggered[0].item_id,
                "reply": final_reply,
                "partial_replies": partial_replies,
            }
        ]

    def _build_delayed_prompt(
        self,
        items: Any,
        group_id: str,
        caller_is_developer: bool = False,
        adapter_type: str | None = None,
    ):
        """构建延迟响应的 PromptBundle。"""
        engine = self._engine
        if not isinstance(items, list):
            items = [items]
        if len(items) == 1:
            message_content = items[0].message_content
            speaker_name = items[0].speaker_name
            channel_user_id = getattr(items[0], "channel_user_id", "") or ""
        else:
            parts = [item.message_content for item in items]
            message_content = "\n".join(parts)
            speaker_name = items[-1].speaker_name
            channel_user_id = getattr(items[-1], "channel_user_id", "") or ""
        glossary = engine.glossary_manager.build_prompt_section(
            group_id, text=message_content, max_terms=5
        )
        # 收集触发批次中所有用户的语义画像
        related_uids: set[str] = set()
        for item in items:
            for uid in getattr(item, "related_user_ids", []):
                if uid:
                    related_uids.add(uid)
        delayed_user_profiles: list[Any] = []
        for uid in related_uids:
            prof = engine.semantic_memory.get_user_profile(group_id, uid)
            if prof:
                delayed_user_profiles.append(prof)

        # 收集候选记忆
        candidate_memories: list[dict[str, Any]] = []
        for item in items:
            for cm in getattr(item, "candidate_memories", []) or []:
                if cm:
                    candidate_memories.append({"source": "working_memory", "content": cm})

        style_params = engine.style_adapter.adapt(
            pace="decelerating",
            persona=engine.persona,
        )

        # 读取 pipeline 缓存的传记上下文
        pending_bio: dict[str, Any] = getattr(engine, "_pending_biography", {}) or {}

        return PromptFactory.assemble_chat(
            message_content=message_content,
            speaker_name=speaker_name,
            channel_user_id=channel_user_id,
            content_is_tagged=True,
            memories=candidate_memories or None,
            group_profile=engine.semantic_memory.get_group_profile(group_id),
            style_params=style_params,
            other_ai_names=engine._other_ai_names,
            user_profiles=delayed_user_profiles,
            biography_speaker=pending_bio.get("speaker_card"),
            biography_mentioned=pending_bio.get("mentioned_cards"),
            biography_confidence=pending_bio.get("confidence"),
            skill_registry=engine._skill_registry,
            plugin_registry=getattr(engine, '_plugin_registry', None),
            caller_is_developer=caller_is_developer,
            glossary_section=glossary,
            adapter_type=adapter_type,
            scene_description="群里的话题有了自然间隙，你决定插一句。",
        )

    def _inject_group_id_into_latest_reminder(self, group_id: str) -> None:
        """Attach group_id and adapter_type to reminders that lack them."""
        engine = self._engine
        if engine._skill_executor is None:
            return
        try:
            store = engine._skill_executor.get_data_store("reminder")
            reminders = list(store.get("reminders", []))
            if not reminders:
                return
            updated = False
            for r in reminders:
                if "group_id" not in r:
                    r["group_id"] = group_id
                    updated = True
                if "adapter_type" not in r:
                    r["adapter_type"] = engine._current_adapter_type
                    updated = True
            if updated:
                store.set("reminders", reminders)
                store.save()
        except Exception as exc:
            logger.warning("Failed to inject group_id into reminder: %s", exc)
