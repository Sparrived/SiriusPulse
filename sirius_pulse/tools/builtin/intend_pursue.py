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

Deliberately **not** allowed during a self-initiated turn
(``allowed_when_self_initiated`` stays false): pursuing one thing must not
silently register the next, or every turn would feed the following one.
"""

from __future__ import annotations

from typing import Any

from sirius_pulse.core.intent import (
    RESOLUTION_DO,
    IntentFileStore,
    Intention,
)
from sirius_pulse.tools.builtin._internal._qq_ops import current_group_id

_MAX_WHAT_CHARS = 300
_MAX_WHY_CHARS = 200
_DEFAULT_URGENCY = 0.7
_DEFAULT_KIND = "musing"

TOOL_META = {
    "name": "intend_pursue",
    "description": (
        "当你遇到一件想自己弄清楚的事（一个没看明白的说法、一篇想读进去的文章、"
        "一个想动手试试的想法），用这个工具把它记下来：写下你想弄明白的是什么、"
        "为什么在意。它只登记，不会立刻去做；之后你会在自己的时间里决定做不做。"
        "这次回复里已经读过、查过、弄明白的事不要再登记。"
    ),
    "version": "1.0.0",
    "model_visible": True,
    "side_effect": "external_write",
    "tags": ["autonomy", "intent", "persona"],
    "parameters": {
        "what": {
            "type": "str",
            "description": "你想弄明白的是什么，用你自己的话写（例如「潮汐为什么一天有两次」）。",
            "required": True,
            "default": "",
        },
        "why": {
            "type": "str",
            "description": "你为什么在意这件事，比如好奇、觉得和之前聊到的有关、想弄懂再讲给别人听。",
            "required": False,
            "default": "",
        },
        "kind": {
            "type": "str",
            "description": "这件事属于哪类：reading 想读进去 / building 想动手做 / note 想记下来 / musing 只是在想。不确定就留空。",
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
    engine_context: Any = None,
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
    )
    store = IntentFileStore(engine_context.get_work_path()).load()
    store.add(intention)
    store.prune()
    IntentFileStore(engine_context.get_work_path()).save(store)

    return {
        "success": True,
        "intention_id": intention.intention_id,
        "summary": "记下了。之后你自己的时间里会决定要不要去做这件事。",
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
