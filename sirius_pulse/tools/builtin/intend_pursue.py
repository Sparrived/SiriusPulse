"""Built-in TOOL that lets a persona record something she wants to work out.

This is the ``do`` half of autonomy, and the deliberate twin of ``intend_share``.

Autonomy has to be driven by her own judgement, formed while she is actually
thinking.  It must not be manufactured by a background job pattern-matching the
tail of the chat log: a tick cannot know what she has already handled, so
"figure out the link someone just posted" mostly re-reads material she already
read while answering, and the reason recorded for it is a constant rather than a
real one.

So a *do*-intention originates here, in a real turn with the real conversation in
front of her: she decides "this is worth coming back to" and writes down what she
wants to work out and why.  The generic autonomy tick later decides whether and
when to actually pursue it, exactly as it does for a tell.

It is also available **during** a self-initiated turn (``allowed_when_self_initiated``
is true).  That matters more than it looks: free time is the one moment she can
start something that nobody prompted, so if she could not write down a follow-up
there, every thread she opened would die when the turn ended and her only durable
source of new intentions would be other people's messages.  Runaway chains are
paced elsewhere instead — each new intention still has to clear ``autonomy``'s
gate and its own attempt budget before it costs a turn.
"""

from __future__ import annotations

from typing import Any

from sirius_pulse.core.intent import (
    RESOLUTION_DO,
    IntentFileStore,
    Intention,
)
from sirius_pulse.tools.builtin._internal._qq_ops import current_group_id
from sirius_pulse.tools.engine_context import WorkspaceAccess

_MAX_WHAT_CHARS = 300
_MAX_WHY_CHARS = 200
_DEFAULT_URGENCY = 0.7
_DEFAULT_KIND = "musing"

TOOL_META = {
    "name": "intend_pursue",
    "description": (
        "【适用场景】记下之后想弄明白的事（如待读文章、待验证方案、未解疑问）；"
        "之后早上排日程时你会考虑把它排进去，或者为它开一个项目。\n"
        "【使用方法】what 填写探究目标事项；可选 why 说明关注原因；可选 kind 标注类型（reading/building/note/musing）；"
        "可选 urgency 指定紧迫度（0.0-1.0）。\n"
        "【注意事项】仅登记意图，不立即执行；当轮对话中已查证或已解答的事项不要重复登记。"
    ),
    "version": "1.0.0",
    "model_visible": True,
    "allowed_when_self_initiated": True,
    "side_effect": "external_write",
    "tags": ["autonomy", "intent", "persona"],
    "parameters": {
        "what": {
            "type": "str",
            "description": "具体探究事项的主题或问题描述。",
            "required": True,
            "default": "",
        },
        "why": {
            "type": "str",
            "description": "关注该事项的原因或背景背景。",
            "required": False,
            "default": "",
        },
        "kind": {
            "type": "str",
            "description": "事项分类：reading（阅读）/ building（动手实践）/ note（整理笔记）/ musing（思考构想）。",
            "required": False,
            "default": "",
        },
        "urgency": {
            "type": "float",
            "description": "你有多惦记这件事，0 到 1。越高越可能被尽早去做。",
            "required": False,
            "default": _DEFAULT_URGENCY,
        },
    },
}


def run(
    what: str = "",
    why: str = "",
    kind: str = "",
    urgency: float = _DEFAULT_URGENCY,
    chat_context: dict[str, Any] | None = None,
    engine_context: WorkspaceAccess | None = None,
    invocation_context: Any = None,
) -> dict[str, Any]:
    """Record one do-intention she formed while actually thinking."""
    if engine_context is None:
        return {
            "success": False,
            "error": "缺少 engine_context，无法登记意图",
            "summary": "登记失败",
        }

    body = str(what or "").strip()
    if not body:
        return {"success": False, "error": "what 不能为空", "summary": "没有记下任何内容"}

    intention = Intention.create(
        what=body[:_MAX_WHAT_CHARS],
        why=str(why or "")[:_MAX_WHY_CHARS],
        resolution=RESOLUTION_DO,
        kind=_normalize_kind(kind),
        urgency=_clamp(urgency),
        source="intend_pursue",
        origin_group=_origin_group(chat_context),
        notebook_origin_group=(
            str(invocation_context.group_id or "").strip()
            if invocation_context is not None
            and getattr(invocation_context, "notebook_context_trusted", False)
            else ""
        ),
    )
    store = IntentFileStore(engine_context.get_work_path()).load()
    store.add(intention)
    store.prune()
    IntentFileStore(engine_context.get_work_path()).save(store)

    return {
        "success": True,
        "intention_id": intention.intention_id,
        "summary": "记下了。之后排日程时你会考虑把它排进去。",
    }


def _normalize_kind(kind: Any) -> str:
    value = str(kind or "").strip().lower()
    return value or _DEFAULT_KIND


def _origin_group(chat_context: dict[str, Any] | None) -> str:
    """Remember where this came up, so her own time happens in that context."""
    return current_group_id(chat_context)


def _clamp(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = _DEFAULT_URGENCY
    return max(0.0, min(1.0, number))
