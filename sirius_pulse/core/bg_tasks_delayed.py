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
from sirius_pulse.core.constants import DEFAULT_BASIC_MEMORY_HISTORY_TOKEN_BUDGET
from sirius_pulse.core.delayed_response_queue import _parse_iso
from sirius_pulse.core.events import SessionEvent, SessionEventType
from sirius_pulse.core.identity_resolver import IdentityContext
from sirius_pulse.core.plan_runtime import (
    consume_plan_events,
    finish_plan_session,
    format_plan_events_for_model,
    format_public_plan_status,
    get_active_plan_session,
    start_plan_session,
    update_plan_progress,
)
from sirius_pulse.core.prompt_factory import PromptFactory
from sirius_pulse.core.sticker_delivery import dedupe_sticker_names
from sirius_pulse.providers.base import ToolCall
from sirius_pulse.tools.builtin._internal import _markdown_image

if TYPE_CHECKING:
    from sirius_pulse.core.engine_core import _EmotionalGroupChatEngineBase

logger = logging.getLogger(__name__)

_AUTONOMOUS_MESSAGE_TOOLS = {
    "interaction_with_master",
}


def _tool_action(tool_call: ToolCall) -> str:
    """Return the action for a tool call that dispatches multiple operations."""
    if tool_call.function_name not in {"group_file_exec", "interaction_with_master"}:
        return ""
    try:
        params = json.loads(tool_call.function_arguments or "{}")
    except json.JSONDecodeError:
        return ""
    return str(params.get("action", "")).strip().lower()


def _composite_action(tool_call: ToolCall) -> str:
    """Return the effective action for a composite file tool call."""
    if tool_call.function_name != "group_file_exec":
        return ""
    return _tool_action(tool_call)


def _build_assistant_tool_message(
    reply: str,
    tool_calls: list[ToolCall],
    reasoning_content: str = "",
) -> dict[str, Any]:
    """Build the assistant message that precedes the matching tool results."""
    message: dict[str, Any] = {
        "role": "assistant",
        "content": reply or None,
        "tool_calls": [
            {
                "id": tool_call.id,
                "type": tool_call.type or "function",
                "function": {
                    "name": tool_call.function_name,
                    "arguments": tool_call.function_arguments,
                },
            }
            for tool_call in tool_calls
        ],
    }
    if reasoning_content:
        message["reasoning_content"] = reasoning_content
    return message


def _reasoning_memory_kwargs(chat_result: Any) -> dict[str, str]:
    reasoning_content = str(getattr(chat_result, "reasoning_content", "") or "").strip()
    return {"reasoning_content": reasoning_content} if reasoning_content else {}


# ── 内置流程控制工具定义 ──────────────────────────────────────────────

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
    def _side_effect_name(tool: Any, params: dict[str, Any] | None = None) -> str:
        if (
            getattr(tool, "name", "") == "interaction_with_master"
            and str((params or {}).get("action", "")).strip().lower() == "status"
        ):
            return "read_only"
        value = getattr(tool, "side_effect", "unknown")
        return str(getattr(value, "value", value) or "unknown")

    @staticmethod
    def _retry_safe(tool: Any, params: dict[str, Any] | None = None) -> bool:
        if getattr(tool, "name", "") == "interaction_with_master":
            return str((params or {}).get("action", "")).strip().lower() == "status"
        return bool(getattr(tool, "retry_safe", False))

    @staticmethod
    def _tool_is_silent(tool: Any, tool_call: ToolCall) -> bool:
        if tool is None:
            return False
        if tool_call.function_name == "group_file_exec":
            return _composite_action(tool_call) == "image"
        if tool_call.function_name == "interaction_with_master":
            return _tool_action(tool_call) == "message"
        return bool(getattr(tool, "silent", False))

    @staticmethod
    def _note_external_delivery(
        engine: Any,
        group_id: str,
        item: Any,
        chat_result: Any,
        tool_call: ToolCall,
        params: dict[str, Any],
        result: Any,
    ) -> None:
        """把已经发到群里的图片/文件写回历史，供下一轮模型自查。

        group_file_exec 的 image/file 是静默投递：群里收到了内容，本轮却不产生
        任何文本，``_record_assistant_message`` 因此不会留下记录。模型下一轮
        看不到自己发过，只能重新找文件再发一次。这里按富文本卡片的既有做法，
        用一条简短回执补上这条历史。只处理真正投递到外部的动作，list/download
        等本地动作不写。
        """
        if _composite_action(tool_call) not in {"image", "file"}:
            return
        metadata = getattr(result, "internal_metadata", {})
        if not isinstance(metadata, dict):
            return
        if not (metadata.get("target_type") and metadata.get("target_id")):
            return
        record = getattr(engine, "_record_assistant_message", None)
        if not callable(record):
            return
        action = _composite_action(tool_call)
        if action == "file":
            label = str(metadata.get("file_name") or params.get("file_path") or "").strip()
            subject = f"文件「{label}」" if label else "文件"
        else:
            label = str(params.get("image_path") or "").strip()
            subject = f"图片 {label}" if label else "图片"
        try:
            record(
                group_id=group_id,
                target_user_id=getattr(item, "user_id", "") or "",
                content=f"（已发送{subject}；除非用户明确要求重发，否则不要再发）",
                system_prompt=getattr(chat_result, "system_prompt", ""),
                tags=[{"type": action, "label": label}],
                injected_request=getattr(chat_result, "injected_request", {}),
                injected_tool_names=getattr(chat_result, "injected_tool_names", []),
                platform_message_id=str(metadata.get("message_id") or ""),
                **_reasoning_memory_kwargs(chat_result),
            )
        except Exception as exc:  # 回执只是辅助信息，不能拖垮本轮回复
            logger.debug("外部投递回执写入失败: %s", exc)

    @staticmethod
    def _append_tool_chain_messages(
        engine: Any,
        messages: list[dict[str, Any]],
        group_id: str,
    ) -> bool:
        """Append explicitly addressed messages captured during tool work."""
        pop_messages = getattr(engine, "pop_tool_chain_messages", None)
        if not callable(pop_messages):
            return False

        injected = pop_messages(group_id)
        for message in injected:
            messages.append(
                {
                    "role": "user",
                    "content": PromptFactory.tag_message(
                        str(getattr(message, "content", "") or ""),
                        speaker=str(getattr(message, "speaker", "") or ""),
                        user_id=str(getattr(message, "channel_user_id", "") or ""),
                        platform_message_id=str(getattr(message, "message_id", "") or ""),
                        group_id=group_id,
                    ),
                }
            )
        return bool(injected)

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
                            event_data = {
                                "group_id": group_id,
                                "item_id": item.item_id,
                                "adapter_type": item.adapter_type or "",
                            }
                            if item.adapter_route_id:
                                event_data["adapter_route_id"] = item.adapter_route_id
                            accepted = await engine.event_bus.emit(
                                SessionEvent(
                                    type=SessionEventType.DELAYED_RESPONSE_TRIGGERED,
                                    data=event_data,
                                )
                            )
                            # Queue admission is not platform delivery, but it
                            # is the first required hand-off.  A closed/full/
                            # unsubscribed bus must leave the item eligible for
                            # a later ticker retry instead of suppressing it
                            # forever in the emitted set.
                            if accepted:
                                emitted.add(item.item_id)
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
                    enable_tools=False,
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
        *,
        adapter_type: str | None = None,
        adapter_route_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Process a delayed queue partition and always close its tool-chain window.

        The injection window is opened while tools run so that messages addressed
        to the persona mid-chain can join the next round. A provider failure in a
        later round raises out of the generation loop, so the window must be
        closed here as well; otherwise the group would keep swallowing every
        addressed message into a chain that is no longer running.
        """
        try:
            return await self._tick_delayed_queue_impl(
                group_id,
                on_partial_reply,
                adapter_type=adapter_type,
                adapter_route_id=adapter_route_id,
            )
        finally:
            end_tool_chain = getattr(self._engine, "end_tool_chain", None)
            if callable(end_tool_chain):
                end_tool_chain(group_id)

    async def _tick_delayed_queue_impl(
        self,
        group_id: str,
        on_partial_reply: Any | None = None,
        *,
        adapter_type: str | None = None,
        adapter_route_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Process delayed response queue for a group.

        If multiple items trigger in the same tick, merge them into a single
        prompt so the model generates only one consolidated reply.
        Supports multi-round TOOL execution similar to immediate responses.

        Args:
            group_id: The group / private chat to tick.
            on_partial_reply: Optional async callable used for the model output
                immediately following a failed or blocked tool call. Normal
                tool-round text stays in the assistant/tool message chain.
        """
        engine = self._engine
        recent = engine._helpers.get_recent_messages(group_id, n=10)
        rhythm = engine.rhythm_analyzer.analyze(group_id, recent)
        triggered = engine.delayed_queue.tick(
            group_id,
            recent,
            rhythm,
            adapter_type=adapter_type,
            adapter_route_id=adapter_route_id,
        )
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
                adapter_type=item.get("adapter_type"),
                adapter_route_id=item.get("adapter_route_id"),
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

        # Engagement rate for TOOL permission control
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
        expose_tools_in_prompt = not (
            plan_mode_enabled and limit_normal_tools and initial_lane != "plan"
        )
        bundle = self._build_delayed_prompt(
            triggered,
            group_id,
            caller_is_developer=caller_is_developer,
            adapter_type=adapter_type,
            expose_tools=expose_tools_in_prompt,
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
        history_token_budget = int(
            engine.config.get(
                "basic_memory_history_token_budget",
                DEFAULT_BASIC_MEMORY_HISTORY_TOKEN_BUDGET,
            )
        )

        # 获取当前发言者信息
        speaker_uid = resolved_uid or ""
        identity_aliases: list[str] = []
        for merged_item in triggered:
            for identity in (
                getattr(merged_item, "user_id", ""),
                getattr(merged_item, "channel_user_id", ""),
                getattr(merged_item, "speaker_name", ""),
            ):
                identity = str(identity or "").strip()
                if identity and identity not in identity_aliases:
                    identity_aliases.append(identity)
        if caller_profile is not None:
            profile_identities = [
                getattr(caller_profile, "user_id", ""),
                getattr(caller_profile, "name", ""),
                *getattr(caller_profile, "identities", {}).values(),
                *getattr(caller_profile, "identity_anchors", []),
            ]
            for identity in profile_identities:
                identity = str(identity or "").strip()
                if identity and identity not in identity_aliases:
                    identity_aliases.append(identity)
                if identity.isdigit() and f"qq_{identity}" not in identity_aliases:
                    identity_aliases.append(f"qq_{identity}")
        speaker_display = getattr(triggered[0], "speaker_name", "") if triggered else ""

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
            identity_aliases=identity_aliases,
            mentioned_user_ids=list(
                dict.fromkeys(
                    user_id
                    for merged_item in triggered
                    for user_id in getattr(merged_item, "related_user_ids", [])
                    if user_id
                )
            ),
            cross_group_enabled=bool(engine.config.get("cross_group_memory_enabled", True)),
            content_is_tagged=True,
            dynamic_context=bundle.dynamic_context,
            history_token_budget=history_token_budget,
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

        # Collect multimodal inputs from triggered items AND recent messages, then
        # inject into the last user message. Recent-message images are needed because
        # an image sent on its own (or attached to an earlier text message) only
        # survives as a caption/XML placeholder otherwise, and pure-image messages
        # never reach the delayed queue at all.
        unanswered_from = 0
        for idx in range(len(recent) - 1, -1, -1):
            if recent[idx].get("role") == "assistant":
                unanswered_from = idx + 1
                break
        all_multimodal: list[dict[str, str]] = []
        seen_values: set[str] = set()
        for source in (
            *[getattr(i, "multimodal_inputs", None) for i in triggered],
            *[r.get("multimodal_inputs") for r in recent[unanswered_from:]],
        ):
            for m in source or []:
                if m.get("type") != "image" or m.get("sub_type") == "1":
                    continue
                value = str(m.get("value", ""))
                if not value or value in seen_values:
                    continue
                seen_values.add(value)
                all_multimodal.append(m)

        messages = engine._helpers.inject_multimodal_into_user_message(messages, all_multimodal)

        # Multi-round generation with function_call support
        from sirius_pulse.core.brain import ChatRequest
        from sirius_pulse.tools.models import ToolInvocationContext, ToolResult

        max_tool_rounds = engine.config.get("max_tool_rounds", 8)
        partial_replies: list[str] = []
        last_partial_sent_at: float | None = None
        send_next_tool_output = False
        _round = 0
        tool_calls: list[ToolCall] = []
        reply = ""
        chat_result: Any = None
        sticker_names_accumulated: list[str] = []
        poke_user_ids_accumulated: list[str] = []
        pending_chat_result: Any = None
        ended_because_max_rounds = False
        max_round_reply: Any | None = None
        plan_mode_enabled = bool(engine.config.get("plan_mode_enabled", False))
        limit_normal_tools = bool(engine.config.get("plan_mode_limit_normal_tools", False))
        plan_mode = getattr(item, "lane", "chat") == "plan"
        plan_session: Any | None = None
        plan_final_reply: str | None = None
        plan_send_to_group = True
        tool_chain_active = False

        while True:
            self._append_tool_chain_messages(engine, messages, group_id)
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
            _extra_tools = []
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
            enable_tools_for_round = bool(engine.config.get("enable_tools", True))
            if plan_mode_enabled and limit_normal_tools and not plan_mode:
                enable_tools_for_round = False

            if pending_chat_result is not None:
                chat_result = pending_chat_result
                pending_chat_result = None
            else:
                if _round > max_tool_rounds:
                    ended_because_max_rounds = bool(
                        tool_calls
                        and engine._tool_registry is not None
                        and engine._tool_executor is not None
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
                        enable_tools=enable_tools_for_round,
                        caller_is_developer=caller_is_developer,
                        post_process=True,
                        extra_tools=_extra_tools,
                    )
                )
                _round += 1
            reply = chat_result.raw_text.strip()
            round_clean = chat_result.clean_text
            sticker_names_accumulated.extend(getattr(chat_result, "sticker_names", []) or [])
            poke_user_ids_accumulated.extend(getattr(chat_result, "poke_user_ids", []) or [])
            agent_turn.set_candidates(getattr(chat_result, "injected_tool_names", []))

            # 分类工具调用：计划控制 vs 普通工具
            tool_calls = chat_result.tool_calls or []
            report_next_tool_output = send_next_tool_output and not plan_mode
            send_next_tool_output = False
            plan_control = [tc for tc in tool_calls if tc.function_name in PLAN_CONTROL_TOOL_NAMES]
            regular_tools = [
                tc for tc in tool_calls if tc.function_name not in PLAN_CONTROL_TOOL_NAMES
            ]
            agent_turn.advance(
                AgentTurnPhase.PLAN if regular_tools or plan_control else AgentTurnPhase.RESPOND
            )
            await self._emit_agent_turn(engine, agent_turn)

            # 没有调用任何工具 → 文本作为最终回复
            if not tool_calls:
                if self._append_tool_chain_messages(engine, messages, group_id):
                    continue
                agent_turn.advance(AgentTurnPhase.RESPOND)
                await self._emit_agent_turn(engine, agent_turn)
                fenced_parts = _markdown_image.split_fenced_markdown(round_clean)
                markdown_contents = [
                    content for is_markdown, content in fenced_parts if is_markdown
                ]
                merged_markdown = _markdown_image.merge_markdown_blocks(markdown_contents)
                if not plan_mode and _markdown_image.should_render_markdown_card(markdown_contents):
                    markdown_sent = False
                    for is_markdown, content in fenced_parts:
                        if is_markdown:
                            if not markdown_sent:
                                try:
                                    message_id = (
                                        await _markdown_image.render_and_send_markdown_image(
                                            merged_markdown,
                                            adapter=getattr(engine, "_adapter", None),
                                            group_id=group_id,
                                        )
                                    )
                                except (RuntimeError, ValueError) as exc:
                                    logger.warning(
                                        "Markdown card delivery failed for %s; falling back to text: %s",
                                        group_id,
                                        exc,
                                    )
                                    if on_partial_reply is None:
                                        raise
                                    await on_partial_reply(merged_markdown)
                                    engine._record_assistant_message(
                                        group_id=group_id,
                                        target_user_id=item.user_id,
                                        content=merged_markdown,
                                        system_prompt=getattr(chat_result, "system_prompt", ""),
                                        injected_request=getattr(
                                            chat_result, "injected_request", {}
                                        ),
                                        injected_tool_names=getattr(
                                            chat_result, "injected_tool_names", []
                                        ),
                                        **_reasoning_memory_kwargs(chat_result),
                                    )
                                    last_partial_sent_at = time.monotonic()
                                else:
                                    engine._record_assistant_message(
                                        group_id=group_id,
                                        target_user_id=item.user_id,
                                        content=merged_markdown,
                                        system_prompt=getattr(chat_result, "system_prompt", ""),
                                        tags=[{"type": "image", "label": "富文本卡片"}],
                                        injected_request=getattr(
                                            chat_result, "injected_request", {}
                                        ),
                                        injected_tool_names=getattr(
                                            chat_result, "injected_tool_names", []
                                        ),
                                        **_reasoning_memory_kwargs(chat_result),
                                        platform_message_id=message_id,
                                    )
                                markdown_sent = True
                        else:
                            if on_partial_reply is None:
                                raise RuntimeError(
                                    "Fenced Markdown delivery requires on_partial_reply for text"
                                )
                            await on_partial_reply(content)
                            engine._record_assistant_message(
                                group_id=group_id,
                                target_user_id=item.user_id,
                                content=content,
                                system_prompt=getattr(chat_result, "system_prompt", ""),
                                injected_request=getattr(chat_result, "injected_request", {}),
                                injected_tool_names=getattr(chat_result, "injected_tool_names", []),
                                **_reasoning_memory_kwargs(chat_result),
                            )
                            last_partial_sent_at = time.monotonic()
                    chat_result.clean_text = ""
                    reply = ""
                    break
                if plan_mode:
                    plan_final_reply = chat_result.clean_text
                    plan_send_to_group = bool(plan_final_reply)
                    if plan_session is not None:
                        finish_plan_session(engine, group_id)
                break

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
                    _build_assistant_tool_message(
                        reply,
                        [enter_plan_tc],
                        getattr(chat_result, "reasoning_content", ""),
                    )
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
                    "需要放弃或无法完成时调用 abort_plan。不要发送计划过程中的中间文本。"
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
                    _build_assistant_tool_message(
                        reply,
                        [get_plan_status_tc],
                        getattr(chat_result, "reasoning_content", ""),
                    )
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
                    "计划模式结束，准备发送最终回复" if plan_send_to_group else "计划模式结束，不发送群消息"
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

            # 1. 发送当前轮次的文字（排除计划控制工具，只看普通工具是否全 silent）
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
                    _build_assistant_tool_message(
                        reply,
                        [update_progress_tc],
                        getattr(chat_result, "reasoning_content", ""),
                    )
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
                tool = (
                    engine._tool_registry.get(tool_call.function_name)
                    if engine._tool_registry is not None
                    else None
                )
                return self._tool_is_silent(tool, tool_call)

            non_tool_text = round_clean
            all_silent = bool(regular_tools) and all(_tool_is_silent(tc) for tc in regular_tools)
            if plan_mode and non_tool_text:
                engine._log_inner_thought(f"计划模式中间文本已隐藏: {non_tool_text[:40]}...")
            elif non_tool_text and report_next_tool_output:
                engine._log_inner_thought(f"工具调用出现问题，转发下一轮模型输出：{non_tool_text[:40]}...")
                if on_partial_reply is None:
                    logger.debug("工具异常后的模型输出没有可用发送回调，保留在消息链中")
                else:
                    await on_partial_reply(non_tool_text)
                    last_partial_sent_at = time.monotonic()
            elif non_tool_text and not all_silent:
                engine._log_inner_thought(f"工具链中间文本保留在消息链，不发送：{non_tool_text[:40]}...")

            # 2. 执行普通工具
            tool_multimodal: list[dict[str, Any]] = []
            if (
                regular_tools
                and engine._tool_registry is not None
                and engine._tool_executor is not None
            ):
                begin_tool_chain = getattr(engine, "begin_tool_chain", None)
                if callable(begin_tool_chain):
                    begin_tool_chain(group_id)
                    tool_chain_active = True
                from sirius_pulse.memory.user.unified_models import UnifiedUser

                caller_user_id = item.user_id
                tool_caller = UnifiedUser(
                    user_id=caller_user_id,
                    name=caller_profile.name if caller_profile else caller_user_id,
                    metadata={"is_developer": caller_is_developer},
                )
                developer_profiles: list[UnifiedUser] = []
                group_entries = engine.user_manager.entries.get(group_id, {})
                for profile in group_entries.values():
                    if profile.is_developer:
                        developer_profiles.append(profile)

                engine._tool_executor.set_chat_context(
                    group_id=group_id,
                    user_id=caller_user_id or "",
                    adapter_type=getattr(engine, "_current_adapter_type", ""),
                )

                # 构造 assistant 消息（含普通工具的 tool_calls）
                assistant_msg = _build_assistant_tool_message(
                    reply,
                    regular_tools,
                    getattr(chat_result, "reasoning_content", ""),
                )
                messages.append(assistant_msg)

                try:
                    tool_timeout = max(
                        0.0, float(engine.config.get("tool_execution_timeout", 30.0))
                    )
                except (TypeError, ValueError):
                    tool_timeout = 30.0

                # 逐个执行 tool_call 并收集结果
                for idx, tc in enumerate(regular_tools):
                    tool_name = tc.function_name
                    try:
                        params = json.loads(tc.function_arguments) if tc.function_arguments else {}
                    except json.JSONDecodeError:
                        params = {}
                        logger.warning(
                            "tool_call 参数解析失败: %s, arguments=%s",
                            tool_name,
                            tc.function_arguments,
                        )

                    tool = engine._tool_registry.get(tool_name)
                    if tool is None:
                        err_msg = f"Tool '{tool_name}' not found"
                        logger.warning(err_msg)
                        send_next_tool_output = not plan_mode
                        messages.append({"role": "tool", "tool_call_id": tc.id, "content": err_msg})
                        continue

                    side_effect = self._side_effect_name(tool, params)

                    # Engagement-based permission
                    if (
                        caller_engagement < 0.1
                        and not caller_is_developer
                        and not self._is_autonomous_message_tool(tool, params)
                    ):
                        err_msg = (
                            f"Tool '{tool_name}' 被拒绝：互动不足 (engagement={caller_engagement:.2f})"
                        )
                        logger.warning(err_msg)
                        send_next_tool_output = not plan_mode
                        messages.append({"role": "tool", "tool_call_id": tc.id, "content": err_msg})
                        continue

                    if tool.developer_only and not caller_is_developer:
                        err_msg = f"Tool '{tool_name}' 被拒绝：caller 不是 developer"
                        logger.warning(err_msg)
                        send_next_tool_output = not plan_mode
                        messages.append({"role": "tool", "tool_call_id": tc.id, "content": err_msg})
                        continue

                    if not agent_turn.begin_action(
                        tool_call_id=tc.id,
                        tool_name=tool_name,
                        params=params,
                        side_effect=side_effect,
                        deduplicate=side_effect != "read_only",
                    ):
                        err_msg = f"Tool '{tool_name}' 被拒绝：本轮相同副作用动作已经执行过。"
                        await self._emit_agent_turn(engine, agent_turn)
                        send_next_tool_output = not plan_mode
                        messages.append({"role": "tool", "tool_call_id": tc.id, "content": err_msg})
                        continue

                    agent_turn.advance(AgentTurnPhase.ACT)
                    await self._emit_agent_turn(engine, agent_turn)

                    ctx = ToolInvocationContext(  # type: ignore[assignment]
                        caller=tool_caller,
                        developer_profiles=developer_profiles,
                    )
                    logger.info(
                        "Tool execute: %s(params=%s, caller=%s, group=%s)",
                        tool_name,
                        params,
                        caller_user_id,
                        group_id,
                    )
                    try:
                        result = await engine._tool_executor.execute_async(
                            tool,
                            params,
                            timeout=tool_timeout,
                            invocation_context=ctx,
                            max_retries=2 if self._retry_safe(tool, params) else 0,
                        )
                        agent_turn.finish_action(
                            tc.id,
                            success=result.success,
                            summary=(
                                result.error if not result.success else result.to_display_text()
                            ),
                        )
                        agent_turn.advance(AgentTurnPhase.VERIFY)
                        await self._emit_agent_turn(engine, agent_turn)
                        logger.info(
                            "Tool execute success: %s -> %s",
                            tool_name,
                            "success" if result.success else "failed",
                        )
                        tool_content = result.to_model_text()
                        if result.success:
                            self._note_external_delivery(
                                engine, group_id, item, chat_result, tc, params, result
                            )
                            # 收集多模态内容
                            for block in result.multimodal_blocks:
                                tool_multimodal.append(
                                    {"type": "image_url", "image_url": {"url": block.value}}
                                )
                        else:
                            logger.warning(
                                "TOOL '%s' 执行失败: %s",
                                tool_name,
                                result.error or "Unknown error",
                            )
                            send_next_tool_output = not plan_mode
                    except Exception as exc:
                        tool_content = ToolResult(success=False, error=str(exc)).to_model_text()
                        agent_turn.finish_action(tc.id, success=False, summary=str(exc))
                        agent_turn.advance(AgentTurnPhase.VERIFY)
                        await self._emit_agent_turn(engine, agent_turn)
                        logger.error("TOOL '%s' 执行异常: %s", tool_name, exc)
                        send_next_tool_output = not plan_mode

                    # 添加 tool 结果消息
                    messages.append(
                        {"role": "tool", "tool_call_id": tc.id, "content": tool_content}
                    )

                    # 链式调用中间增加延迟，避免回复过快
                    if idx < len(regular_tools) - 1:
                        await asyncio.sleep(2)

            if all_silent and not send_next_tool_output:
                if not self._append_tool_chain_messages(engine, messages, group_id):
                    break

            # 如果有多模态内容，作为 user 消息注入
            if tool_multimodal:
                messages.append({"role": "user", "content": tool_multimodal})

        if tool_chain_active:
            end_tool_chain = getattr(engine, "end_tool_chain", None)
            if callable(end_tool_chain):
                end_tool_chain(group_id)

        # Let the model turn the accumulated tool results into the final reply.
        if ended_because_max_rounds and not plan_mode:
            logger.debug(
                "Chain hit max_tool_rounds=%d; asking the model for a final reply",
                max_tool_rounds,
            )
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "【工具链控制信息】本轮已经达到工具调用轮次上限。"
                        "请停止调用工具，直接给出最终回复；根据上方工具结果说明已完成、"
                        "未完成的内容及原因，不要声称未经验证的操作已经完成。"
                    ),
                }
            )
            try:
                candidate = await engine.brain.chat(
                    ChatRequest(
                        group_id=group_id,
                        user_id=item.user_id or "",
                        system_prompt=system_prompt,
                        messages=messages,
                        task_name="response_generate",
                        enable_tools=bool(engine.config.get("enable_tools", True)),
                        caller_is_developer=caller_is_developer,
                        post_process=True,
                        tool_choice="none",
                    )
                )
                if getattr(candidate, "tool_calls", None):
                    logger.warning(
                        "Final tool-limit reply unexpectedly requested tools; using fallback"
                    )
                else:
                    max_round_reply = candidate
            except Exception as exc:
                logger.warning("Final tool-limit reply generation failed: %s", exc)

        # 最终回复：hooks 已处理 pin/dedup/memory/timestamp
        if ended_because_max_rounds:
            if max_round_reply is not None:
                chat_result = max_round_reply
                clean_reply = max_round_reply.clean_text
            else:
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
        elif ended_because_max_rounds:
            final_reply = clean_reply or "本轮工具调用上限已到，部分操作尚未完成。"
        else:
            final_reply = clean_reply

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
        sticker_names = dedupe_sticker_names(sticker_names_accumulated)
        poke_user_ids = list(dict.fromkeys(poke_user_ids_accumulated))[:1]

        # Emit event with full reply data for external delivery
        agent_turn.advance(AgentTurnPhase.RESPOND)
        await self._emit_agent_turn(engine, agent_turn)
        agent_turn.advance(AgentTurnPhase.COMPLETE)
        await self._emit_agent_turn(engine, agent_turn)
        event_data = {
            "group_id": group_id,
            "item_id": triggered[0].item_id,
            "adapter_type": getattr(triggered[0], "adapter_type", None) or "",
            "reply": final_reply,
            "partial_replies": partial_replies,
            "sticker_names": sticker_names,
            "poke_user_ids": poke_user_ids,
            "agent_turn_id": agent_turn.turn_id,
        }
        triggered_route = getattr(triggered[0], "adapter_route_id", None)
        if triggered_route:
            event_data["adapter_route_id"] = triggered_route
        await engine.event_bus.emit(
            SessionEvent(
                type=SessionEventType.DELAYED_RESPONSE_TRIGGERED,
                data=event_data,
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
                "poke_user_ids": poke_user_ids,
                "agent_turn_id": agent_turn.turn_id,
            }
        ]

    def _build_delayed_prompt(
        self,
        items: Any,
        group_id: str,
        caller_is_developer: bool = False,
        adapter_type: str | None = None,
        expose_tools: bool = True,
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
            tool_registry=engine._tool_registry if expose_tools else None,
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
        return bundle

    @staticmethod
    def _is_autonomous_message_tool(tool: Any, params: dict[str, Any] | None = None) -> bool:
        """Return True for package built-ins that replace legacy prompt tags."""
        tool_name = getattr(tool, "name", "")
        if tool_name not in _AUTONOMOUS_MESSAGE_TOOLS:
            return False
        source_path = getattr(tool, "source_path", None)
        if source_path is None:
            return False
        try:
            builtin_dir = (Path(__file__).resolve().parents[1] / "tools" / "builtin").resolve()
            if not source_path.resolve().is_relative_to(builtin_dir):
                return False
            if tool_name == "interaction_with_master":
                return str((params or {}).get("action", "")).strip().lower() == "message"
            return True
        except Exception:
            return False
