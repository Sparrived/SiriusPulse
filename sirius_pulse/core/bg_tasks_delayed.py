"""延迟队列相关后台任务。

包含延迟队列轮询、延迟响应处理等功能。队列取出一批消息后，这里负责认人、拼提示词；
多轮工具循环本身在 ``core/reply_turn.py``。
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any

from sirius_pulse.core import life_bridge
from sirius_pulse.core.agent_turn import AgentTurn
from sirius_pulse.core.constants import (
    DEFAULT_BASIC_MEMORY_HISTORY_TOKEN_BUDGET,
    DEFAULT_MAX_TOOL_ROUNDS,
    DEFAULT_MEMORY_UNIT_TOKEN_BUDGET,
    DEFAULT_MEMORY_UNIT_TOP_K,
)
from sirius_pulse.core.delayed_response_queue import persist_delayed_queue
from sirius_pulse.core.events import SessionEvent, SessionEventType
from sirius_pulse.core.identity_resolver import IdentityContext
from sirius_pulse.core.prompt_factory import PromptFactory
from sirius_pulse.core.reply_turn import (
    ChatReplyTurn,
    ReplyCaller,
    append_tool_chain_messages,
    emit_agent_turn,
    engagement_denial,
    first_sentence_streamer,
    note_external_delivery,
    tool_chain_message_text,
    tool_is_silent,
)
from sirius_pulse.core.tool_call_runner import retry_safe, side_effect_name
from sirius_pulse.core.tool_loop import assistant_tool_message as _build_assistant_tool_message
from sirius_pulse.core.tool_loop import resolve_max_tool_rounds
from sirius_pulse.tools.builtin._internal import _markdown_image  # noqa: F401  (测试从这里打桩)

if TYPE_CHECKING:
    from sirius_pulse.core.engine_core import _EmotionalGroupChatEngineBase

logger = logging.getLogger(__name__)

__all__ = ["DelayedQueueTasks", "_build_assistant_tool_message"]

#: ticker 两次醒来的最短间隔，防止「已到点但还没被取走」的条目让它空转。
_TICKER_MIN_SLEEP_SECONDS = 0.05

# C 方案：指向性落在灰色区间时，把“要不要接话”交回模型判断。
# 区间内既不能 100% 认定在叫自己（会抢别人的话），也不该直接静默（会漏掉真正找自己的消息）。
_GREY_ZONE_DIRECTED_MIN = 0.45
_GREY_ZONE_DIRECTED_MAX = 0.70
_GREY_ZONE_SKIP_HINT = "（这条消息不一定是在叫你；如果你判断它其实是在叫别人，就只输出 [SKIP]，系统不会发送任何内容）"

#: 对方明确不想被问名字时的说法；命中就不再提示她问。
_DECLINE_NAME_PHRASES = (
    "别再问我名字",
    "不要再问我的名字",
    "不用问我名字",
    "不想告诉你名字",
    "不想说名字",
    "别问我的名字",
)


def _grey_zone_directed_score(item: Any) -> float | None:
    """取出触发这条队列项时的 directed_score（历史数据可能没有）。"""
    decision = getattr(item, "strategy_decision", None)
    context = getattr(decision, "context", None)
    if not isinstance(context, dict):
        return None
    participation = context.get("participation")
    if not isinstance(participation, dict):
        return None
    try:
        return float(participation.get("directed_score"))
    except (TypeError, ValueError):
        return None


def _apply_grey_zone_hint(message_content: str, score: float | None) -> str:
    """灰色区间补一句"拿不准就跳过"的提示；不在区间原样返回。"""
    if score is not None and _GREY_ZONE_DIRECTED_MIN <= score < _GREY_ZONE_DIRECTED_MAX:
        return f"{message_content}\n{_GREY_ZONE_SKIP_HINT}"
    return message_content


def _pending_evidence_callers(
    engine: Any, group_id: str, evidence_refs: list[str]
) -> tuple[tuple[str, Any], ...]:
    """Compatibility wrapper for callers that already admitted pending evidence."""
    from sirius_pulse.tools.pending_sources import pending_evidence_callers

    return pending_evidence_callers(engine, group_id, evidence_refs)


def _coerce_queue_item(item: Any, group_id: str) -> Any:
    """Defensive: if _queues was corrupted externally, an item may be a dict."""
    if not isinstance(item, dict):
        return item
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
    return DelayedResponseItem(
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
        evidence_refs=[str(ref) for ref in item.get("evidence_refs", []) if ref],
    )


def _identity_aliases(triggered: list[Any], caller_profile: Any) -> list[str]:
    """这一轮说话人的所有叫法：记忆检索靠它认出「说的是我」的旧记录。"""
    aliases: list[str] = []
    for merged_item in triggered:
        for identity in (
            getattr(merged_item, "user_id", ""),
            getattr(merged_item, "channel_user_id", ""),
            getattr(merged_item, "speaker_name", ""),
        ):
            identity = str(identity or "").strip()
            if identity and identity not in aliases:
                aliases.append(identity)
    if caller_profile is not None:
        profile_identities = [
            getattr(caller_profile, "user_id", ""),
            getattr(caller_profile, "name", ""),
            *getattr(caller_profile, "identities", {}).values(),
            *getattr(caller_profile, "identity_anchors", []),
        ]
        for identity in profile_identities:
            identity = str(identity or "").strip()
            if identity and identity not in aliases:
                aliases.append(identity)
            if identity.isdigit() and f"qq_{identity}" not in aliases:
                aliases.append(f"qq_{identity}")
    return aliases


def _turn_images(
    triggered: list[Any], pending_history: list[dict[str, Any]]
) -> list[dict[str, str]]:
    """这一轮要带给模型的图片：触发条目上的，加上她上次开口之后群里发的。

    Recent-message images are needed because an image sent on its own (or attached
    to an earlier text message) only survives as a caption/XML placeholder otherwise,
    and pure-image messages never reach the delayed queue at all. 表情包也一并带上：
    它们不进视觉通道，但本地路径要列进文本，模型才能把表情包原图当生图参考图。
    """
    unanswered_from = 0
    for idx in range(len(pending_history) - 1, -1, -1):
        if pending_history[idx].get("role") == "assistant":
            unanswered_from = idx + 1
            break
    images: list[dict[str, str]] = []
    seen_values: set[str] = set()
    for source in (
        *[getattr(i, "multimodal_inputs", None) for i in triggered],
        *[r.get("multimodal_inputs") for r in pending_history[unanswered_from:]],
    ):
        for m in source or []:
            if m.get("type") != "image":
                continue
            value = str(m.get("value", ""))
            if not value or value in seen_values:
                continue
            seen_values.add(value)
            images.append(m)
    return images


def _append_context(bundle: Any, block: str) -> None:
    if block:
        bundle.dynamic_context = (
            f"{bundle.dynamic_context}\n\n{block}" if bundle.dynamic_context else block
        )


class DelayedQueueTasks:
    """延迟队列相关任务组件。"""

    def __init__(self, engine: _EmotionalGroupChatEngineBase) -> None:
        self._engine = engine

    # 回合内的小判断已经搬到 ``core/reply_turn``；这些别名留给按旧名调用的地方。
    _emit_agent_turn = staticmethod(emit_agent_turn)
    _first_sentence_streamer = staticmethod(first_sentence_streamer)
    _tool_is_silent = staticmethod(tool_is_silent)
    _note_external_delivery = staticmethod(note_external_delivery)
    _tool_chain_message_text = staticmethod(tool_chain_message_text)
    _append_tool_chain_messages = staticmethod(append_tool_chain_messages)
    _engagement_denial = staticmethod(engagement_denial)
    _side_effect_name = staticmethod(side_effect_name)
    _retry_safe = staticmethod(retry_safe)

    @staticmethod
    def _scene_context(engine: Any, group_id: str, adapter_type: str | None) -> str:
        """群里刚出现、她可以借来用的表情包，以及「最近的回复在套模板」的提醒。"""
        memory = getattr(engine, "basic_memory", None)
        get_context = getattr(memory, "get_context", None)
        if not callable(get_context):
            return ""
        try:
            from sirius_pulse.core.borrowed_images import (
                recent_reusable_images,
                remember,
                render_block,
            )
            from sirius_pulse.core.style_drift import template_hint

            entries = list(get_context(group_id, n=60))
            parts: list[str] = []
            if adapter_type == "napcat" and not str(group_id).startswith("private_"):
                images = recent_reusable_images(entries)
                remember(engine, group_id, images)
                block = render_block(images)
                if block:
                    parts.append(block)
            hint = template_hint(
                [str(e.content or "") for e in entries if getattr(e, "role", "") == "assistant"]
            )
            if hint:
                parts.append(hint)
            return "\n\n".join(parts)
        except Exception:
            logger.warning("群内情景提示暂不可用，本轮不注入", exc_info=True)
            return ""

    @staticmethod
    def _impressions_context(engine: Any, items: list[Any], message_content: str) -> str:
        """这一轮说话的人、以及消息里点到名字的人，她心里对他们的印象。"""
        if not getattr(engine, "work_path", None):
            return ""
        try:
            from sirius_pulse.memory.impressions import (
                impression_store,
                pick_people,
                render_impressions,
            )

            speakers: list[str] = []
            for item in reversed(items):
                for uid in [getattr(item, "user_id", ""), *getattr(item, "related_user_ids", [])]:
                    if uid and uid not in speakers:
                        speakers.append(uid)
            people = pick_people(impression_store(engine).all(), speakers, message_content or "")
            return render_impressions(people)
        except Exception:
            logger.warning("人物印象暂不可用，本轮不注入", exc_info=True)
            return ""

    @staticmethod
    def _recent_search_context(engine: Any, group_id: str, current_texts: list[str]) -> str:
        """当前消息之前的最近几句对话，供记忆检索补全短追问的指代。"""
        basic = getattr(engine, "basic_memory", None)
        get_context = getattr(basic, "get_context", None)
        if not callable(get_context):
            return ""
        current = {text.strip() for text in current_texts if text.strip()}
        lines: list[str] = []
        try:
            entries = get_context(group_id, n=8)
        except Exception:
            return ""
        for entry in entries:
            content = str(getattr(entry, "content", "") or "").strip()
            if not content or content in current:
                continue
            speaker = getattr(entry, "speaker_name", "") or getattr(entry, "user_id", "") or ""
            lines.append(f"{speaker}: {content[:120]}" if speaker else content[:120])
        return "\n".join(lines[-3:])

    async def delayed_queue_ticker(self) -> None:
        """Event-driven ticker for the delayed queue.

        Sleeps until the next item could become due, or until the queue changes
        (enqueue / promote / requeue wake it immediately). Due-ness uses the same
        rule as ``tick()``—window expiry *and* the early triggers (topic gap,
        silent pace)—and stale items past their freshness TTL are dropped here.
        Actual reply generation and delivery is handled by the external caller
        via tick_delayed_queue().
        """
        engine = self._engine
        max_interval = engine.config.get("delayed_queue_tick_interval_seconds", 10)
        wake = asyncio.Event()
        engine.delayed_queue.set_wake_callback(wake.set)
        try:
            while engine._bg_running:
                # 先清再算：清之后才入队的消息会重新置位，不会被这一轮睡过去。
                wake.clear()
                try:
                    next_wake = self._ticker_sleep_seconds(max_interval)
                except Exception:
                    logger.warning("计算延迟队列唤醒时间失败", exc_info=True)
                    next_wake = max_interval
                try:
                    await asyncio.wait_for(wake.wait(), timeout=next_wake)
                except TimeoutError:
                    pass
                for group_id in list(engine._group_last_message_at.keys()):
                    try:
                        await self._emit_due_items(group_id)
                    except Exception as exc:
                        logger.warning("Delayed queue tick failed for %s: %s", group_id, exc)
        finally:
            engine.delayed_queue.set_wake_callback(None)

    def _ticker_sleep_seconds(self, max_interval: float) -> float:
        engine = self._engine
        next_wake = float(max_interval)
        for group_id in list(engine._group_last_message_at.keys()):
            emitted = engine._delayed_event_emitted.get(group_id, set())
            wait = engine.delayed_queue.seconds_until_next_check(group_id, emitted=emitted)
            if wait is not None:
                next_wake = min(next_wake, wait)
        # 下限防空转：已到点但还没被投递循环取走的条目由 emitted 挡住，不参与计算。
        return max(_TICKER_MIN_SLEEP_SECONDS, next_wake)

    async def _emit_due_items(self, group_id: str) -> None:
        engine = self._engine
        queue = engine.delayed_queue
        if queue.drop_stale(group_id):
            persist_delayed_queue(engine)
        pending = queue.get_pending(group_id)
        # Per-group emitted tracking: only clean up IDs that no longer exist in
        # this group's pending list.
        emitted = engine._delayed_event_emitted.setdefault(group_id, set())
        emitted &= {i.item_id for i in pending}
        if not pending or all(item.item_id in emitted for item in pending):
            return
        recent = engine._helpers.get_recent_messages(group_id, n=10)
        rhythm = engine.rhythm_analyzer.analyze(group_id, recent)
        due = [
            item
            for item in queue.due_items(group_id, recent, rhythm)
            if item.item_id not in emitted
        ]
        if not due:
            return
        engine._log_inner_thought("之前记下的延迟回复，现在该开口了～")
        for item in due:
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
            # Queue admission is not platform delivery, but it is the first
            # required hand-off.  A closed/full/unsubscribed bus must leave the
            # item eligible for a later ticker retry instead of suppressing it
            # forever in the emitted set.
            if accepted:
                emitted.add(item.item_id)

    async def tick_delayed_queue(
        self,
        group_id: str,
        on_partial_reply: Any | None = None,
        *,
        adapter_type: str | None = None,
        adapter_route_id: str | None = None,
        item_id: str | None = None,
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
        prepared = queue.take_prepared_delivery(
            group_id, item_id=item_id, adapter_type=adapter_type, adapter_route_id=adapter_route_id
        )
        if prepared is not None:
            persist_delayed_queue(self._engine)
            return prepared
        # 记录调用前已在途的条目，避免把并发/上一轮的在途条目误判为本轮产物。
        pre_existing = set(
            queue.in_flight_ids(
                group_id, adapter_type=adapter_type, adapter_route_id=adapter_route_id
            )
        )
        try:
            results = await self._tick_delayed_queue_impl(
                group_id,
                on_partial_reply,
                adapter_type=adapter_type,
                adapter_route_id=adapter_route_id,
                item_id=item_id,
            )
        except BaseException:
            # Only retry generation with no escaped actions, including cancellation.
            # A completed/running tool or partial output forbids whole-turn replay.
            in_flight = [
                item_id
                for item_id in queue.in_flight_ids(
                    group_id, adapter_type=adapter_type, adapter_route_id=adapter_route_id
                )
                if item_id not in pre_existing
            ]
            unsafe = [item_id for item_id in in_flight if not queue.generation_retry_safe(item_id)]
            if unsafe:
                queue.fail_delivery(unsafe, uncertain=True)
                await self._engine.event_bus.emit(
                    SessionEvent(
                        type=SessionEventType.AGENT_TURN_UPDATED,
                        data={
                            "group_id": group_id,
                            "item_ids": unsafe,
                            "phase": "verify",
                            "delivery_status": "uncertain",
                            "error": "生成中断，已执行动作结果未确认；禁止自动重跑整轮",
                        },
                    )
                )
            retryable = [item_id for item_id in in_flight if item_id not in unsafe]
            if retryable:
                requeued = queue.requeue_in_flight(retryable)
                dropped = [item_id for item_id in retryable if item_id not in requeued]
                if dropped:
                    logger.error(
                        "群 %s 有 %d 条延迟回复已达重试上限被放弃: %s",
                        group_id,
                        len(dropped),
                        dropped,
                    )
            # 回队/放弃的条目必须清掉已发射标记。否则 ticker 的
            # ``newly_expired`` 会一直把这条 item_id 当成「已通知过」而跳过，
            # 群就再也不会触发 LLM 生成，后续消息只会被 merge_incoming 吞掉。
            emitted = getattr(self._engine, "_delayed_event_emitted", None)
            if isinstance(emitted, dict):
                group_emitted = emitted.get(group_id)
                if isinstance(group_emitted, set):
                    for item_id in in_flight:
                        group_emitted.discard(item_id)
            persist_delayed_queue(self._engine)
            raise
        else:
            # Generation is not delivery: retain payload until the adapter observes ACKs.
            completed = [
                item_id
                for item_id in queue.in_flight_ids(
                    group_id, adapter_type=adapter_type, adapter_route_id=adapter_route_id
                )
                if item_id not in pre_existing
            ]
            queue.stage_delivery(completed, results)
            persist_delayed_queue(self._engine)
            # Generated items remain in flight for the adapter, not the ticker.
            emitted = getattr(self._engine, "_delayed_event_emitted", None)
            if isinstance(emitted, dict):
                group_emitted = emitted.get(group_id)
                if isinstance(group_emitted, set):
                    for item_id in completed:
                        group_emitted.discard(item_id)
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
        item_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """Process delayed response queue for a group.

        If multiple items trigger in the same tick, merge them into a single
        prompt so the model generates only one consolidated reply, then run the
        multi-round tool loop (:class:`ChatReplyTurn`).

        Args:
            group_id: The group / private chat to tick.
            on_partial_reply: Optional async callable used for model output the
                group should see immediately (see :class:`ChatReplyTurn`).
        """
        engine = self._engine
        # 工作模式内部自带循环；期间不放行新的 tick，避免同一个群并排跑两轮。
        is_work_mode_active = getattr(engine, "is_work_mode_active", None)
        if callable(is_work_mode_active) and is_work_mode_active(group_id):
            return []
        taken = self._take_triggered(
            group_id, adapter_type=adapter_type, adapter_route_id=adapter_route_id, item_id=item_id
        )
        if taken is None:
            return []
        triggered, pending_history = taken
        item_ids = [item.item_id for item in triggered]
        engine.delayed_queue.mark_generation_started(item_ids)
        persist_delayed_queue(engine)
        triggered[0] = _coerce_queue_item(triggered[0], group_id)
        caller = self._resolve_caller(group_id, triggered[0])
        evidence_refs = list(
            dict.fromkeys(
                ref
                for triggered_item in triggered
                for ref in getattr(triggered_item, "evidence_refs", [])
                if ref
            )
        )
        system_prompt, messages, agent_turn = await self._prepare_reply_messages(
            group_id, triggered, caller, pending_history
        )
        turn = ChatReplyTurn(
            engine,
            group_id=group_id,
            triggered=triggered,
            caller=caller,
            system_prompt=system_prompt,
            messages=messages,
            evidence_refs=evidence_refs,
            agent_turn=agent_turn,
            max_tool_rounds=resolve_max_tool_rounds(
                engine.config.get("max_tool_rounds", DEFAULT_MAX_TOOL_ROUNDS)
            ),
            on_partial_reply=self._unsafe_after_partial(on_partial_reply, item_ids),
        )
        return await turn.run()

    def _take_triggered(
        self,
        group_id: str,
        *,
        adapter_type: str | None,
        adapter_route_id: str | None,
        item_id: str | None,
    ) -> tuple[list[Any], list[dict[str, Any]]] | None:
        """从队列里取出这一轮要回的条目；没有可回的返回 None。"""
        engine = self._engine
        recent = engine._helpers.get_recent_messages(group_id, n=10)
        # 节奏和队列判定只需要短窗口；未被 assistant 回复切断的图片则要从整段
        # 未完成对话里收集，否则图片先发、文字后发且中间消息较多时会丢失参考路径。
        pending_history = engine._helpers.get_recent_messages(group_id, n=0)
        rhythm = engine.rhythm_analyzer.analyze(group_id, recent)
        triggered = engine.delayed_queue.tick(
            group_id,
            recent,
            rhythm,
            adapter_type=adapter_type,
            adapter_route_id=adapter_route_id,
            item_id=item_id,
        )
        if triggered:
            return triggered, pending_history
        if item_id is not None:
            # ticker 按「话题停顿」发的触发，到这里时可能又有人说话了，条目继续
            # 等。撤掉已发标记，ticker 才会在下一次停顿时再叫它。
            engine._delayed_event_emitted.get(group_id, set()).discard(item_id)
        return None

    def _unsafe_after_partial(
        self, on_partial_reply: Any | None, item_ids: list[str]
    ) -> Any | None:
        """群里一旦看到了半截回复，这一轮就不能整轮重跑：先落这条边界再发。"""
        if on_partial_reply is None:
            return None
        engine = self._engine

        async def _send(text: str) -> None:
            engine.delayed_queue.mark_generation_unsafe(item_ids)
            persist_delayed_queue(engine)
            await on_partial_reply(text)

        return _send

    def _resolve_caller(self, group_id: str, item: Any) -> ReplyCaller:
        """认出这一轮是谁在叫她，以及她的互动度（决定能不能驱动工具）。"""
        engine = self._engine
        profile = None
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
                profile = engine.user_manager.get_user(resolved_uid, group_id)
        if profile is None and item.user_id:
            # 队列携带框架 ID；生成/查询路径不能用显示名再注册或绑定账号。
            profile = engine.user_manager.get_user(item.user_id, group_id)
            if profile is not None:
                resolved_uid = item.user_id
        engagement = 0.0
        if resolved_uid:
            semantic_profile = engine.semantic_memory.get_user_profile(group_id, resolved_uid)
            if semantic_profile:
                engagement = semantic_profile.engagement_rate
        return ReplyCaller(
            profile=profile,
            resolved_uid=resolved_uid,
            is_developer=bool(profile and profile.is_developer),
            engagement=engagement,
        )

    async def _prepare_reply_messages(
        self,
        group_id: str,
        triggered: list[Any],
        caller: ReplyCaller,
        pending_history: list[dict[str, Any]],
    ) -> tuple[str, list[dict[str, Any]], AgentTurn]:
        """拼出这一轮的 system prompt 与消息链（记忆 RAG + XML 历史 + 图片）。"""
        engine = self._engine
        bundle = self._build_delayed_prompt(
            triggered,
            group_id,
            caller_is_developer=caller.is_developer,
            adapter_type=getattr(triggered[0], "adapter_type", None),
        )
        # 提取原始聊天内容用于记忆检索，避免 XML 标签干扰
        raw_parts = [
            text
            for triggered_item in triggered
            for text in PromptFactory._extract_message_texts(
                getattr(triggered_item, "message_content", "")
            )
        ]
        raw_chat_content = "\n".join(raw_parts) if raw_parts else bundle.user_content
        search_context = self._recent_search_context(engine, group_id, raw_parts)
        await self._prefetch_retrieval(group_id, raw_chat_content, search_context)
        agent_turn = AgentTurn(
            group_id=group_id,
            item_ids=[triggered_item.item_id for triggered_item in triggered],
            query=raw_chat_content,
        )
        await emit_agent_turn(engine, agent_turn)

        cross_group = bool(engine.config.get("cross_group_memory_enabled", True))
        msgs, _ = engine.context_assembler.build_messages_with_breakdown(
            group_id=group_id,
            current_query=bundle.user_content,
            system_prompt=bundle.system_prompt,
            search_query=raw_chat_content,
            search_context=search_context,
            memory_unit_top_k=engine.config.get("memory_unit_top_k", DEFAULT_MEMORY_UNIT_TOP_K),
            memory_unit_token_budget=int(
                engine.config.get("memory_unit_token_budget", DEFAULT_MEMORY_UNIT_TOKEN_BUDGET)
            ),
            include_pending=False,
            speaker_user_id=caller.resolved_uid or "",
            speaker_name=getattr(triggered[0], "speaker_name", ""),
            identity_aliases=_identity_aliases(triggered, caller.profile),
            mentioned_user_ids=list(
                dict.fromkeys(
                    user_id
                    for merged_item in triggered
                    for user_id in getattr(merged_item, "related_user_ids", [])
                    if user_id
                )
            ),
            cross_group_enabled=cross_group,
            content_is_tagged=True,
            dynamic_context=bundle.dynamic_context,
            history_token_budget=int(
                engine.config.get(
                    "basic_memory_history_token_budget",
                    DEFAULT_BASIC_MEMORY_HISTORY_TOKEN_BUDGET,
                )
            ),
        )
        messages = engine._helpers.inject_multimodal_into_user_message(
            msgs[1:], _turn_images(triggered, pending_history)
        )
        return msgs[0]["content"], messages, agent_turn

    async def _prefetch_retrieval(self, group_id: str, query: str, search_context: str) -> None:
        """查询编码要发网络请求、冷层向量要读盘：放到线程里提前做完，聊天主循环里的
        检索只剩内存运算。预取失败只会让这一轮少一路召回。"""
        engine = self._engine
        manager = getattr(engine, "memory_unit_manager", None)
        prepare_async = getattr(manager, "prepare_retrieval_async", None)
        prepare = getattr(manager, "prepare_retrieval", None)
        if callable(prepare_async):
            await prepare_async(
                query,
                group_id=group_id,
                context=search_context,
                cross_group=bool(engine.config.get("cross_group_memory_enabled", True)),
            )
        elif callable(prepare):
            await asyncio.to_thread(prepare, query, group_id=group_id, context=search_context)

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
        # C 方案：灰色区间里补一句提示，让模型自己判断是否该接话
        _score = _grey_zone_directed_score(items[-1])
        _hinted = _apply_grey_zone_hint(message_content, _score)
        if _hinted != message_content:
            message_content = _hinted
            logger.info("[参与] 灰色区间提示已注入 group=%s directed=%.2f", group_id, _score)

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
            style_params=style_params,
            other_ai_names=engine._other_ai_names,
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
        _append_context(bundle, life_bridge.presence_context(engine, chat_id=group_id))
        _append_context(bundle, self._scene_context(engine, group_id, adapter_type))
        _append_context(bundle, self._impressions_context(engine, items, message_content))
        hints = self._name_hints(items, group_id)
        if hints:
            bundle.dynamic_context += "\n\n【怎么称呼他们】\n" + "\n".join(hints[:3])
        bundle.dynamic_context = PromptFactory.with_reference_header(bundle.dynamic_context)
        return bundle

    def _name_hints(self, items: list[Any], group_id: str) -> list[str]:
        """这一轮说话的人希望被怎么称呼；还不知道的，提示她合适时问一次。"""
        people = getattr(self._engine, "person_service", None)
        if people is None:
            return []
        from sirius_pulse.people import PersonAccess

        hints: list[str] = []
        sources: dict[str, list[dict[str, Any]]] = {}
        for item in items:
            for ref in getattr(item, "evidence_refs", []):
                try:
                    evidence = people.get_evidence(ref)
                    if evidence["group_id"] == group_id:
                        sources.setdefault(evidence["actor_id"], []).append(evidence)
                except Exception:
                    logger.warning("人物称呼来源暂不可用，保持原聊天流程")
        for actor, evidence_items in sources.items():
            try:
                content = "\n".join(item.get("content", "") for item in evidence_items)
                if any(phrase in content for phrase in _DECLINE_NAME_PHRASES):
                    people.should_ask_name(actor, declined=True)
                    continue
                access = PersonAccess(actor_id=people.persona_id, group_id=group_id)
                person = people.get_person(actor, access)
                preferred = next(
                    (
                        n["name"]
                        for n in person["names"]
                        if n["name_type"] == "preferred" and n["scope"] == group_id
                    ),
                    "",
                )
                # 用消息标签上的 evidence_ref 指人：和最近消息里的写法一致，
                # person_update 记称呼时也正好要它。
                ref = evidence_items[-1].get("evidence_ref", "")
                who = f"evidence_ref={ref} 那条消息的发送者"
                if preferred:
                    hints.append(f"- {who}：希望被叫「{preferred[:80]}」。")
                elif people.should_ask_name(actor):
                    hints.append(
                        f"- {who}：还不知道想被怎么称呼，合适时自然问一次，不用为此打断回答；" "对方说了就用 person_update 记下。"
                    )
                    people.mark_name_asked(actor)
            except Exception:
                logger.warning("人物称呼提示暂不可用，保持原聊天流程")
        return hints
