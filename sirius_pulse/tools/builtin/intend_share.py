"""Built-in TOOL that lets a persona record something she wants to say.

This is the second half of autonomy.  A turn can produce material she wants to
share — a thought, a question, a link — and rather than broadcasting it
immediately, she records *what* she wants to say and *who* she wants to say it
to.  The generic autonomy tick later decides whether and when to actually say it,
so "想说" and "说出来" stay separate decisions.

The TOOL never sends anything itself.  Delivery happens on a later tick, where it
goes through the normal proactive-message pipeline.
"""

from __future__ import annotations

import logging
from typing import Any

from sirius_pulse.core.intent import (
    RESOLUTION_TELL,
    IntentFileStore,
    Intention,
)

logger = logging.getLogger(__name__)

_MAX_WHAT_CHARS = 300
_MAX_WHY_CHARS = 200
_DEFAULT_URGENCY = 0.7

TOOL_META = {
    "name": "intend_share",
    "description": (
        "当你心里有一件想告诉某人的事（一个想法、一个问题、一篇文章、一点情绪），"
        "用这个工具把它记下来，并说明想说给谁。它只登记，不会立刻发送；"
        "之后你会在合适的时机自己决定说不说。"
    ),
    "version": "1.0.0",
    "model_visible": True,
    "side_effect": "external_write",
    "allowed_when_self_initiated": True,
    "tags": ["autonomy", "intent", "share", "persona"],
    "parameters": {
        "what": {
            "type": "str",
            "description": "你想说的内容本身，尽量保留你原本的语气。已用 intention_id 指定旧意图时可留空。",
            "required": False,
            "default": "",
        },
        "why": {
            "type": "str",
            "description": "为什么想说出来，比如想听听对方怎么看、只是想分享。",
            "required": False,
            "default": "",
        },
        "audience": {
            "type": "str",
            "description": "想说给谁听。填候选受众里的 chat_id；留空表示还没想好，稍后再定。",
            "required": False,
            "default": "",
        },
        "intention_id": {
            "type": "str",
            "description": "你之前已经记下、只是还没想好说给谁的那件事，填它的 id 来补上受众，不要重复登记同样的内容。",
            "required": False,
            "default": "",
        },
        "urgency": {
            "type": "float",
            "description": "你有多想说出来，0 到 1。越高越可能被尽早说出。",
            "required": False,
            "default": _DEFAULT_URGENCY,
        },
    },
}


def run(
    what: str = "",
    why: str = "",
    audience: str = "",
    urgency: float = _DEFAULT_URGENCY,
    intention_id: str = "",
    engine_context: Any = None,
) -> dict[str, Any]:
    """Record one tell-intention, or decide who an existing one is for.

    Passing ``intention_id`` attaches an audience to something she already wanted
    to say, instead of registering a second copy of the same words.
    """
    if engine_context is None:
        return {
            "success": False,
            "error": "缺少 engine_context，无法登记意图",
            "summary": "登记失败",
        }

    store = _load_store(engine_context)

    if intention_id:
        return _attach_audience(engine_context, store, intention_id, audience)

    body = str(what or "").strip()
    if not body:
        return {"success": False, "error": "what 不能为空", "summary": "没有记下任何内容"}

    label = _audience_label(engine_context, audience)
    intention = Intention.create(
        what=body[:_MAX_WHAT_CHARS],
        why=str(why or "")[:_MAX_WHY_CHARS],
        resolution=RESOLUTION_TELL,
        kind="share",
        audience=str(audience or "").strip(),
        audience_label=label,
        urgency=_clamp(urgency),
        source="intend_share",
    )
    store.add(intention)
    store.prune()
    _save_store(engine_context, store)

    if audience:
        note = f"已记下想对「{label}」说的话"
    else:
        note = "已记下想说的事（还没想好说给谁）"
    return {
        "success": True,
        "intention_id": intention.intention_id,
        "audience": intention.audience,
        "summary": f"{note}；之后合适的时候你会自己决定说不说。",
    }


def _attach_audience(
    engine_context: Any, store: Any, intention_id: str, audience: str
) -> dict[str, Any]:
    chat_id = str(audience or "").strip()
    if not chat_id:
        return {"success": False, "error": "audience 不能为空", "summary": "没有指定说给谁"}
    label = _audience_label(engine_context, chat_id)
    item = store.attach_audience(intention_id, audience=chat_id, label=label)
    if item is None:
        return {
            "success": False,
            "error": f"找不到可改的意图: {intention_id}",
            "summary": "这件事已经说过或不存在了",
        }
    _save_store(engine_context, store)
    return {
        "success": True,
        "intention_id": item.intention_id,
        "audience": item.audience,
        "summary": f"好，这句话之后找机会说给「{label}」听。",
    }


def _load_store(engine_context: Any) -> Any:
    return IntentFileStore(engine_context.get_work_path()).load()


def _save_store(engine_context: Any, store: Any) -> None:
    IntentFileStore(engine_context.get_work_path()).save(store)


def _audience_label(engine_context: Any, audience: str) -> str:
    chat_id = str(audience or "").strip()
    if not chat_id:
        return ""
    try:
        for candidate in engine_context.list_audiences():
            if candidate.chat_id == chat_id:
                return candidate.label
    except Exception:
        logger.debug("解析受众标签失败: %s", chat_id)
    return chat_id


def _clamp(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        number = _DEFAULT_URGENCY
    return max(0.0, min(1.0, number))
