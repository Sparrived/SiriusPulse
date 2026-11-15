"""聊天回复的多轮工具循环：延迟队列取出一批消息之后，到交给投递层之前的那一段。

``DelayedQueueTasks`` 负责取队列、认人、拼提示词；拼好的消息链交给
:class:`ChatReplyTurn`，它按轮次跑模型、处理工作模式的流程控制工具、执行普通工具，
最后组装交给投递层的结果字典。投递本身（拆句、发送、回执）不在这里。

和框架发起的工作回合（``core/tool_context/agent_turns.py``）共用的部分在
``core/tool_loop.py``。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from sirius_pulse.adapters.interaction_markers import parse_interaction_markers
from sirius_pulse.core import life_bridge
from sirius_pulse.core.agent_turn import AgentTurn, AgentTurnPhase
from sirius_pulse.core.delayed_response_queue import persist_delayed_queue
from sirius_pulse.core.events import SessionEvent, SessionEventType
from sirius_pulse.core.prompt_factory import PromptFactory
from sirius_pulse.core.reply_streaming import FirstSentenceStreamer
from sirius_pulse.core.sticker_delivery import dedupe_sticker_names
from sirius_pulse.core.tool_call_runner import (
    ToolCallOutcome,
    ToolCallRunner,
    parse_tool_arguments,
)
from sirius_pulse.core.tool_loop import (
    append_tool_multimodal,
    append_tool_outcome,
    assistant_tool_message,
    open_work_run,
    record_tool_round,
    request_closing_round,
    save_work_run,
)
from sirius_pulse.core.work_mode import (
    ENTER_WORK_MODE,
    QUIT_WORK_MODE,
    SEND_MIDWAY_MSG,
    WorkModeRun,
    control_tools,
    is_control_tool,
    parse_arguments,
)
from sirius_pulse.providers.base import ToolCall
from sirius_pulse.tools.builtin._internal import _markdown_image
from sirius_pulse.tools.models import ToolDefinition

logger = logging.getLogger(__name__)

#: 一轮里两次对外投递之间的间隔，模拟真人连发的节奏、也避开平台风控。
DELIVERY_PACING_SECONDS = 2.0

#: 收尾生成失败时发给群里的兜底话术。这些话会被群友看到，必须像她自己说的。
UNREADY_REPLY_FALLBACK = "抱歉，我刚才没能组织好回复，可以再说一次吗？"
TOOL_LIMIT_REPLY_FALLBACK = "这事我先弄到这儿，剩下的晚点再接着来～"

_DEFAULT_PARTIAL_LEAD_SECONDS = 1.5

_WORK_MODE_CONTINUE_NUDGE = (
    "【工具链控制信息】你还在工作模式中。"
    "上面的话没有人会看到；想对外说话用 send_midway_msg。"
    "继续推进任务，做完了或者做不下去就用 quit_work_mode 退出并说明结果。"
)
_NOTEBOOK_DRAFT_NOTICE = (
    "【内部控制信息】刚才小本工具同轮的正文尚未发送，" "只是你的草稿。请继续自然回应用户的问题；" "一般不必主动播报整理小本，若对方明确询问则按工具结果如实回答，" "待整理不等于已写入。"
)
_NOTEBOOK_FINAL_PROMPT = "【内部控制信息】请停止调用工具，直接自然回应用户；" "不用主动汇报小本整理，若被明确询问则按工具回执如实回答。"
_TOOL_LIMIT_FINAL_PROMPT = (
    "【工具链控制信息】本轮已经达到工具调用轮次上限。" "请停止调用工具，直接给出最终回复；根据上方工具结果说明已完成、" "未完成的内容及原因，不要声称未经验证的操作已经完成。"
)
_EMPTY_QUIT_RESULT_NOTICE = (
    "result 为空，不能这样退出：群里会收不到任何结果。" "请用一句话说明这次工作做完了什么、没做完什么，" "再调用 quit_work_mode。"
)


# ── 回合内用到的小判断（无状态，可单独测） ─────────────────────────────


def delivered_externally(result: Any) -> bool:
    """工具确实往群里/私聊投递了内容（以投递回执为准）。"""
    metadata = getattr(result, "internal_metadata", None)
    return isinstance(metadata, dict) and isinstance(metadata.get("history_receipt"), dict)


def is_bookkeeping(tool: Any) -> bool:
    """她私人的记录工具（如小本）：同轮正文只是草稿，见 ``ToolDefinition.bookkeeping``。"""
    return isinstance(tool, ToolDefinition) and tool.is_bookkeeping


def has_bookkeeping_tool(registry: Any) -> bool:
    names_where = getattr(registry, "names_where", None)
    return callable(names_where) and bool(names_where(is_bookkeeping))


def reasoning_memory_kwargs(chat_result: Any) -> dict[str, str]:
    reasoning_content = str(getattr(chat_result, "reasoning_content", "") or "").strip()
    return {"reasoning_content": reasoning_content} if reasoning_content else {}


def engagement_denial(
    tool: Any,
    params: dict[str, Any],
    engagement: float,
    is_developer: bool,
) -> str:
    """互动太少的人不能驱动工具；工具声明了 engagement_exempt 的除外（仅内置）。"""
    if engagement >= 0.1 or is_developer:
        return ""
    if isinstance(tool, ToolDefinition) and tool.engagement_exempt_for(params):
        return ""
    name = getattr(tool, "name", "")
    return f"Tool '{name}' 被拒绝：互动不足 (engagement={engagement:.2f})"


def tool_is_silent(tool: Any, tool_call: ToolCall) -> bool:
    """这次调用本身就是回复（发图、私聊主人……），同轮正文不必再单独发出。"""
    if not isinstance(tool, ToolDefinition):
        return False
    return tool.silent_for(parse_tool_arguments(tool.name, tool_call.function_arguments))


async def emit_agent_turn(engine: Any, turn: AgentTurn) -> None:
    await engine.event_bus.emit(
        SessionEvent(type=SessionEventType.AGENT_TURN_UPDATED, data=turn.to_event_data())
    )


def first_sentence_streamer(
    engine: Any,
    group_id: str,
    item: Any,
    *,
    on_partial_reply: Any | None,
    eligible: bool,
) -> FirstSentenceStreamer | None:
    """普通聊天回合里，让第一句话不必等整段写完（见 ``core/reply_streaming``）。

    工作模式的正文不外发、兜底收尾轮要整段处理，都不走这条路；没有半截发送回调
    （例如离线调用）时也没法提前发。
    """
    if on_partial_reply is None or not eligible:
        return None
    if not bool(engine.config.get("stream_first_sentence", True)):
        return None

    async def _send_first_sentence(text: str) -> None:
        await on_partial_reply(text)
        # 提前发出的这句是独立的一条消息：现在就记进历史，排在剩下那部分前面。
        record = getattr(engine, "_record_assistant_message", None)
        if callable(record):
            record(
                group_id=group_id,
                target_user_id=getattr(item, "user_id", "") or "",
                content=text,
            )

    return FirstSentenceStreamer(_send_first_sentence)


def note_external_delivery(
    engine: Any,
    group_id: str,
    item: Any,
    chat_result: Any,
    tool: Any,
    result: Any,
) -> None:
    """把已经投递到外部的静默动作写回历史，供下一轮模型自查。

    发图、传文件、私聊主人都是静默投递：外部确实收到了内容，本轮却不产生任何
    群聊文本，``_record_assistant_message`` 因此不会留下记录。模型下一轮看不到
    自己发过，于是重发同一张图、或把同一段话反复私聊给主人。工具在真正投递
    成功时把回执放进 ``internal_metadata["history_receipt"]``（content / tags /
    platform_message_id），这里照抄写进历史。回执会进她的记忆，所以只认内置工具。
    """
    if not (isinstance(tool, ToolDefinition) and tool.trusted):
        return
    metadata = getattr(result, "internal_metadata", None)
    receipt = metadata.get("history_receipt") if isinstance(metadata, dict) else None
    if not isinstance(receipt, dict) or not str(receipt.get("content") or "").strip():
        return
    record = getattr(engine, "_record_assistant_message", None)
    if not callable(record):
        return
    tags = receipt.get("tags")
    try:
        record(
            group_id=group_id,
            target_user_id=getattr(item, "user_id", "") or "",
            content=str(receipt["content"]),
            system_prompt=getattr(chat_result, "system_prompt", ""),
            tags=list(tags) if isinstance(tags, list) else [],
            injected_request=getattr(chat_result, "injected_request", {}),
            injected_tool_names=getattr(chat_result, "injected_tool_names", []),
            platform_message_id=str(receipt.get("platform_message_id") or ""),
            **reasoning_memory_kwargs(chat_result),
        )
    except Exception as exc:  # 回执只是辅助信息，不能拖垮本轮回复
        logger.debug("外部投递回执写入失败: %s", exc)


def tool_chain_message_text(message: Any) -> str:
    """点名消息的注入文本：图片/表情包带上读图描述和本地路径。

    描述写在 ``multimodal_inputs`` 上（历史 XML 取的就是它），而工具链当轮注入
    用的是 ``content`` 原文——纯图片消息的原文只有一个文件名哈希，模型在这一轮
    看不出内容，要等下一轮重建 XML 才知道。本地路径一并给出，模型才能在工具链
    里把刚收到的图当参考图交给 ``image_generate``。
    """
    from sirius_pulse.utils.image_paths import resolve_local_image_path

    text = str(getattr(message, "content", "") or "")
    images = [
        item
        for item in (getattr(message, "multimodal_inputs", None) or [])
        if item.get("type") == "image"
    ]
    if not images:
        return text
    captions = [str(item.get("caption", "")).strip() for item in images]
    caption = "；".join(dict.fromkeys(c for c in captions if c))
    local_paths: list[str] = []
    for item in images:
        local_path = resolve_local_image_path(item.get("value", ""))
        if local_path and local_path not in local_paths:
            local_paths.append(local_path)
    suffix_parts: list[str] = []
    if caption and caption not in text:
        tag = "动画表情" if any(item.get("sub_type") == "1" for item in images) else "图片描述"
        suffix_parts.append(f"[{tag}：{caption}]")
    if local_paths:
        suffix_parts.append("[本地图片路径：" + "；".join(local_paths) + "]")
    if not suffix_parts:
        return text
    return f"{text} {' '.join(suffix_parts)}".strip()


def append_tool_chain_messages(
    engine: Any,
    messages: list[dict[str, Any]],
    group_id: str,
    evidence_refs: list[str] | None = None,
) -> bool:
    """Append explicitly addressed messages captured during tool work."""
    pop_messages = getattr(engine, "pop_tool_chain_messages", None)
    if not callable(pop_messages):
        return False

    injected = pop_messages(group_id)
    for message in injected:
        ref = str(getattr(message, "evidence_ref", "") or "")
        if ref and evidence_refs is not None and ref not in evidence_refs:
            evidence_refs.append(ref)
        messages.append(
            {
                "role": "user",
                "content": PromptFactory.tag_message(
                    tool_chain_message_text(message),
                    speaker=str(getattr(message, "speaker", "") or ""),
                    user_id=str(getattr(message, "channel_user_id", "") or ""),
                    platform_message_id=str(getattr(message, "message_id", "") or ""),
                    evidence_ref=ref,
                    group_id=group_id,
                ),
            }
        )
    return bool(injected)


def _partial_lead_seconds(engine: Any) -> float:
    try:
        return max(
            0.0,
            float(engine.config.get("partial_reply_lead_seconds", _DEFAULT_PARTIAL_LEAD_SECONDS)),
        )
    except (TypeError, ValueError):
        return _DEFAULT_PARTIAL_LEAD_SECONDS


# ── 回合 ─────────────────────────────────────────────────────────────


@dataclass(slots=True)
class ReplyCaller:
    """这一轮是谁在叫她：工具权限与身份判定都看它。"""

    profile: Any
    resolved_uid: str | None
    is_developer: bool
    engagement: float


@dataclass(slots=True)
class _ToolCaller:
    """执行工具时的调用者视图（一轮只算一次）。"""

    user_id: str
    caller: Any
    developer_profiles: list[Any]


class ChatReplyTurn:
    """一次聊天回复：多轮「模型 → 工具」直到她给出要发的话。

    ``run()`` 是唯一入口，返回交给投递层的结果列表（目前总是一条）。

    Args:
        on_partial_reply: 需要群里立刻看到的模型输出走这里：普通聊天里伴随工具
            调用的正文、工具失败后下一轮的正文、提前发出的第一句、工作模式里的
            ``send_midway_msg`` 与进入时补发的进展。工作模式里模型自己的正文不外发。
    """

    def __init__(
        self,
        engine: Any,
        *,
        group_id: str,
        triggered: list[Any],
        caller: ReplyCaller,
        system_prompt: str,
        messages: list[dict[str, Any]],
        evidence_refs: list[str],
        agent_turn: AgentTurn,
        max_tool_rounds: int,
        on_partial_reply: Any | None,
    ) -> None:
        self.engine = engine
        self.group_id = group_id
        self.triggered = triggered
        self.item = triggered[0]
        self.item_ids = [t.item_id for t in triggered]
        self.caller = caller
        self.system_prompt = system_prompt
        self.messages = messages
        self.evidence_refs = evidence_refs
        self.agent_turn = agent_turn
        self.max_tool_rounds = max_tool_rounds
        self.on_partial_reply = on_partial_reply

        self.rounds = 0
        self.chat_result: Any = None
        self.tool_calls: list[ToolCall] = []
        self.sticker_names: list[str] = []
        self.poke_user_ids: list[str] = []
        self.last_partial_sent_at: float | None = None
        #: 本轮有工具失败/被拒：下一轮正文即使伴随静默工具也要发出去。
        self.report_next_round = False
        self.hit_round_limit = False
        self.notebook_final = False
        self.tool_chain_active = False
        self.work_run: WorkModeRun | None = None
        self.work_final_reply: str | None = None
        self.delivery_complete = False

    # ── 驱动 ──

    async def run(self) -> list[dict[str, Any]]:
        from sirius_pulse.core.brain import Brain

        # 一个回合只取一次时间：每轮重写会改掉这条 user 消息，后面累积的工具
        # 调用与结果就全部错过前缀缓存。
        Brain.pin_current_time(self.messages)
        while True:
            # 本轮开始时的工作模式状态：本轮才调用 enter_work_mode 的话，本轮正文
            # 仍按普通聊天规则外发。
            was_in_work_mode = self.work_run is not None
            self._absorb_waiting_messages()
            if self.rounds > self.max_tool_rounds or (self.notebook_final and self.rounds > 0):
                self.hit_round_limit = bool(self.tool_calls and self._can_execute_tools)
                break
            await self._model_round()
            if await self._handle_round(was_in_work_mode):
                break

        self._end_tool_chain()
        self._close_work_run()
        closing = await self._closing_round() if self.hit_round_limit else None
        return [await self._result(closing)]

    @property
    def _can_execute_tools(self) -> bool:
        return self.engine._tool_registry is not None and self.engine._tool_executor is not None

    def _absorb_waiting_messages(self) -> None:
        """把等待中的群消息补进上下文：工作模式暂存里被点名的、工具链窗口里点名她的。"""
        run = self.work_run
        if run is not None:
            # 暂存的群消息只有被点名时才一次性补进来，其余轮次保持前缀不变。
            run.take_flushed()
            for stashed in run.take_flushed_for_model():
                self.messages.append({"role": "user", "content": stashed})
            for ref in run.flushed_evidence_refs:
                if ref not in self.evidence_refs:
                    self.evidence_refs.append(ref)
            run.flushed_evidence_refs = []
        append_tool_chain_messages(self.engine, self.messages, self.group_id, self.evidence_refs)

    def _chat_request(self, **overrides: Any) -> Any:
        from sirius_pulse.core.brain import ChatRequest

        active = self.work_run is not None
        return ChatRequest(
            group_id=self.group_id,
            user_id=self.item.user_id or "",
            system_prompt=self.system_prompt,
            messages=self.messages,
            caller_is_developer=self.caller.is_developer,
            post_process=True,
            inject_current_time=False,
            work_mode=active,
            extra_tools=control_tools(active=active),
            **overrides,
        )

    async def _model_round(self) -> None:
        engine = self.engine
        self.agent_turn.advance(AgentTurnPhase.DECIDE)
        await emit_agent_turn(engine, self.agent_turn)
        # 工作模式期间可以换一个任务名，也就是换 AMKR 里的模型。
        task_name = (
            (self.work_run.task_name or "response_generate")
            if self.work_run is not None
            else "response_generate"
        )
        streamer = first_sentence_streamer(
            engine,
            self.group_id,
            self.item,
            on_partial_reply=self.on_partial_reply,
            eligible=self.work_run is None and not self.notebook_final,
        )
        try:
            self.chat_result = await engine.brain.chat(
                self._chat_request(
                    task_name=task_name,
                    enable_tools=bool(engine.config.get("enable_tools", True)),
                    defer_bookkeeping_tool_text=(
                        self.work_run is None
                        and engine._tool_registry is not None
                        and has_bookkeeping_tool(engine._tool_registry)
                    ),
                    stream_sink=streamer,
                )
            )
        finally:
            if streamer is not None:
                await streamer.settle()
        if streamer is not None and streamer.sent_at is not None:
            self.last_partial_sent_at = streamer.sent_at
        self.rounds += 1

    async def _handle_round(self, was_in_work_mode: bool) -> bool:
        """处理刚拿到的一轮模型输出；返回 True 表示回合到此结束。"""
        engine = self.engine
        result = self.chat_result
        # 消息链里要放她这一轮说的完整原话，包括已经提前发出去的第一句。
        reply = (getattr(result, "streamed_prefix", "") + result.raw_text).strip()
        round_clean = result.clean_text
        self.sticker_names.extend(getattr(result, "sticker_names", []) or [])
        self.poke_user_ids.extend(getattr(result, "poke_user_ids", []) or [])
        self.agent_turn.set_candidates(getattr(result, "injected_tool_names", []))

        # 分类工具调用：流程控制工具由本回合处理，其余交给 ToolExecutor
        tool_calls = result.tool_calls or []
        self.tool_calls = tool_calls
        control_calls = [tc for tc in tool_calls if is_control_tool(tc.function_name)]
        regular_tools = [tc for tc in tool_calls if not is_control_tool(tc.function_name)]
        report_failures = self.report_next_round
        self.report_next_round = False
        self.agent_turn.advance(AgentTurnPhase.PLAN if tool_calls else AgentTurnPhase.RESPOND)
        await emit_agent_turn(engine, self.agent_turn)

        if tool_calls and (control_calls or (regular_tools and self._can_execute_tools)):
            self.messages.append(
                assistant_tool_message(reply, tool_calls, getattr(result, "reasoning_content", ""))
            )
        quit_result = await self._handle_control_calls(control_calls, round_clean)
        work_step: dict[str, Any] | None = None
        if self.work_run is not None:
            work_step = record_tool_round(self.work_run, text=round_clean, tool_calls=tool_calls)
            save_work_run(self.work_run)

        if quit_result is not None:
            self._finish_work_mode(quit_result)
            return True
        if not tool_calls:
            return await self._handle_plain_reply(reply)
        return await self._handle_tool_round(
            regular_tools,
            has_control_calls=bool(control_calls),
            round_clean=round_clean,
            was_in_work_mode=was_in_work_mode,
            report_failures=report_failures,
            work_step=work_step,
        )

    async def _handle_plain_reply(self, reply: str) -> bool:
        """没有调用任何工具：文本就是最终回复（工作模式里除外）。"""
        if self.work_run is not None:
            # 工作模式里正文不外发，也不能就此结束：催她继续或主动退出。
            self.messages.append({"role": "assistant", "content": reply or "(无输出)"})
            self.messages.append({"role": "user", "content": _WORK_MODE_CONTINUE_NUDGE})
            return False
        if append_tool_chain_messages(
            self.engine, self.messages, self.group_id, self.evidence_refs
        ):
            return False
        self.agent_turn.advance(AgentTurnPhase.RESPOND)
        await emit_agent_turn(self.engine, self.agent_turn)
        return True

    async def _handle_tool_round(
        self,
        regular_tools: list[ToolCall],
        *,
        has_control_calls: bool,
        round_clean: str,
        was_in_work_mode: bool,
        report_failures: bool,
        work_step: dict[str, Any] | None,
    ) -> bool:
        """这一轮带了工具调用：先处理同轮正文，再执行普通工具。"""
        registry = self.engine._tool_registry
        all_silent = bool(regular_tools) and all(
            tool_is_silent(registry.get(tc.function_name) if registry is not None else None, tc)
            for tc in regular_tools
        )
        notebook_only = (
            not was_in_work_mode
            and not has_control_calls
            and bool(regular_tools)
            and registry is not None
            and all(is_bookkeeping(registry.get(tc.function_name)) for tc in regular_tools)
        )
        await self._send_round_text(
            round_clean,
            notebook_only=notebook_only,
            was_in_work_mode=was_in_work_mode,
            report_failures=report_failures,
            all_silent=all_silent,
        )

        tool_multimodal: list[dict[str, Any]] = []
        if regular_tools and self._can_execute_tools:
            tool_multimodal = await self._run_regular_tools(
                regular_tools, notebook_only=notebook_only, work_step=work_step
            )
        if work_step is not None:
            save_work_run(self.work_run)

        if notebook_only:
            # 小本写成功后直接回复，避免小本整理占用多轮聊天；失败（含误调已移除的
            # read）则让模型看到错误后再改一次，仍受 max_tool_rounds 约束。
            if not self.report_next_round:
                self.notebook_final = True
            self.messages.append({"role": "user", "content": _NOTEBOOK_DRAFT_NOTICE})

        # 静默只免去普通聊天的后续回复，工作模式仍须继续剩余动作并显式收尾。
        if (
            self.work_run is None
            and all_silent
            and not notebook_only
            and not self.report_next_round
            and not append_tool_chain_messages(
                self.engine, self.messages, self.group_id, self.evidence_refs
            )
        ):
            self.delivery_complete = True
            self.chat_result.clean_text = ""
            return True

        append_tool_multimodal(self.messages, tool_multimodal)
        return False

    async def _send_partial(self, text: str) -> None:
        assert self.on_partial_reply is not None
        await self.on_partial_reply(text)
        self.last_partial_sent_at = time.monotonic()

    async def _send_round_text(
        self,
        text: str,
        *,
        notebook_only: bool,
        was_in_work_mode: bool,
        report_failures: bool,
        all_silent: bool,
    ) -> None:
        """伴随工具调用的正文：普通聊天照常发出，草稿与工作模式里的不外发。"""
        if not text:
            return
        if notebook_only:
            # 它可能也是对用户的回答：留作草稿让下一轮完整作答，不先发整理播报。
            return
        if was_in_work_mode:
            # 工作模式内模型正文一律不外发，只有工具结果和 send_midway_msg 有效。
            self.engine._log_inner_thought(f"工作模式内正文不外发，留在自己的上下文里：{text[:40]}...")
            return
        if not (report_failures or not all_silent):
            return
        # 普通聊天里伴随工具调用的正文照常发出去，不再悄悄留在消息链里。
        if self.on_partial_reply is None:
            logger.debug("伴随工具调用的模型输出没有可用发送回调，保留在消息链中")
            return
        if report_failures:
            self.engine._log_inner_thought(f"工具调用出现问题，转发下一轮模型输出：{text[:40]}...")
        else:
            self.engine._log_inner_thought(f"正文与工具调用同轮返回，先发正文：{text[:40]}...")
        await self._send_partial(text)

    # ── 工作模式流程控制工具：由本回合直接处理，不进 ToolExecutor ──

    async def _handle_control_calls(
        self, control_calls: list[ToolCall], round_clean: str
    ) -> str | None:
        """处理 send_midway_msg / enter / quit；返回 quit 带出的结果（未退出为 None）。"""
        quit_result: str | None = None
        for tc in control_calls:
            params = parse_arguments(tc.function_arguments)
            if tc.function_name == SEND_MIDWAY_MSG:
                content = await self._send_midway(params)
            elif tc.function_name == ENTER_WORK_MODE:
                content = await self._enter_work_mode(params, round_clean)
            elif tc.function_name == QUIT_WORK_MODE:
                quit_result, content = self._quit_work_mode(params)
            else:
                continue
            self.messages.append({"role": "tool", "tool_call_id": tc.id, "content": content})
        return quit_result

    async def _send_midway(self, params: dict[str, Any]) -> str:
        markers = parse_interaction_markers(
            str(params.get("message", "") or "").strip(),
            available_stickers=getattr(self.engine, "_sticker_names", []) or [],
        )
        text = markers.text
        self.sticker_names.extend(markers.sticker_names)
        if text and self.on_partial_reply is not None:
            await self._send_partial(text)
            if self.work_run is not None:
                self.work_run.add_step(kind="midway", text=text)
            return "消息已发送给群里。"
        if not text and markers.sticker_names:
            return "表情包已记录，会随本轮结果发出。"
        if not text:
            return "message 为空，没有发送。"
        return "当前没有可用的发送通道，消息没有发出去。"

    async def _enter_work_mode(self, params: dict[str, Any], round_clean: str) -> str:
        engine = self.engine
        goal = str(params.get("goal", "") or "").strip()
        said_nothing = not round_clean.strip()
        if self.work_run is None:
            self.work_run = open_work_run(engine.work_path, group_id=self.group_id, goal=goal)
            engine.begin_work_mode(self.group_id, self.work_run)
            # 工作模式有自己的暂存窗口，前面那轮开着的工具链窗口得关上。
            self._end_tool_chain()
            save_work_run(self.work_run)
            engine._log_inner_thought(f"进入工作模式：{goal}")
            if said_nothing:
                await self._announce_work_start(goal)
        # 怎么说话、怎么退出写在下面的【工作模式规范】里，这里只报状态。
        notice = "已进入工作模式，重工具已解锁。"
        if said_nothing:
            # 进入轮一句话都没对群里说时，群里只会看到她突然开始做事。
            # 提醒她先用 send_midway_msg 报个方向，别让人干等。
            notice += "（这一轮你没有对群里说话；如果这次工作要花一会儿，"
            notice += "先用 send_midway_msg 简短说明你在做什么。）"
        # 工作细则只在真正开工时送到，不常驻在每一轮闲聊的 system prompt 里。
        notice += "\n\n" + PromptFactory.build_work_mode_guide()
        home_context = await life_bridge.home_overview(engine)
        if home_context:
            notice += "\n\n" + home_context
        return notice

    async def _announce_work_start(self, goal: str) -> None:
        """进入轮没写正文时，框架替她补一句进展。

        模型调用工具时几乎不写正文（线上进入工作模式的 19 轮全是 0 字，提示词劝
        不动），别让群里干等到结果才第一次看到她说话。
        """
        goal_preview = goal if len(goal) <= 80 else goal[:80] + "…"
        status_text = f"收到，开始处理：{goal_preview}" if goal_preview else "收到，开始处理，稍后汇报结果。"
        if self.on_partial_reply is not None:
            await self._send_partial(status_text)
            self.engine._log_inner_thought(f"进入轮无正文，补发进展：{status_text[:40]}...")

    @staticmethod
    def _quit_work_mode(params: dict[str, Any]) -> tuple[str | None, str]:
        result = str(params.get("result", "") or "").strip()
        if result:
            return result, "已退出工作模式。"
        # result 是这次工作唯一的对外出口。让它空着退出等于这一轮群里什么都收不到
        # （模型漏参、参数 JSON 截断都会走到这里），所以空 result 不算退出，只回一句
        # 提醒让它补上。
        return None, _EMPTY_QUIT_RESULT_NOTICE

    def _finish_work_mode(self, quit_result: str) -> None:
        markers = parse_interaction_markers(
            quit_result,
            available_stickers=getattr(self.engine, "_sticker_names", []) or [],
        )
        self.sticker_names.extend(markers.sticker_names)
        self.work_final_reply = markers.text
        if self.work_run is not None:
            self.work_run.finish(result=self.work_final_reply)
            save_work_run(self.work_run)
        self.engine._log_inner_thought(f"退出工作模式，结果：{self.work_final_reply[:40]}...")

    def _close_work_run(self) -> None:
        """工作模式只由 quit_work_mode 正常收尾；循环因别的原因结束（轮次上限、
        静默退出等）时如实记成未完成。"""
        run = self.work_run
        if run is not None and not run.finish_if_running(reason="工作模式被中断，任务未完成。"):
            save_work_run(run)

    def _end_tool_chain(self) -> None:
        if not self.tool_chain_active:
            return
        end_tool_chain = getattr(self.engine, "end_tool_chain", None)
        if callable(end_tool_chain):
            end_tool_chain(self.group_id)
        self.tool_chain_active = False

    # ── 普通工具 ──

    def _tool_caller(self) -> _ToolCaller:
        from sirius_pulse.memory.user.unified_models import UnifiedUser

        engine = self.engine
        profile = self.caller.profile
        user_id = self.caller.resolved_uid or self.item.user_id
        caller = (
            profile
            if isinstance(profile, UnifiedUser)
            else UnifiedUser(
                user_id=user_id,
                name=profile.name if profile else user_id,
                metadata={"is_developer": self.caller.is_developer},
            )
        )
        developer_profiles = [
            p for p in engine.user_manager.entries.get(self.group_id, {}).values() if p.is_developer
        ]
        return _ToolCaller(user_id=user_id, caller=caller, developer_profiles=developer_profiles)

    def _mark_generation_unsafe(self) -> None:
        # Tool retry_safe describes executor retries, not replaying an entire turn.
        self.engine.delayed_queue.mark_generation_unsafe(self.item_ids)
        persist_delayed_queue(self.engine)

    async def _run_regular_tools(
        self,
        regular_tools: list[ToolCall],
        *,
        notebook_only: bool,
        work_step: dict[str, Any] | None,
    ) -> list[dict[str, Any]]:
        """逐个执行普通工具，返回它们带回的多模态内容。"""
        from sirius_pulse.tools.models import ToolInvocationContext
        from sirius_pulse.tools.pending_sources import pending_evidence_callers

        engine = self.engine
        # 工作模式有自己的暂存窗口，别再开工具链注入窗口，免得两条路各记一份。
        if self.work_run is None:
            begin_tool_chain = getattr(engine, "begin_tool_chain", None)
            if callable(begin_tool_chain):
                begin_tool_chain(self.group_id)
                self.tool_chain_active = True
        tool_caller = self._tool_caller()
        engine._tool_executor.set_chat_context(
            group_id=self.group_id,
            user_id=tool_caller.user_id or "",
            # 用本条待发回复自己的平台来源：这是本次调用的权威取值，
            # 引擎级「当前适配器」可能已被并发的另一群覆盖。
            adapter_type=self.item.adapter_type or getattr(engine, "_current_adapter_type", ""),
        )
        # assistant 消息（含本轮的 tool_calls）已在流程控制工具处理前写入
        runner = ToolCallRunner(engine)
        evidence_callers = pending_evidence_callers(engine, self.group_id, self.evidence_refs)
        adapter_type = self.item.adapter_type or getattr(engine, "_current_adapter_type", "") or ""

        multimodal: list[dict[str, Any]] = []
        for idx, tc in enumerate(regular_tools):
            context = ToolInvocationContext(
                caller=tool_caller.caller,
                developer_profiles=tool_caller.developer_profiles,
                evidence_refs=tuple(self.evidence_refs),
                evidence_callers=evidence_callers,
                group_id=self.group_id,
                adapter_type=adapter_type,
            )
            outcome = await self._run_tool(runner, tc, context, tool_caller.user_id)
            if outcome is None:
                continue
            append_tool_outcome(self.messages, outcome, step=work_step, multimodal=multimodal)
            # 节奏间隔只为了别让两次对外投递（发图、传文件、私聊）挤在一起；
            # 查资料、读文件这类没往外发东西的工具接着跑，不必干等。
            if (
                idx < len(regular_tools) - 1
                and not notebook_only
                and outcome.ok
                and delivered_externally(outcome.result)
            ):
                await asyncio.sleep(DELIVERY_PACING_SECONDS)
        return multimodal

    def _reject(self, runner: ToolCallRunner, tc: ToolCall, reason: str) -> None:
        self.report_next_round = True
        self.messages.append(runner.reject(tc, reason).tool_message())

    async def _run_tool(
        self,
        runner: ToolCallRunner,
        tc: ToolCall,
        context: Any,
        caller_user_id: str,
    ) -> ToolCallOutcome | None:
        """执行一次普通工具调用；被拒时把原因回给模型并返回 None。"""
        engine = self.engine
        prepared = runner.prepare(tc, context)
        tool = prepared.tool
        if tool is None:
            logger.warning(prepared.rejection)
            self._reject(runner, tc, prepared.rejection)
            return None
        side_effect = prepared.side_effect
        denial = self._tool_denial(prepared, caller_user_id)
        if denial:
            logger.warning(denial)
            self.agent_turn.deny_action(tc.id, tc.function_name, side_effect, denial)
            await emit_agent_turn(engine, self.agent_turn)
            self._reject(runner, tc, denial)
            return None
        if not self.agent_turn.begin_action(
            tool_call_id=tc.id,
            tool_name=tc.function_name,
            params=prepared.params,
            side_effect=side_effect,
            deduplicate=side_effect != "read_only",
        ):
            await emit_agent_turn(engine, self.agent_turn)
            self._reject(runner, tc, f"Tool '{tc.function_name}' 被拒绝：本轮相同副作用动作已经执行过。")
            return None

        self.agent_turn.advance(AgentTurnPhase.ACT)
        await emit_agent_turn(engine, self.agent_turn)
        outcome = await runner.execute(
            prepared, group_id=self.group_id, before_execute=self._mark_generation_unsafe
        )
        result = outcome.result
        assert result is not None
        self.agent_turn.finish_action(
            tc.id,
            success=result.success,
            # 人物声明、私有资产与小本的内容不进 AgentTurn（会推给 WebUI）。
            summary=("操作已处理" if result.success else "操作未确认")
            if getattr(tool, "redact_logs", False)
            else (result.error if not result.success else result.to_display_text()),
        )
        self.agent_turn.advance(AgentTurnPhase.VERIFY)
        await emit_agent_turn(engine, self.agent_turn)
        if result.success:
            try:
                note_external_delivery(
                    engine, self.group_id, self.item, self.chat_result, tool, result
                )
            except Exception:
                logger.warning("外部投递回执生成失败", exc_info=True)
        else:
            self.report_next_round = True
        return outcome

    def _tool_denial(self, prepared: Any, caller_user_id: str) -> str:
        """互动门槛、开发者专用与来源绑定，任一不过就拒绝。

        批级门槛看是谁发起的这一轮；逐条门槛看这次操作实际绑定到的作者。
        """
        tool, params = prepared.tool, prepared.params
        call_context = prepared.context
        call_caller = call_context.caller if call_context is not None else None
        call_is_developer = bool(call_context is not None and call_context.caller_is_developer)
        denial = engagement_denial(tool, params, self.caller.engagement, self.caller.is_developer)
        if not denial:
            call_engagement = self.caller.engagement
            if call_caller is not None and call_caller.user_id != caller_user_id:
                profile = self.engine.semantic_memory.get_user_profile(
                    self.group_id, call_caller.user_id
                )
                call_engagement = profile.engagement_rate if profile else 0.0
            denial = engagement_denial(tool, params, call_engagement, call_is_developer)
        if not denial and tool.developer_only and not call_is_developer:
            denial = f"Tool '{prepared.name}' 被拒绝：caller 不是 developer"
        return denial or prepared.rejection

    # ── 收尾 ──

    async def _closing_round(self) -> Any | None:
        """撞到轮次上限：让模型把已经拿到的工具结果变成最终回复。"""
        logger.debug(
            "Chain hit max_tool_rounds=%d; asking the model for a final reply",
            self.max_tool_rounds,
        )
        candidate = await request_closing_round(
            self.engine.brain.chat,
            # 工具清单与前面各轮逐字一致才接得上前缀缓存，用 tool_choice 禁止调用而不是摘掉工具。
            self._chat_request(
                task_name="response_generate",
                enable_tools=bool(self.engine.config.get("enable_tools", True)),
                tool_choice="none",
            ),
            messages=self.messages,
            prompt=_NOTEBOOK_FINAL_PROMPT if self.notebook_final else _TOOL_LIMIT_FINAL_PROMPT,
            failure_log="Final tool-limit reply generation failed: %s",
        )
        if candidate is not None and getattr(candidate, "tool_calls", None):
            logger.warning("Final tool-limit reply unexpectedly requested tools; using fallback")
            return None
        return candidate

    def _final_reply_text(self, closing: Any | None) -> str:
        """决定要发出去的那段话（收尾轮的结果已并入 ``self.chat_result``）。"""
        if self.hit_round_limit:
            clean_reply = closing.clean_text if closing is not None else ""
            if not clean_reply:
                # 这句话会原样发到群里：只能是她自己的口吻，不能是工程提示。
                # 具体原因留在日志里给运维看。
                logger.warning(
                    "工具链达到轮次上限且收尾回复为空，发送兜底话术: group=%s rounds=%d",
                    self.group_id,
                    self.max_tool_rounds,
                )
            final_reply = clean_reply or (
                UNREADY_REPLY_FALLBACK if self.notebook_final else TOOL_LIMIT_REPLY_FALLBACK
            )
        else:
            final_reply = self.chat_result.clean_text if self.chat_result else ""

        # quit_work_mode 的 result 是这次工作的对外结果，直接作为本轮回复发出。
        if self.work_final_reply is not None:
            return self.work_final_reply
        if self.work_run is not None and not final_reply:
            # 兜底：进了工作模式就必须留下一个对外结果。轮次用尽、模型没话说、
            # 收尾轮也失败时，宁可说实话被打断，也不能让群里一声不响。
            return self.work_run.result
        return final_reply

    async def _wait_for_partial_lead(self, final_reply: str) -> None:
        """Fast tools can finish before the client has had time to visually render
        the partial reply. Keep a minimum lead window without delaying tool work."""
        if not final_reply or self.last_partial_sent_at is None:
            return
        remaining = _partial_lead_seconds(self.engine) - (
            time.monotonic() - self.last_partial_sent_at
        )
        if remaining > 0:
            await asyncio.sleep(remaining)

    async def _result(self, closing: Any | None) -> dict[str, Any]:
        from sirius_pulse.models.response_strategy import ResponseStrategy

        if self.hit_round_limit and closing is not None:
            self.chat_result = closing
        final_reply = self._final_reply_text(closing)
        strategy = "delayed"
        if any(i.strategy_decision.strategy == ResponseStrategy.IMMEDIATE for i in self.triggered):
            strategy = "immediate"
        await self._wait_for_partial_lead(final_reply)

        chat_result = self.chat_result
        rich_reply = bool(final_reply and _markdown_image.should_send_as_image(final_reply))
        rich_history = (
            {
                "target_user_id": self.item.user_id,
                "system_prompt": getattr(chat_result, "system_prompt", ""),
                "injected_request": getattr(chat_result, "injected_request", {}),
                "injected_tool_names": getattr(chat_result, "injected_tool_names", []),
                **reasoning_memory_kwargs(chat_result),
            }
            if rich_reply
            else {}
        )

        # The caller owns platform delivery; do not emit a second consumptive trigger.
        self.agent_turn.advance(AgentTurnPhase.RESPOND)
        await emit_agent_turn(self.engine, self.agent_turn)
        return {
            "strategy": strategy,
            "item_id": self.item.item_id,
            "reply": final_reply,
            # 模型主动 [SKIP]：没有正文是预期结果，投递层按「已出结果」收尾，
            # 不再当成生成失败去回队重试。
            "skip_reply": bool(getattr(chat_result, "skipped", False)),
            "partial_replies": [],
            "reply_references": chat_result.reply_references if chat_result else [],
            "sticker_names": dedupe_sticker_names(self.sticker_names),
            "poke_user_ids": list(dict.fromkeys(self.poke_user_ids))[:1],
            "agent_turn_id": self.agent_turn.turn_id,
            "agent_turn": self.agent_turn.to_event_data(),
            "delivery_complete": self.delivery_complete,
            "rich_reply": rich_reply,
            "rich_history": rich_history,
        }
