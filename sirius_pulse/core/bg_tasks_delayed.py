"""延迟队列相关后台任务。

包含延迟队列轮询、延迟响应处理等功能。
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from sirius_pulse.core.delayed_response_queue import _parse_iso
from sirius_pulse.core.events import SessionEvent, SessionEventType
from sirius_pulse.core.identity_resolver import IdentityContext
from sirius_pulse.core.prompt_factory import TAG_GLOSSARY, PromptFactory
from sirius_pulse.core.sticker_delivery import dedupe_sticker_names, defer_send_sticker_tool
from sirius_pulse.providers.base import ToolCall

if TYPE_CHECKING:
    from sirius_pulse.core.engine_core import _EmotionalGroupChatEngineBase

logger = logging.getLogger(__name__)

_AUTONOMOUS_MESSAGE_SKILLS = {
    "send_sticker",
    "pin_message",
    "unpin_message",
}


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
            # 使用 IdentityResolver 统一解析
            ctx = IdentityContext(
                speaker_name=item.user_id or "",
                platform_uid=item.channel_user_id,
                platform=item.channel,
            )
            resolution = engine.identity_resolver.resolve_with_alias(
                ctx, engine.user_manager, group_id
            )
            if resolution.user_id:
                resolved_uid = resolution.user_id
                caller_profile = engine.user_manager.get_user(resolved_uid, group_id)
        if caller_profile is None:
            # Fallback: search by user_id (nickname) across all groups
            ctx = IdentityContext(speaker_name=item.user_id or "")
            resolution = engine.identity_resolver.resolve_with_alias(
                ctx, engine.user_manager, group_id
            )
            if resolution.user_id:
                resolved_uid = resolution.user_id
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

        # Use ContextAssembler to build full messages with diary RAG + XML history
        diary_top_k = engine.config.get("diary_top_k", 5)
        diary_token_budget = engine.config.get("diary_token_budget", 800)

        # 获取当前发言者信息
        speaker_uid = resolved_uid or ""
        speaker_display = triggered[0].user_id if triggered else ""

        # 提取原始聊天内容用于日记检索，避免 XML 标签干扰
        raw_parts = [it.message_content for it in triggered if getattr(it, "message_content", None)]
        raw_chat_content = "\n".join(raw_parts) if raw_parts else bundle.user_content

        msgs, ca_breakdown = engine.context_assembler.build_messages_with_breakdown(
            group_id=group_id,
            current_query=bundle.user_content,
            system_prompt=bundle.system_prompt,
            search_query=raw_chat_content,
            diary_top_k=diary_top_k,
            diary_token_budget=diary_token_budget,
            include_pending=False,
            speaker_user_id=speaker_uid,
            speaker_name=speaker_display,
            content_is_tagged=True,
            pinned_messages=(
                engine.get_pinned_messages_for_prompt(group_id)
                if hasattr(engine, "get_pinned_messages_for_prompt")
                else []
            ),
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

        # Multi-round generation with function_call support
        from sirius_pulse.core.brain import ChatRequest
        from sirius_pulse.skills.models import SkillInvocationContext

        max_skill_rounds = engine.config.get("max_skill_rounds", 8)
        partial_replies: list[str] = []
        last_round_had_partial = False
        last_partial_sent_at: float | None = None
        _round = 0
        tool_calls: list[ToolCall] = []
        reply = ""
        chat_result: Any = None
        deferred_sticker_names: list[str] = []
        pending_chat_result: Any = None
        sticker_text_retry_used = False
        ended_because_max_rounds = False

        while True:
            if pending_chat_result is not None:
                chat_result = pending_chat_result
                pending_chat_result = None
            else:
                if _round > max_skill_rounds:
                    ended_because_max_rounds = bool(
                        tool_calls
                        and engine._skill_registry is not None
                        and engine._skill_executor is not None
                    )
                    break
                chat_result = await engine.brain.chat(
                    ChatRequest(
                        group_id=group_id,
                        user_id=item.user_id or "",
                        system_prompt=system_prompt,
                        messages=messages,
                        task_name="response_generate",
                        enable_skills=True,
                        caller_is_developer=caller_is_developer,
                        post_process=True,
                    )
                )
                _round += 1
            reply = chat_result.raw_text.strip()
            round_clean = chat_result.clean_text

            # 检查是否有 tool_calls
            tool_calls = chat_result.tool_calls or []
            if not tool_calls or engine._skill_registry is None or engine._skill_executor is None:
                break

            # Determine if every invoked skill is marked silent.
            all_silent = all(
                engine._skill_registry.get(tc.function_name) is not None
                and engine._skill_registry.get(tc.function_name).silent
                for tc in tool_calls
            )

            # 使用 chat_result 已由 hooks 处理好的 clean_text
            non_skill_text = round_clean
            last_round_had_partial = False
            if non_skill_text and not all_silent:
                engine._log_inner_thought(f"先跟用户回一声：{non_skill_text[:40]}...")
                last_round_had_partial = True
                if on_partial_reply is None:
                    raise RuntimeError(
                        "Tool execution requires on_partial_reply when partial text is present"
                    )
                await on_partial_reply(non_skill_text)
                last_partial_sent_at = time.monotonic()

            # Execute tool_calls and collect results
            from sirius_pulse.memory.user.unified_models import UnifiedUser

            caller_user_id = item.user_id
            skill_caller = UnifiedUser(
                user_id=caller_user_id,
                name=caller_profile.name if caller_profile else caller_user_id,
                metadata={"is_developer": caller_is_developer},
            )
            developer_profiles: list[UnifiedUser] = []
            group_entries = engine.user_manager.entries.get(group_id, {})
            for profile in group_entries.values():
                if profile.is_developer:
                    developer_profiles.append(profile)

            if engine._skill_executor is not None:
                engine._skill_executor.set_chat_context(
                    group_id=group_id, user_id=caller_user_id or ""
                )

            # 构造 assistant 消息（含 tool_calls）
            assistant_msg: dict[str, Any] = {
                "role": "assistant",
                "content": reply or None,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.function_name,
                            "arguments": tc.function_arguments,
                        },
                    }
                    for tc in tool_calls
                ],
            }
            messages.append(assistant_msg)

            # 逐个执行 tool_call 并收集结果
            skill_multimodal: list[dict[str, Any]] = []
            for idx, tc in enumerate(tool_calls):
                skill_name = tc.function_name
                try:
                    params = json.loads(tc.function_arguments) if tc.function_arguments else {}
                except json.JSONDecodeError:
                    params = {}
                    logger.warning(
                        "tool_call 参数解析失败: %s, arguments=%s", skill_name, tc.function_arguments
                    )

                skill = engine._skill_registry.get(skill_name)
                if skill is None:
                    err_msg = f"Skill '{skill_name}' not found"
                    logger.warning(err_msg)
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": err_msg})
                    continue

                if skill_name == "send_sticker":
                    names, tool_content = defer_send_sticker_tool(
                        params,
                        available_names=getattr(engine, "_sticker_names", []) or [],
                    )
                    deferred_sticker_names.extend(names)
                    messages.append(
                        {"role": "tool", "tool_call_id": tc.id, "content": tool_content}
                    )
                    continue

                # Engagement-based permission
                if (
                    caller_engagement < 0.1
                    and not caller_is_developer
                    and not self._is_autonomous_message_skill(skill)
                ):
                    err_msg = f"Skill '{skill_name}' 被拒绝：互动不足 (engagement={caller_engagement:.2f})"
                    logger.warning(err_msg)
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": err_msg})
                    continue

                if skill.developer_only and not caller_is_developer:
                    err_msg = f"Skill '{skill_name}' 被拒绝：caller 不是 developer"
                    logger.warning(err_msg)
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": err_msg})
                    continue

                ctx = SkillInvocationContext(  # type: ignore[assignment]
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
                        tool_content = result.to_display_text()
                        # 收集多模态内容
                        for block in result.multimodal_blocks:
                            skill_multimodal.append(
                                {"type": "image_url", "image_url": {"url": block.value}}
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
                        tool_content = result.error or "Unknown error"
                        logger.warning("SKILL '%s' 执行失败: %s", skill_name, tool_content)
                except Exception as exc:
                    tool_content = str(exc)
                    logger.error("SKILL '%s' 执行异常: %s", skill_name, exc)

                # 添加 tool 结果消息
                messages.append({"role": "tool", "tool_call_id": tc.id, "content": tool_content})

                # 链式调用中间增加延迟，避免回复过快
                if idx < len(tool_calls) - 1:
                    await asyncio.sleep(2)

            # If the model only picked a sticker, give it one text-only retry
            # without exposing the sticker tool again.
            if all_silent:
                only_sticker_calls = all(tc.function_name == "send_sticker" for tc in tool_calls)
                if only_sticker_calls and not non_skill_text and not sticker_text_retry_used:
                    sticker_text_retry_used = True
                    pending_chat_result = await engine.brain.chat(
                        ChatRequest(
                            group_id=group_id,
                            user_id=item.user_id or "",
                            system_prompt=(
                                system_prompt
                                + "\n\nYou already selected a sticker for this turn. "
                                "Now write the text reply only. Do not call send_sticker again."
                            ),
                            messages=messages,
                            task_name="response_generate",
                            enable_skills=True,
                            disabled_skill_names={"send_sticker"},
                            caller_is_developer=caller_is_developer,
                            post_process=True,
                        )
                    )
                    continue
                break

            # 如果有多模态内容，作为 user 消息注入
            if skill_multimodal:
                messages.append({"role": "user", "content": skill_multimodal})

        # If the loop ended because max rounds were exhausted and the last round
        # already sent a partial reply, don't duplicate that text as the final reply.
        if ended_because_max_rounds and last_round_had_partial:
            logger.debug(
                "Chain hit max_skill_rounds=%d; last partial already sent, "
                "clearing clean_reply to avoid duplication",
                max_skill_rounds,
            )
            reply = ""

        # 最终回复：hooks 已处理 pin/dedup/memory/timestamp
        if ended_because_max_rounds and last_round_had_partial:
            clean_reply = ""
        else:
            clean_reply = chat_result.clean_text if chat_result else ""

        # Determine return strategy
        from sirius_pulse.models.response_strategy import ResponseStrategy

        strategy = "delayed"
        if any(i.strategy_decision.strategy == ResponseStrategy.IMMEDIATE for i in triggered):
            strategy = "immediate"

        final_reply = clean_reply or (partial_replies[-1] if partial_replies else "")

        # Fast tools can finish before the client has had time to visually render
        # the partial reply. Keep a minimum lead window without delaying tool work.
        if final_reply and last_partial_sent_at is not None:
            try:
                lead_seconds = max(
                    0.0,
                    float(engine.config.get("partial_reply_lead_seconds", 1.5)),
                )
            except (TypeError, ValueError):
                lead_seconds = 1.5
            remaining = lead_seconds - (time.monotonic() - last_partial_sent_at)
            if remaining > 0:
                await asyncio.sleep(remaining)

        # 获取引用回复信息
        reply_references = chat_result.reply_references if chat_result else []
        sticker_names = dedupe_sticker_names(deferred_sticker_names)

        # Emit event with full reply data for external delivery
        await engine.event_bus.emit(
            SessionEvent(
                type=SessionEventType.DELAYED_RESPONSE_TRIGGERED,
                data={
                    "group_id": group_id,
                    "item_id": triggered[0].item_id,
                    "reply": final_reply,
                    "partial_replies": partial_replies,
                    "sticker_names": sticker_names,
                },
            )
        )

        return [
            {
                "strategy": strategy,
                "item_id": triggered[0].item_id,
                "reply": final_reply,
                "partial_replies": partial_replies,
                "reply_references": reply_references,
                "sticker_names": sticker_names,
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

        bundle = PromptFactory.assemble_chat(
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
            plugin_registry=getattr(engine, "_plugin_registry", None),
            caller_is_developer=caller_is_developer,
            adapter_type=adapter_type,
            sticker_names=getattr(engine, "_sticker_names", None),
            qq_mention_members=(
                engine.get_qq_group_members_for_prompt(group_id)
                if hasattr(engine, "get_qq_group_members_for_prompt")
                else []
            ),
        )
        if glossary:
            bundle.system_prompt = f"{bundle.system_prompt}\n\n{TAG_GLOSSARY}\n{glossary}"
        return bundle

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

    @staticmethod
    def _is_autonomous_message_skill(skill: Any) -> bool:
        """Return True for package built-ins that replace legacy prompt tags."""
        if getattr(skill, "name", "") not in _AUTONOMOUS_MESSAGE_SKILLS:
            return False
        source_path = getattr(skill, "source_path", None)
        if source_path is None:
            return False
        try:
            builtin_dir = (Path(__file__).resolve().parents[1] / "skills" / "builtin").resolve()
            return source_path.resolve().is_relative_to(builtin_dir)
        except Exception:
            return False
