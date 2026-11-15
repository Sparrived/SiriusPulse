"""两条工具循环共用的零件。

聊天回复（``core/reply_turn.py``）和框架发起的工作回合（``core/tool_context/agent_turns.py``）
各自有一套多轮工具循环：前者多了工作模式进出、首句流式、投递节奏与互动门槛，后者
整轮都在工作模式里。两边都要做的事——轮次上限、assistant/tool 消息的拼法、工作模式
轨迹的开启与逐轮记录、撞上限后的收尾轮——放在这里，只保留一份。
"""

from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from sirius_pulse.core.constants import DEFAULT_MAX_TOOL_ROUNDS
from sirius_pulse.core.tool_call_runner import ToolCallOutcome
from sirius_pulse.core.work_mode import WORK_MODE_SOURCE_CHAT, WorkModeRun, WorkModeStore
from sirius_pulse.providers.base import ToolCall

logger = logging.getLogger(__name__)


def resolve_max_tool_rounds(raw: Any = DEFAULT_MAX_TOOL_ROUNDS, *, minimum: int = 0) -> int:
    """配置里的 ``max_tool_rounds``，缺省用 :data:`DEFAULT_MAX_TOOL_ROUNDS`。

    聊天回合允许 0（只说话、不调工具）；工作回合至少要跑一轮，调用方传 ``minimum=1``。
    """
    if raw is None:
        raw = DEFAULT_MAX_TOOL_ROUNDS
    return max(minimum, int(raw))


def assistant_tool_message(
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
                "id": call.id,
                "type": call.type or "function",
                "function": {
                    "name": call.function_name,
                    "arguments": call.function_arguments,
                },
            }
            for call in tool_calls
        ],
    }
    if reasoning_content:
        message["reasoning_content"] = reasoning_content
    return message


def append_tool_outcome(
    messages: list[dict[str, Any]],
    outcome: ToolCallOutcome,
    *,
    step: dict[str, Any] | None = None,
    multimodal: list[dict[str, Any]] | None = None,
) -> None:
    """把一次工具调用的结果回给模型，并记进本轮工作轨迹（若有）。"""
    if multimodal is not None:
        multimodal.extend(outcome.multimodal)
    messages.append(outcome.tool_message())
    if step is not None:
        step["results"].append({"tool": outcome.call.function_name, "output": outcome.content})


def append_tool_multimodal(
    messages: list[dict[str, Any]], multimodal: list[dict[str, Any]]
) -> None:
    """工具带回的图片作为一条 user 消息补进上下文（tool 消息本身只能是文本）。"""
    if multimodal:
        messages.append({"role": "user", "content": multimodal})


def open_work_run(
    work_path: Any,
    *,
    group_id: str,
    goal: str,
    source: str = WORK_MODE_SOURCE_CHAT,
    fallback_task_name: str = "",
) -> WorkModeRun:
    """开始一段工作模式轨迹，并挂上落盘视图（异常与取消路径靠它写终态）。

    任务名每次开工时重新读设置：改完设置不需要重启人格。设置为空时沿用
    ``fallback_task_name``。调用方决定何时第一次落盘。
    """
    store = WorkModeStore(work_path)
    run = WorkModeRun(
        group_id=group_id,
        goal=goal,
        source=source,
        task_name=store.work_task_name() or fallback_task_name,
    )
    run.store = store
    return run


def save_work_run(run: WorkModeRun | None) -> None:
    """轨迹落盘；没有开始过工作模式就没有可写的东西。"""
    if run is not None and run.store is not None:
        run.store.save_run(run)


def record_tool_round(run: WorkModeRun, *, text: str, tool_calls: list[ToolCall]) -> dict[str, Any]:
    """把这一轮的正文与工具调用记进轨迹，返回这条记录（工具结果随后追加进去）。

    调用方要持有返回的引用：中途发言会往 ``run.steps`` 里再插一条，工具结果不能
    挂到那条中途记录上。
    """
    step: dict[str, Any] = {
        "round": len(run.steps) + 1,
        "text": text,
        "tools": [
            {"name": call.function_name, "arguments": call.function_arguments}
            for call in tool_calls
        ],
        "results": [],
    }
    run.steps.append(step)
    return step


async def request_closing_round(
    chat: Callable[[Any], Awaitable[Any]],
    request: Any,
    *,
    messages: list[dict[str, Any]],
    prompt: str,
    failure_log: str,
) -> Any | None:
    """工具轮次用尽后，再要一轮只说话的收尾。

    请求里的工具清单要与前面各轮逐字一致（它是前缀的第一段，摘掉就整段错过缓存），
    由调用方用 ``tool_choice="none"`` 禁止调用。收尾失败不该让整轮消失：异常只记
    一条日志并返回 ``None``，由调用方退回兜底。
    """
    messages.append({"role": "user", "content": prompt})
    try:
        return await chat(request)
    except Exception as exc:
        logger.warning(failure_log, exc)
        return None
