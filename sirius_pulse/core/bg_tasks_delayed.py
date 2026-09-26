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
from sirius_pulse.core.constants import (
    DEFAULT_BASIC_MEMORY_HISTORY_TOKEN_BUDGET,
    DEFAULT_MEMORY_UNIT_TOKEN_BUDGET,
    DEFAULT_MEMORY_UNIT_TOP_K,
)
from sirius_pulse.core.delayed_response_queue import _parse_iso
from sirius_pulse.core.events import SessionEvent, SessionEventType
from sirius_pulse.core.identity_resolver import IdentityContext
from sirius_pulse.core.prompt_factory import PromptFactory
from sirius_pulse.core.sticker_delivery import dedupe_sticker_names
from sirius_pulse.core.work_mode import (
    ENTER_WORK_MODE,
    QUIT_WORK_MODE,
    SEND_MIDWAY_MSG,
    WorkModeRun,
    WorkModeStore,
    control_tools,
    is_control_tool,
    parse_arguments,
)
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

        ``tick()`` consumes the queue destructively before generation, so a
        failure out of the generation loop would otherwise drop the user's
        message entirely. Items already taken are therefore re-queued here.
        """
        queue = self._engine.delayed_queue
        # 记录调用前已在途的条目，避免把并发/上一轮的在途条目误判为本轮产物。
        pre_existing = set(queue.in_flight_ids(group_id))
        try:
            results = await self._tick_delayed_queue_impl(
                group_id,
                on_partial_reply,
                adapter_type=adapter_type,
                adapter_route_id=adapter_route_id,
            )
        except BaseException:
            # 生成/投递失败：把本群刚触发但未成功外发的条目放回队列。
            # 用 BaseException 是为了连取消也回队——被取消的条目同样还没外发。
            in_flight = [
                item_id for item_id in queue.in_flight_ids(group_id) if item_id not in pre_existing
            ]
            if in_flight:
                requeued = queue.requeue_in_flight(in_flight)
                dropped = [item_id for item_id in in_flight if item_id not in requeued]
                if dropped:
                    logger.error(
                        "群 %s 有 %d 条延迟回复已达重试上限被放弃: %s",
                        group_id,
                        len(dropped),
                        dropped,
                    )
            raise
        else:
            # 生成 + 外发成功，确认丢弃本轮在途引用（合并批次可能有多条）。
            queue.commit_in_flight(
                [
                    item_id
                    for item_id in queue.in_flight_ids(group_id)
                    if item_id not in pre_existing
                ]
            )
            return results
        finally:
            end_tool_chain = getattr(self._engine, "end_tool_chain", None)
            if callable(end_tool_chain):
                end_tool_chain(group_id)
            # 工作模式同理：生成过程中抛错也不能把群永久留在暂存窗口里。
            end_work_mode = getattr(self._engine, "end_work_mode", None)
            if callable(end_work_mode):
                end_work_mode(group_id)

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
            on_partial_reply: Optional async callable used for model output the
                group should see immediately: text that accompanies a tool call
                outside work mode, text following a failed tool call, and
                ``send_midway_msg`` inside work mode. In work mode the model's
                own text is never sent.
        """
        engine = self._engine
        # 工作模式内部自带循环；期间不放行新的 tick，避免同一个群并排跑两轮。
        is_work_mode_active = getattr(engine, "is_work_mode_active", None)
        if callable(is_work_mode_active) and is_work_mode_active(group_id):
            return []
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
        bundle = self._build_delayed_prompt(
            triggered,
            group_id,
            caller_is_developer=caller_is_developer,
            adapter_type=adapter_type,
        )

        # Use ContextAssembler to build full messages with memory-unit RAG + XML history
        memory_unit_top_k = engine.config.get("memory_unit_top_k", DEFAULT_MEMORY_UNIT_TOP_K)
        memory_unit_token_budget = int(
            engine.config.get("memory_unit_token_budget", DEFAULT_MEMORY_UNIT_TOKEN_BUDGET)
        )
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

        msgs, _ = engine.context_assembler.build_messages_with_breakdown(
            group_id=group_id,
            current_query=bundle.user_content,
            system_prompt=bundle.system_prompt,
            search_query=raw_chat_content,
            memory_unit_top_k=memory_unit_top_k,
            memory_unit_token_budget=memory_unit_token_budget,
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
        tool_chain_active = False
        work_run: WorkModeRun | None = None
        work_store: WorkModeStore | None = None
        work_final_reply: str | None = None

        def _save_work_run() -> None:
            """轨迹落盘；没有开始过工作模式就没有可写的东西。"""
            if work_run is not None and work_store is not None:
                work_store.save_run(work_run)

        while True:
            # 本轮开始时的工作模式状态：本轮才调用 enter_work_mode 的话，本轮正文
            # 仍按普通聊天规则外发。
            was_in_work_mode = work_run is not None
            if work_run is not None:
                # 暂存的群消息只有被点名时才一次性补进来，其余轮次保持前缀不变。
                for stashed in work_run.take_flushed():
                    messages.append({"role": "user", "content": stashed})
            self._append_tool_chain_messages(engine, messages, group_id)
            enable_tools_for_round = bool(engine.config.get("enable_tools", True))

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
                # 工作模式期间可以换一个任务名，也就是换 AMKR 里的模型。
                round_task_name = (
                    (work_run.task_name or "response_generate")
                    if work_run is not None
                    else "response_generate"
                )
                chat_result = await engine.brain.chat(
                    ChatRequest(
                        group_id=group_id,
                        user_id=item.user_id or "",
                        system_prompt=system_prompt,
                        messages=messages,
                        task_name=round_task_name,
                        enable_tools=enable_tools_for_round,
                        caller_is_developer=caller_is_developer,
                        post_process=True,
                        work_mode=work_run is not None,
                        extra_tools=control_tools(active=work_run is not None),
                    )
                )
                _round += 1
            reply = chat_result.raw_text.strip()
            round_clean = chat_result.clean_text
            sticker_names_accumulated.extend(getattr(chat_result, "sticker_names", []) or [])
            poke_user_ids_accumulated.extend(getattr(chat_result, "poke_user_ids", []) or [])
            agent_turn.set_candidates(getattr(chat_result, "injected_tool_names", []))

            # 分类工具调用：流程控制工具由本循环处理，其余交给 ToolExecutor
            tool_calls = chat_result.tool_calls or []
            control_calls = [tc for tc in tool_calls if is_control_tool(tc.function_name)]
            regular_tools = [tc for tc in tool_calls if not is_control_tool(tc.function_name)]
            report_next_tool_output = send_next_tool_output
            send_next_tool_output = False
            agent_turn.advance(AgentTurnPhase.PLAN if tool_calls else AgentTurnPhase.RESPOND)
            await self._emit_agent_turn(engine, agent_turn)

            # ── 工作模式流程控制工具：由本循环直接处理，不进 ToolExecutor ──
            has_executable_tools = bool(
                regular_tools
                and engine._tool_registry is not None
                and engine._tool_executor is not None
            )
            if tool_calls and (control_calls or has_executable_tools):
                messages.append(
                    _build_assistant_tool_message(
                        reply,
                        tool_calls,
                        getattr(chat_result, "reasoning_content", ""),
                    )
                )

            quit_result: str | None = None
            for tc in control_calls:
                params = parse_arguments(tc.function_arguments)
                if tc.function_name == SEND_MIDWAY_MSG:
                    midway_text = str(params.get("message", "") or "").strip()
                    if midway_text and on_partial_reply is not None:
                        await on_partial_reply(midway_text)
                        last_partial_sent_at = time.monotonic()
                        if work_run is not None:
                            work_run.add_step(kind="midway", text=midway_text)
                        midway_result = "消息已发送给群里。"
                    elif not midway_text:
                        midway_result = "message 为空，没有发送。"
                    else:
                        midway_result = "当前没有可用的发送通道，消息没有发出去。"
                    messages.append(
                        {"role": "tool", "tool_call_id": tc.id, "content": midway_result}
                    )
                elif tc.function_name == ENTER_WORK_MODE:
                    goal = str(params.get("goal", "") or "").strip()
                    if work_run is None:
                        work_run = WorkModeRun(group_id=group_id, goal=goal)
                        work_store = WorkModeStore(engine.work_path)
                        # 每次进入时重新读设置：改完设置不需要重启人格。
                        work_run.task_name = work_store.work_task_name()
                        engine.begin_work_mode(group_id, work_run)
                        # 工作模式有自己的暂存窗口，前面那轮开着的工具链窗口得关上。
                        if tool_chain_active:
                            end_tool_chain = getattr(engine, "end_tool_chain", None)
                            if callable(end_tool_chain):
                                end_tool_chain(group_id)
                            tool_chain_active = False
                        _save_work_run()
                        engine._log_inner_thought(f"进入工作模式：{goal}")
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "content": (
                                "已进入工作模式：重工具已解锁，你的正文不会再被发送。"
                                "完成后用 quit_work_mode 退出，需要对外说话用 send_midway_msg。"
                            ),
                        }
                    )
                elif tc.function_name == QUIT_WORK_MODE:
                    quit_result = str(params.get("result", "") or "").strip()
                    messages.append({"role": "tool", "tool_call_id": tc.id, "content": "已退出工作模式。"})

            work_step: dict[str, Any] | None = None
            if work_run is not None:
                work_step = {
                    "round": len(work_run.steps) + 1,
                    "text": round_clean,
                    "tools": [
                        {"name": tc.function_name, "arguments": tc.function_arguments}
                        for tc in tool_calls
                    ],
                    "results": [],
                }
                work_run.steps.append(work_step)
                _save_work_run()

            if quit_result is not None:
                work_final_reply = quit_result
                if work_run is not None:
                    work_run.finish(result=quit_result)
                    _save_work_run()
                engine._log_inner_thought(f"退出工作模式，结果：{quit_result[:40]}...")
                break

            # 没有调用任何工具 → 文本作为最终回复
            if not tool_calls:
                if work_run is not None:
                    # 工作模式里正文不外发，也不能就此结束：催她继续或主动退出。
                    messages.append({"role": "assistant", "content": reply or "(无输出)"})
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "【工具链控制信息】你还在工作模式中。"
                                "上面的话没有人会看到；想对外说话用 send_midway_msg。"
                                "继续推进任务，做完了或者做不下去就用 quit_work_mode 退出并说明结果。"
                            ),
                        }
                    )
                    continue
                if self._append_tool_chain_messages(engine, messages, group_id):
                    continue
                agent_turn.advance(AgentTurnPhase.RESPOND)
                await self._emit_agent_turn(engine, agent_turn)
                rich_content = round_clean.strip()
                if _markdown_image.has_rich_structure(rich_content):
                    try:
                        delivery = await _markdown_image.render_and_send_rich_reply(
                            rich_content,
                            adapter=getattr(engine, "_adapter", None),
                            group_id=group_id,
                        )
                    except (RuntimeError, ValueError) as exc:
                        logger.warning(
                            "Rich reply image delivery failed for %s; falling back to text: %s",
                            group_id,
                            exc,
                        )
                        if on_partial_reply is None:
                            raise
                        await on_partial_reply(rich_content)
                        engine._record_assistant_message(
                            group_id=group_id,
                            target_user_id=item.user_id,
                            content=rich_content,
                            system_prompt=getattr(chat_result, "system_prompt", ""),
                            injected_request=getattr(chat_result, "injected_request", {}),
                            injected_tool_names=getattr(chat_result, "injected_tool_names", []),
                            **_reasoning_memory_kwargs(chat_result),
                        )
                        last_partial_sent_at = time.monotonic()
                    else:
                        engine._record_assistant_message(
                            group_id=group_id,
                            target_user_id=item.user_id,
                            content=rich_content,
                            system_prompt=getattr(chat_result, "system_prompt", ""),
                            tags=[{"type": "image", "label": "富文本卡片"}],
                            injected_request=getattr(chat_result, "injected_request", {}),
                            injected_tool_names=getattr(chat_result, "injected_tool_names", []),
                            **_reasoning_memory_kwargs(chat_result),
                            platform_message_id=delivery["image_message_id"],
                        )
                    chat_result.clean_text = ""
                    reply = ""
                    break
                break

            def _tool_is_silent(tool_call: ToolCall) -> bool:
                tool = (
                    engine._tool_registry.get(tool_call.function_name)
                    if engine._tool_registry is not None
                    else None
                )
                return self._tool_is_silent(tool, tool_call)

            non_tool_text = round_clean
            all_silent = bool(regular_tools) and all(_tool_is_silent(tc) for tc in regular_tools)
            if non_tool_text and was_in_work_mode:
                # 工作模式内模型正文一律不外发，只有工具结果和 send_midway_msg 有效。
                engine._log_inner_thought(f"工作模式内正文不外发，留在自己的上下文里：{non_tool_text[:40]}...")
            elif non_tool_text and (report_next_tool_output or not all_silent):
                # 普通聊天里伴随工具调用的正文照常发出去，不再悄悄留在消息链里。
                if on_partial_reply is None:
                    logger.debug("伴随工具调用的模型输出没有可用发送回调，保留在消息链中")
                else:
                    if report_next_tool_output:
                        engine._log_inner_thought(f"工具调用出现问题，转发下一轮模型输出：{non_tool_text[:40]}...")
                    else:
                        engine._log_inner_thought(f"正文与工具调用同轮返回，先发正文：{non_tool_text[:40]}...")
                    await on_partial_reply(non_tool_text)
                    last_partial_sent_at = time.monotonic()

            # 2. 执行普通工具
            tool_multimodal: list[dict[str, Any]] = []
            if (
                regular_tools
                and engine._tool_registry is not None
                and engine._tool_executor is not None
            ):
                # 工作模式有自己的暂存窗口，别再开工具链注入窗口，免得两条路各记一份。
                if work_run is None:
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
                    # 用本条待发回复自己的平台来源：这是本次调用的权威取值，
                    # 引擎级「当前适配器」可能已被并发的另一群覆盖。
                    adapter_type=item.adapter_type or getattr(engine, "_current_adapter_type", ""),
                )

                # assistant 消息（含本轮的 tool_calls）已在流程控制工具处理前写入
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
                        send_next_tool_output = True
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
                        send_next_tool_output = True
                        messages.append({"role": "tool", "tool_call_id": tc.id, "content": err_msg})
                        continue

                    if tool.developer_only and not caller_is_developer:
                        err_msg = f"Tool '{tool_name}' 被拒绝：caller 不是 developer"
                        logger.warning(err_msg)
                        send_next_tool_output = True
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
                        send_next_tool_output = True
                        messages.append({"role": "tool", "tool_call_id": tc.id, "content": err_msg})
                        continue

                    agent_turn.advance(AgentTurnPhase.ACT)
                    await self._emit_agent_turn(engine, agent_turn)

                    ctx = ToolInvocationContext(  # type: ignore[assignment]
                        caller=tool_caller,
                        developer_profiles=developer_profiles,
                        group_id=group_id,
                        adapter_type=item.adapter_type
                        or getattr(engine, "_current_adapter_type", "")
                        or "",
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
                            send_next_tool_output = True
                    except Exception as exc:
                        tool_content = ToolResult(success=False, error=str(exc)).to_model_text()
                        agent_turn.finish_action(tc.id, success=False, summary=str(exc))
                        agent_turn.advance(AgentTurnPhase.VERIFY)
                        await self._emit_agent_turn(engine, agent_turn)
                        logger.error("TOOL '%s' 执行异常: %s", tool_name, exc)
                        send_next_tool_output = True

                    # 添加 tool 结果消息
                    messages.append(
                        {"role": "tool", "tool_call_id": tc.id, "content": tool_content}
                    )
                    if work_step is not None:
                        work_step["results"].append({"tool": tool_name, "output": tool_content})

                    # 链式调用中间增加延迟，避免回复过快
                    if idx < len(regular_tools) - 1:
                        await asyncio.sleep(2)

            if work_step is not None:
                _save_work_run()

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

        # 工作模式只由 quit_work_mode 正常收尾；循环因别的原因结束（轮次上限、
        # 静默退出等）时如实记成未完成，并释放暂存窗口。
        if work_run is not None:
            if work_run.status == "running":
                work_run.finish(
                    result=work_final_reply or "工作模式被中断，任务未完成。",
                    status="aborted",
                )
            _save_work_run()

        # Let the model turn the accumulated tool results into the final reply.
        if ended_because_max_rounds:
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

        # Determine return strategy
        from sirius_pulse.models.response_strategy import ResponseStrategy

        strategy = "delayed"
        if any(i.strategy_decision.strategy == ResponseStrategy.IMMEDIATE for i in triggered):
            strategy = "immediate"

        if ended_because_max_rounds:
            final_reply = clean_reply or "本轮工具调用上限已到，部分操作尚未完成。"
        else:
            final_reply = clean_reply

        # quit_work_mode 的 result 是这次工作的对外结果，直接作为本轮回复发出。
        if work_final_reply is not None:
            final_reply = work_final_reply

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
            tool_registry=engine._tool_registry,
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
