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

from sirius_pulse.core.agent_turn import AgentTurn, AgentTurnPhase
from sirius_pulse.core.delayed_response_queue import _parse_iso
from sirius_pulse.core.events import SessionEvent, SessionEventType
from sirius_pulse.core.identity_resolver import IdentityContext
from sirius_pulse.core.plan_runtime import (
    consume_plan_events,
    finish_plan_session,
    format_public_plan_status,
    format_plan_events_for_model,
    get_active_plan_session,
    start_plan_session,
    update_plan_progress,
)
from sirius_pulse.core.prompt_factory import TAG_GLOSSARY, PromptFactory
from sirius_pulse.core.sticker_delivery import (
    dedupe_sticker_names,
    defer_interaction_sticker_tool,
)
from sirius_pulse.models.response_strategy import PersonaProfilePromptContext
from sirius_pulse.providers.base import ToolCall

if TYPE_CHECKING:
    from sirius_pulse.core.engine_core import _EmotionalGroupChatEngineBase

logger = logging.getLogger(__name__)

_AUTONOMOUS_MESSAGE_SKILLS = {
    "chat_with_developer",
}


def _composite_action(tool_call: ToolCall) -> str:
    """Return the effective action for a legacy or unified tool call."""
    if tool_call.function_name not in {"interaction", "file_upload"}:
        return ""
    try:
        params = json.loads(tool_call.function_arguments or "{}")
    except json.JSONDecodeError:
        return ""
    return str(params.get("action", "")).strip().lower()


def _is_sticker_tool_call(tool_call: ToolCall) -> bool:
    return _composite_action(tool_call) == "sticker"

# ── 内置流程控制工具定义 ──────────────────────────────────────────────

STOP_TOOL_DEF: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "stop",
        "description": ("结束本轮回复。你的最后一条文字消息会发送给用户，然后本轮回复结束。"),
        "parameters": {
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "description": "固定为 stop",
                    "enum": ["stop"],
                }
            },
            "required": ["action"],
        },
    },
}

ENTER_PLAN_TOOL_DEF: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "enter_plan",
        "description": (
            "Enter hidden planning mode for a complex request. "
            "Use this when the task needs multiple tool calls or careful background work. "
            "Intermediate text in planning mode is private; call exit_plan to send the final message."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "goal": {
                    "type": "string",
                    "description": "The concrete goal for the hidden planning session.",
                },
                "reason": {
                    "type": "string",
                    "description": "Short reason why planning mode is needed.",
                },
            },
            "required": ["goal"],
        },
    },
}

EXIT_PLAN_TOOL_DEF: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "exit_plan",
        "description": (
            "Exit hidden planning mode and optionally send exactly one final message to the chat."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "final_message": {
                    "type": "string",
                    "description": "The final visible message to send to the chat.",
                },
                "send_to_group": {
                    "type": "boolean",
                    "description": "Whether the final_message should be sent.",
                    "default": True,
                },
                "summary": {
                    "type": "string",
                    "description": "Private execution summary for logs.",
                },
            },
            "required": ["final_message"],
        },
    },
}

ABORT_PLAN_TOOL_DEF: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "abort_plan",
        "description": (
            "Abort hidden planning mode when the task should not continue, was cancelled, "
            "or cannot be completed safely."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "reason": {
                    "type": "string",
                    "description": "Private reason for aborting the plan.",
                },
                "message": {
                    "type": "string",
                    "description": "Optional visible message to send to the chat.",
                },
                "send_to_group": {
                    "type": "boolean",
                    "description": "Whether the optional message should be sent.",
                    "default": False,
                },
            },
        },
    },
}

UPDATE_PLAN_PROGRESS_TOOL_DEF: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "update_plan_progress",
        "description": (
            "Update the public, sanitized progress snapshot for the active hidden plan. "
            "This does not send a chat message and must not include private reasoning, "
            "tool results, secrets, or pending message text."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "phase": {
                    "type": "string",
                    "description": "Short public phase label, e.g. searching/analyzing/verifying.",
                },
                "summary": {
                    "type": "string",
                    "description": "Brief public progress summary safe for normal chat awareness.",
                },
                "confidence": {
                    "type": "string",
                    "enum": ["low", "medium", "high"],
                    "description": "Public confidence in current progress.",
                },
                "visible": {
                    "type": "boolean",
                    "description": "Whether the public snapshot may be shown to normal chat.",
                    "default": True,
                },
            },
        },
    },
}

GET_PLAN_STATUS_TOOL_DEF: dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "get_plan_status",
        "description": (
            "Read the public status snapshot for the active hidden plan in this group. "
            "Only public progress is returned; private reasoning and tool details are never exposed."
        ),
        "parameters": {
            "type": "object",
            "properties": {},
        },
    },
}

FLOW_CONTROL_TOOL_NAMES = {"stop"}
PLAN_CONTROL_TOOL_NAMES = {
    "enter_plan",
    "exit_plan",
    "abort_plan",
    "update_plan_progress",
    "get_plan_status",
}


class DelayedQueueTasks:
    """延迟队列相关任务组件。"""

    def __init__(self, engine: _EmotionalGroupChatEngineBase) -> None:
        self._engine = engine

    @staticmethod
    async def _emit_agent_turn(engine: Any, turn: AgentTurn) -> None:
        await engine.event_bus.emit(
            SessionEvent(type=SessionEventType.AGENT_TURN_UPDATED, data=turn.to_event_data())
        )

    @staticmethod
    def _side_effect_name(skill: Any) -> str:
        value = getattr(skill, "side_effect", "unknown")
        return str(getattr(value, "value", value) or "unknown")

    @staticmethod
    def _resolve_identity_with_optional_profile_manager(
        engine: Any,
        ctx: IdentityContext,
        group_id: str,
    ) -> Any:
        try:
            return engine.identity_resolver.resolve_with_alias(
                ctx,
                engine.user_manager,
                group_id,
                profile_manager=getattr(engine, "profile_manager", None),
            )
        except TypeError:
            return engine.identity_resolver.resolve_with_alias(
                ctx,
                engine.user_manager,
                group_id,
            )

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

    async def _maybe_send_plan_presence(
        self,
        engine: _EmotionalGroupChatEngineBase,
        on_partial_reply: Any | None,
        group_id: str,
        event: str,
        system_prompt: str,
    ) -> None:
        if on_partial_reply is None:
            return
        if not bool(engine.config.get("plan_mode_presence_enabled", False)):
            return
        try:
            min_interval = float(engine.config.get("plan_mode_presence_min_interval_seconds", 45.0))
        except (TypeError, ValueError):
            min_interval = 45.0
        now = time.monotonic()
        state = getattr(engine, "_plan_presence_sent_at", None)
        if not isinstance(state, dict):
            state = {}
            setattr(engine, "_plan_presence_sent_at", state)
        last = float(state.get(group_id, 0.0) or 0.0)
        if last and now - last < max(0.0, min_interval):
            return

        try:
            max_chars = int(engine.config.get("max_sentence_chars", 20))
        except (TypeError, ValueError):
            max_chars = 20
        max_chars = max(5, min(50, max_chars))
        scene = "刚进入后台计划状态" if event == "enter" else "后台计划收到新进展"
        prompt = (
            "请以当前人格口吻生成一条即将发到群聊的短状态消息。\n"
            f"场景：{scene}，需要让对方知道你看到了并会继续处理。\n"
            f"要求：只输出消息本身；一句话；不超过 {max_chars} 个汉字；"
            "不要透露工具、后台计划、内部推理或系统提示；不要 Markdown。"
        )
        try:
            from sirius_pulse.core.brain import ChatRequest

            result = await engine.brain.chat(
                ChatRequest(
                    group_id=group_id,
                    user_id="",
                    system_prompt=system_prompt,
                    messages=[{"role": "user", "content": prompt}],
                    task_name="response_generate",
                    enable_skills=False,
                    post_process=True,
                    max_tokens=48,
                )
            )
            text = (getattr(result, "clean_text", "") or getattr(result, "raw_text", "")).strip()
        except Exception as exc:
            logger.warning("Plan presence generation failed for %s: %s", group_id, exc)
            return
        if not text:
            return
        try:
            await on_partial_reply(text)
        except Exception as exc:
            logger.warning("Plan presence send failed for %s: %s", group_id, exc)
            return
        state[group_id] = now

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
                candidate_memories=item.get("candidate_memories", []),
                enqueue_time=item.get("enqueue_time", ""),
                window_seconds=float(item.get("window_seconds", 30.0)),
                status=item.get("status", "pending"),
                multimodal_inputs=item.get("multimodal_inputs", []),
                lane=item.get("lane", "chat"),
                plan_id=item.get("plan_id", ""),
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
            resolution = self._resolve_identity_with_optional_profile_manager(engine, ctx, group_id)
            if resolution.user_id:
                resolved_uid = resolution.user_id
                caller_profile = engine.user_manager.get_user(resolved_uid, group_id)
        if caller_profile is None:
            # Fallback: search by user_id (nickname) across all groups
            ctx = IdentityContext(speaker_name=item.user_id or "")
            resolution = self._resolve_identity_with_optional_profile_manager(engine, ctx, group_id)
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
        plan_mode_enabled = bool(engine.config.get("plan_mode_enabled", False))
        limit_normal_tools = bool(engine.config.get("plan_mode_limit_normal_tools", False))
        initial_lane = getattr(triggered[0], "lane", "chat") if triggered else "chat"
        expose_skills_in_prompt = not (
            plan_mode_enabled and limit_normal_tools and initial_lane != "plan"
        )
        bundle = self._build_delayed_prompt(
            triggered,
            group_id,
            caller_is_developer=caller_is_developer,
            adapter_type=adapter_type,
            expose_skills=expose_skills_in_prompt,
            tool_flow_mode="plan" if initial_lane == "plan" else "chat",
        )
        active_plan_for_chat = (
            get_active_plan_session(engine, group_id)
            if plan_mode_enabled and initial_lane != "plan"
            else None
        )
        if active_plan_for_chat is not None and bool(
            engine.config.get("plan_mode_chat_awareness_enabled", False)
        ):
            bundle.system_prompt = (
                f"{bundle.system_prompt}\n\n" f"{format_public_plan_status(active_plan_for_chat)}"
            )
        if (
            plan_mode_enabled
            and limit_normal_tools
            and initial_lane != "plan"
            and not getattr(engine, "_active_plan_sessions", {}).get(group_id)
        ):
            bundle.system_prompt = (
                f"{bundle.system_prompt}\n\n"
                "【计划模式】普通聊天阶段只做轻量可见回复。"
                "如果请求需要复杂工具、多步确认或较长推理，请调用 enter_plan。"
                "enter_plan 后的中间内容不会发送到群里，完成后用 exit_plan 给出最终消息。"
            )

        # Use ContextAssembler to build full messages with diary RAG + XML history
        diary_top_k = engine.config.get("diary_top_k", 5)
        memory_unit_top_k = engine.config.get("memory_unit_top_k", diary_top_k)
        diary_token_budget = engine.config.get("diary_token_budget", 800)

        # 获取当前发言者信息
        speaker_uid = resolved_uid or ""
        speaker_display = triggered[0].user_id if triggered else ""

        # 提取原始聊天内容用于日记检索，避免 XML 标签干扰
        raw_parts = [
            text
            for triggered_item in triggered
            for text in PromptFactory._extract_message_texts(
                getattr(triggered_item, "message_content", "")
            )
        ]
        raw_chat_content = "\n".join(raw_parts) if raw_parts else bundle.user_content
        agent_turn = AgentTurn(
            group_id=group_id,
            item_ids=[triggered_item.item_id for triggered_item in triggered],
            query=raw_chat_content,
        )
        await self._emit_agent_turn(engine, agent_turn)
        try:
            agent_max_skill_candidates = max(
                1, int(engine.config.get("agent_max_skill_candidates", 8))
            )
        except (TypeError, ValueError):
            agent_max_skill_candidates = 8

        msgs, ca_breakdown = engine.context_assembler.build_messages_with_breakdown(
            group_id=group_id,
            current_query=bundle.user_content,
            system_prompt=bundle.system_prompt,
            search_query=raw_chat_content,
            diary_top_k=diary_top_k,
            memory_unit_top_k=memory_unit_top_k,
            diary_token_budget=diary_token_budget,
            include_pending=False,
            speaker_user_id=speaker_uid,
            speaker_name=speaker_display,
            content_is_tagged=True,
            dynamic_context=bundle.dynamic_context,
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
        from sirius_pulse.skills.models import SkillInvocationContext, SkillResult

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
        plan_mode_enabled = bool(engine.config.get("plan_mode_enabled", False))
        limit_normal_tools = bool(engine.config.get("plan_mode_limit_normal_tools", False))
        plan_mode = getattr(item, "lane", "chat") == "plan"
        plan_session: Any | None = None
        plan_final_reply: str | None = None
        plan_send_to_group = True

        while True:
            if plan_mode and plan_session is None:
                plan_session = get_active_plan_session(engine, group_id)
            if plan_mode and plan_session is not None:
                if getattr(plan_session, "status", "active") != "active":
                    plan_final_reply = ""
                    plan_send_to_group = False
                    break
                event_text = format_plan_events_for_model(consume_plan_events(plan_session))
                if event_text:
                    messages.append({"role": "user", "content": event_text})
                    await self._maybe_send_plan_presence(
                        engine,
                        on_partial_reply,
                        group_id,
                        "update",
                        system_prompt,
                    )

            # 内置流程控制工具
            _extra_tools = [STOP_TOOL_DEF]
            if plan_mode_enabled:
                if plan_mode:
                    _extra_tools = [
                        UPDATE_PLAN_PROGRESS_TOOL_DEF,
                        EXIT_PLAN_TOOL_DEF,
                        ABORT_PLAN_TOOL_DEF,
                    ]
                elif not getattr(engine, "_active_plan_sessions", {}).get(group_id):
                    _extra_tools.append(ENTER_PLAN_TOOL_DEF)
                else:
                    _extra_tools.append(GET_PLAN_STATUS_TOOL_DEF)
            enable_skills_for_round = bool(engine.config.get("enable_skills", True))
            if plan_mode_enabled and limit_normal_tools and not plan_mode:
                enable_skills_for_round = False

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
                agent_turn.advance(AgentTurnPhase.DECIDE)
                await self._emit_agent_turn(engine, agent_turn)
                chat_result = await engine.brain.chat(
                    ChatRequest(
                        group_id=group_id,
                        user_id=item.user_id or "",
                        system_prompt=system_prompt,
                        messages=messages,
                        task_name="response_generate",
                        enable_skills=enable_skills_for_round,
                        caller_is_developer=caller_is_developer,
                        post_process=True,
                        extra_tools=_extra_tools,
                        skill_query=agent_turn.query,
                        max_skill_candidates=agent_max_skill_candidates,
                    )
                )
                _round += 1
            reply = chat_result.raw_text.strip()
            round_clean = chat_result.clean_text
            agent_turn.set_candidates(getattr(chat_result, "injected_tool_names", []))

            # 分类工具调用：流程控制 vs 普通技能
            tool_calls = chat_result.tool_calls or []
            flow_control = [tc for tc in tool_calls if tc.function_name in FLOW_CONTROL_TOOL_NAMES]
            plan_control = [tc for tc in tool_calls if tc.function_name in PLAN_CONTROL_TOOL_NAMES]
            regular_tools = [
                tc
                for tc in tool_calls
                if tc.function_name not in FLOW_CONTROL_TOOL_NAMES
                and tc.function_name not in PLAN_CONTROL_TOOL_NAMES
            ]
            agent_turn.advance(
                AgentTurnPhase.PLAN if regular_tools or plan_control else AgentTurnPhase.RESPOND
            )
            await self._emit_agent_turn(engine, agent_turn)

            # 没调用任何工具 → 隐式 stop，文本作为最终回复
            if not tool_calls:
                agent_turn.advance(AgentTurnPhase.RESPOND)
                await self._emit_agent_turn(engine, agent_turn)
                if plan_mode:
                    plan_final_reply = chat_result.clean_text
                    plan_send_to_group = bool(plan_final_reply)
                    if plan_session is not None:
                        finish_plan_session(engine, group_id)
                break

            should_stop = False
            for tc in flow_control:
                try:
                    fc_params = json.loads(tc.function_arguments) if tc.function_arguments else {}
                except json.JSONDecodeError:
                    fc_params = {}
                action = fc_params.get("action", "stop")
                if action == "stop":
                    should_stop = True

            enter_plan_tc = next(
                (tc for tc in plan_control if tc.function_name == "enter_plan"), None
            )
            if enter_plan_tc and plan_mode_enabled and not plan_mode:
                try:
                    plan_params = (
                        json.loads(enter_plan_tc.function_arguments)
                        if enter_plan_tc.function_arguments
                        else {}
                    )
                except json.JSONDecodeError:
                    plan_params = {}
                goal = str(plan_params.get("goal") or raw_chat_content or bundle.user_content)
                reason = str(plan_params.get("reason") or "")
                plan_session = start_plan_session(
                    engine,
                    group_id=group_id,
                    owner_user_id=item.user_id or "",
                    goal=goal,
                    reason=reason,
                )
                plan_mode = True
                messages.append(
                    {
                        "role": "assistant",
                        "content": reply or None,
                        "tool_calls": [
                            {
                                "id": enter_plan_tc.id,
                                "type": "function",
                                "function": {
                                    "name": "enter_plan",
                                    "arguments": enter_plan_tc.function_arguments or "{}",
                                },
                            }
                        ],
                    }
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": enter_plan_tc.id,
                        "content": (
                            "Planning mode is now active. Do not send intermediate text. "
                            "Use available tools privately. Optionally call update_plan_progress "
                            "with a public-safe status snapshot, then call exit_plan or abort_plan."
                        ),
                    }
                )
                system_prompt = (
                    f"{system_prompt}\n\n"
                    "【隐藏计划模式】你现在处于后台计划模式。中间文本不会发送到群里；"
                    "可以私下调用可用工具并处理计划事件。完成时必须调用 exit_plan，"
                    "需要放弃或无法完成时调用 abort_plan。不要调用 continue 或 stop。"
                )
                engine._log_inner_thought(f"进入计划模式: {goal[:60]}")
                await self._maybe_send_plan_presence(
                    engine,
                    on_partial_reply,
                    group_id,
                    "enter",
                    system_prompt,
                )
                continue

            get_plan_status_tc = next(
                (tc for tc in plan_control if tc.function_name == "get_plan_status"), None
            )
            if get_plan_status_tc and plan_mode_enabled and not plan_mode:
                active_session = get_active_plan_session(engine, group_id)
                status_text = (
                    format_public_plan_status(active_session)
                    if active_session is not None
                    else "No active hidden planning session in this group."
                )
                messages.append(
                    {
                        "role": "assistant",
                        "content": reply or None,
                        "tool_calls": [
                            {
                                "id": get_plan_status_tc.id,
                                "type": "function",
                                "function": {
                                    "name": "get_plan_status",
                                    "arguments": get_plan_status_tc.function_arguments or "{}",
                                },
                            }
                        ],
                    }
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": get_plan_status_tc.id,
                        "content": (
                            f"{status_text}\n"
                            "Answer naturally if the user asked about progress. "
                            "Do not call get_plan_status again unless new status is needed."
                        ),
                    }
                )
                continue

            exit_plan_tc = next(
                (tc for tc in plan_control if tc.function_name == "exit_plan"), None
            )
            if exit_plan_tc and plan_mode:
                try:
                    plan_params = (
                        json.loads(exit_plan_tc.function_arguments)
                        if exit_plan_tc.function_arguments
                        else {}
                    )
                except json.JSONDecodeError:
                    plan_params = {}
                plan_final_reply = str(plan_params.get("final_message") or "").strip()
                plan_send_to_group = bool(plan_params.get("send_to_group", True))
                if plan_session is not None:
                    finish_plan_session(engine, group_id)
                engine._log_inner_thought(
                    "计划模式结束，准备发送最终回复"
                    if plan_send_to_group
                    else "计划模式结束，不发送群消息"
                )
                break

            abort_plan_tc = next(
                (tc for tc in plan_control if tc.function_name == "abort_plan"), None
            )
            if abort_plan_tc and plan_mode:
                try:
                    plan_params = (
                        json.loads(abort_plan_tc.function_arguments)
                        if abort_plan_tc.function_arguments
                        else {}
                    )
                except json.JSONDecodeError:
                    plan_params = {}
                plan_final_reply = str(plan_params.get("message") or "").strip()
                plan_send_to_group = bool(plan_params.get("send_to_group", False))
                if plan_session is not None:
                    finish_plan_session(engine, group_id, status="aborted")
                reason = str(plan_params.get("reason") or "").strip()
                engine._log_inner_thought(f"计划模式中止: {reason[:80]}")
                break

            # 1. 发送当前轮次的文字（排除 flow control 工具，只看普通技能是否全 silent）
            update_progress_tc = next(
                (tc for tc in plan_control if tc.function_name == "update_plan_progress"), None
            )
            if update_progress_tc and plan_mode:
                if plan_session is None:
                    plan_session = get_active_plan_session(engine, group_id)
                try:
                    progress_params = (
                        json.loads(update_progress_tc.function_arguments)
                        if update_progress_tc.function_arguments
                        else {}
                    )
                except json.JSONDecodeError:
                    progress_params = {}
                if plan_session is not None:
                    update_plan_progress(
                        plan_session,
                        phase=str(progress_params.get("phase") or ""),
                        summary=str(progress_params.get("summary") or ""),
                        confidence=str(progress_params.get("confidence") or ""),
                        visible=(
                            bool(progress_params["visible"])
                            if "visible" in progress_params
                            else None
                        ),
                    )
                    tool_content = "Public planning progress updated."
                else:
                    tool_content = "No active hidden planning session to update."
                messages.append(
                    {
                        "role": "assistant",
                        "content": reply or None,
                        "tool_calls": [
                            {
                                "id": update_progress_tc.id,
                                "type": "function",
                                "function": {
                                    "name": "update_plan_progress",
                                    "arguments": update_progress_tc.function_arguments or "{}",
                                },
                            }
                        ],
                    }
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": update_progress_tc.id,
                        "content": tool_content,
                    }
                )
                continue

            def _tool_is_silent(tool_call: ToolCall) -> bool:
                skill = (
                    engine._skill_registry.get(tool_call.function_name)
                    if engine._skill_registry is not None
                    else None
                )
                if skill is None:
                    return False
                if tool_call.function_name == "interaction":
                    return _composite_action(tool_call) == "sticker"
                if tool_call.function_name == "file_upload":
                    return _composite_action(tool_call) == "image"
                return skill.silent

            all_silent = bool(regular_tools) and all(_tool_is_silent(tc) for tc in regular_tools)

            non_skill_text = round_clean
            last_round_had_partial = False
            if plan_mode and non_skill_text:
                engine._log_inner_thought(f"计划模式中间文本已隐藏: {non_skill_text[:40]}...")
            elif non_skill_text and not all_silent and not should_stop:
                engine._log_inner_thought(f"先跟用户回一声：{non_skill_text[:40]}...")
                last_round_had_partial = True
                if on_partial_reply is None:
                    raise RuntimeError(
                        "Tool execution requires on_partial_reply when partial text is present"
                    )
                await on_partial_reply(non_skill_text)
                last_partial_sent_at = time.monotonic()

            # 2. 执行普通工具（stop 以外的技能）
            skill_multimodal: list[dict[str, Any]] = []
            if (
                regular_tools
                and engine._skill_registry is not None
                and engine._skill_executor is not None
            ):
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

                engine._skill_executor.set_chat_context(
                    group_id=group_id, user_id=caller_user_id or ""
                )

                # 构造 assistant 消息（含普通工具的 tool_calls）
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
                        for tc in regular_tools
                    ],
                }
                messages.append(assistant_msg)

                try:
                    skill_timeout = max(
                        0.0, float(engine.config.get("skill_execution_timeout", 30.0))
                    )
                except (TypeError, ValueError):
                    skill_timeout = 30.0

                # 逐个执行 tool_call 并收集结果
                for idx, tc in enumerate(regular_tools):
                    skill_name = tc.function_name
                    try:
                        params = json.loads(tc.function_arguments) if tc.function_arguments else {}
                    except json.JSONDecodeError:
                        params = {}
                        logger.warning(
                            "tool_call 参数解析失败: %s, arguments=%s",
                            skill_name,
                            tc.function_arguments,
                        )

                    skill = engine._skill_registry.get(skill_name)
                    if skill is None:
                        err_msg = f"Skill '{skill_name}' not found"
                        logger.warning(err_msg)
                        messages.append({"role": "tool", "tool_call_id": tc.id, "content": err_msg})
                        continue

                    side_effect = self._side_effect_name(skill)

                    if _is_sticker_tool_call(tc):
                        if not agent_turn.begin_action(
                            tool_call_id=tc.id,
                            skill_name=skill_name,
                            params=params,
                            side_effect=side_effect,
                            deduplicate=side_effect != "read_only",
                        ):
                            err_msg = f"Skill '{skill_name}' 被拒绝：本轮相同副作用动作已经执行过。"
                            messages.append({"role": "tool", "tool_call_id": tc.id, "content": err_msg})
                            continue
                        agent_turn.advance(AgentTurnPhase.ACT)
                        await self._emit_agent_turn(engine, agent_turn)
                        names, tool_content = defer_interaction_sticker_tool(
                            params,
                            available_names=getattr(engine, "_sticker_names", []) or [],
                        )
                        deferred_sticker_names.extend(names)
                        agent_turn.finish_action(tc.id, success=True, summary=tool_content)
                        agent_turn.advance(AgentTurnPhase.VERIFY)
                        await self._emit_agent_turn(engine, agent_turn)
                        messages.append(
                            {"role": "tool", "tool_call_id": tc.id, "content": tool_content}
                        )
                        continue

                    # Engagement-based permission
                    if (
                        caller_engagement < 0.1
                        and not caller_is_developer
                        and not self._is_autonomous_message_skill(skill, params)
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

                    if not agent_turn.begin_action(
                        tool_call_id=tc.id,
                        skill_name=skill_name,
                        params=params,
                        side_effect=side_effect,
                        deduplicate=side_effect != "read_only",
                    ):
                        err_msg = f"Skill '{skill_name}' 被拒绝：本轮相同副作用动作已经执行过。"
                        await self._emit_agent_turn(engine, agent_turn)
                        messages.append({"role": "tool", "tool_call_id": tc.id, "content": err_msg})
                        continue

                    agent_turn.advance(AgentTurnPhase.ACT)
                    await self._emit_agent_turn(engine, agent_turn)

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
                            skill,
                            params,
                            timeout=skill_timeout,
                            invocation_context=ctx,
                            max_retries=2 if getattr(skill, "retry_safe", False) else 0,
                        )
                        agent_turn.finish_action(
                            tc.id,
                            success=result.success,
                            summary=result.error if not result.success else result.to_display_text(),
                        )
                        agent_turn.advance(AgentTurnPhase.VERIFY)
                        await self._emit_agent_turn(engine, agent_turn)
                        logger.info(
                            "Skill execute success: %s -> %s",
                            skill_name,
                            "success" if result.success else "failed",
                        )
                        tool_content = result.to_model_text()
                        if result.success:
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
                                        GlossaryTerm(
                                            term=term, definition=definition, source="skill"
                                        ),
                                    )
                            # Inject group_id into newly created reminders
                            if (
                                skill_name == "reminder"
                                and params.get("action", "").strip().lower() == "create"
                            ):
                                self._inject_group_id_into_latest_reminder(group_id)
                        else:
                            logger.warning(
                                "SKILL '%s' 执行失败: %s", skill_name, result.error or "Unknown error"
                            )
                    except Exception as exc:
                        tool_content = SkillResult(success=False, error=str(exc)).to_model_text()
                        agent_turn.finish_action(tc.id, success=False, summary=str(exc))
                        agent_turn.advance(AgentTurnPhase.VERIFY)
                        await self._emit_agent_turn(engine, agent_turn)
                        logger.error("SKILL '%s' 执行异常: %s", skill_name, exc)

                    # 添加 tool 结果消息
                    messages.append(
                        {"role": "tool", "tool_call_id": tc.id, "content": tool_content}
                    )

                    # 链式调用中间增加延迟，避免回复过快
                    if idx < len(regular_tools) - 1:
                        await asyncio.sleep(2)

            # 3. 处理 flow control：stop
            stop_tc = next((tc for tc in flow_control if tc.function_name == "stop"), None)

            # 注入 stop 的 assistant 消息并退出
            if stop_tc:
                messages.append(
                    {
                        "role": "assistant",
                        "content": reply or None,
                        "tool_calls": [
                            {
                                "id": stop_tc.id,
                                "type": "function",
                                "function": {
                                    "name": "stop",
                                    "arguments": '{"action":"stop"}',
                                },
                            }
                        ],
                    }
                )
                break

            # sticker-only 兜底：给模型一次纯文字重试机会
            if all_silent:
                only_sticker_calls = all(_is_sticker_tool_call(tc) for tc in regular_tools)
                if only_sticker_calls and not non_skill_text and not sticker_text_retry_used:
                    sticker_text_retry_used = True
                    pending_chat_result = await engine.brain.chat(
                        ChatRequest(
                            group_id=group_id,
                            user_id=item.user_id or "",
                            system_prompt=(
                                system_prompt + "\n\nYou already selected a sticker for this turn. "
                                "Now write the text reply only. Do not call the sticker interaction again."
                            ),
                            messages=messages,
                            task_name="response_generate",
                            enable_skills=True,
                            disabled_skill_names={tc.function_name for tc in regular_tools},
                            caller_is_developer=caller_is_developer,
                            post_process=True,
                            extra_tools=_extra_tools,
                            skill_query=agent_turn.query,
                            max_skill_candidates=agent_max_skill_candidates,
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

        if plan_mode and plan_session is not None and plan_final_reply is None:
            finish_plan_session(engine, group_id, status="aborted")
            plan_final_reply = clean_reply if clean_reply else ""
            plan_send_to_group = bool(plan_final_reply)

        # Determine return strategy
        from sirius_pulse.models.response_strategy import ResponseStrategy

        strategy = "delayed"
        if any(i.strategy_decision.strategy == ResponseStrategy.IMMEDIATE for i in triggered):
            strategy = "immediate"

        if plan_final_reply is not None:
            final_reply = plan_final_reply if plan_send_to_group else ""
        else:
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
        agent_turn.advance(AgentTurnPhase.RESPOND)
        await self._emit_agent_turn(engine, agent_turn)
        agent_turn.advance(AgentTurnPhase.COMPLETE)
        await self._emit_agent_turn(engine, agent_turn)
        await engine.event_bus.emit(
            SessionEvent(
                type=SessionEventType.DELAYED_RESPONSE_TRIGGERED,
                data={
                    "group_id": group_id,
                    "item_id": triggered[0].item_id,
                    "reply": final_reply,
                    "partial_replies": partial_replies,
                    "sticker_names": sticker_names,
                    "agent_turn_id": agent_turn.turn_id,
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
                "agent_turn_id": agent_turn.turn_id,
            }
        ]

    def _build_delayed_prompt(
        self,
        items: Any,
        group_id: str,
        caller_is_developer: bool = False,
        adapter_type: str | None = None,
        expose_skills: bool = True,
        tool_flow_mode: str = "chat",
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

        try:
            max_sentence_chars = int(engine.config.get("max_sentence_chars", 20))
        except (TypeError, ValueError):
            max_sentence_chars = 20
        style_params = engine.style_adapter.adapt(
            pace="decelerating",
            persona=engine.persona,
            max_sentence_chars=max_sentence_chars,
        )

        # 仅使用队列项自带的人物传记快照，避免回读引擎共享状态
        bio_ctx = self._merge_persona_profile_contexts(items)

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
            persona_profile_speaker=bio_ctx.speaker_card,
            persona_profile_mentioned=list(bio_ctx.mentioned_cards),
            persona_profile_confidence=dict(bio_ctx.confidence),
            skill_registry=engine._skill_registry if expose_skills else None,
            plugin_registry=getattr(engine, "_plugin_registry", None),
            caller_is_developer=caller_is_developer,
            adapter_type=adapter_type,
            sticker_names=getattr(engine, "_sticker_names", None),
            qq_mention_members=(
                engine.get_qq_group_members_for_prompt(group_id)
                if hasattr(engine, "get_qq_group_members_for_prompt")
                else []
            ),
            tool_flow_mode=tool_flow_mode,
        )
        dynamic_parts: list[str] = []
        if glossary:
            dynamic_parts.append(f"{TAG_GLOSSARY}\n{glossary}")

        if dynamic_parts:
            bundle.dynamic_context = (
                f"{bundle.dynamic_context}\n\n" + "\n\n".join(dynamic_parts)
                if bundle.dynamic_context
                else "\n\n".join(dynamic_parts)
            )

        return bundle

    @staticmethod
    def _merge_persona_profile_contexts(items: list[Any]) -> PersonaProfilePromptContext:
        """合并队列项中携带的人物传记快照。"""
        speaker_card: Any | None = None
        mentioned_cards: list[Any] = []
        confidence: dict[str, float] = {}

        for item in items:
            ctx: PersonaProfilePromptContext | None = getattr(item, "persona_profile_context", None)
            if ctx is None:
                continue
            if ctx.speaker_card is not None:
                speaker_card = ctx.speaker_card
            for card in ctx.mentioned_cards or []:
                if card is not None:
                    mentioned_cards.append(card)
            for alias, score in (ctx.confidence or {}).items():
                confidence[alias] = max(confidence.get(alias, 0.0), float(score))

        return PersonaProfilePromptContext(
            speaker_card=speaker_card,
            mentioned_cards=mentioned_cards,
            confidence=confidence,
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

    @staticmethod
    def _is_autonomous_message_skill(skill: Any, params: dict[str, Any] | None = None) -> bool:
        """Return True for package built-ins that replace legacy prompt tags."""
        skill_name = getattr(skill, "name", "")
        if skill_name == "interaction":
            if str((params or {}).get("action", "")).strip().lower() != "sticker":
                return False
        if skill_name not in _AUTONOMOUS_MESSAGE_SKILLS:
            if skill_name != "interaction":
                return False
        source_path = getattr(skill, "source_path", None)
        if source_path is None:
            return False
        try:
            builtin_dir = (Path(__file__).resolve().parents[1] / "skills" / "builtin").resolve()
            return source_path.resolve().is_relative_to(builtin_dir)
        except Exception:
            return False
