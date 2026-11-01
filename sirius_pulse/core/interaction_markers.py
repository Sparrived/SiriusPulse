"""统一解析模型输出的交互标记。

``[STICKER:名称]`` / ``[POKE:QQ号]`` 是系统控制语法，必须在文本真正发出去
之前剥离，否则标记会原样出现在聊天里。解析规则集中在这里：聊天回复的
post-hook、主动消息出口、工作模式对外发言共用同一套正则，避免各通道各写
一份、漏掉全角冒号或全角括号这类模型常见变体。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Collection, Iterable

# 模型偶尔会在标记里塞零宽字符或换行，这里一并容忍。
_SPACE = r"[\s\u200b]*"
_NAME_SEPARATORS = re.compile(r"[,，、;；]")
_NAME_QUOTES = "\"'“”‘’「」『』"

STICKER_MARKER_RE = re.compile(
    rf"[\[［【]{_SPACE}STICKERS?{_SPACE}[:：]{_SPACE}" rf"([^\[\]［］【】\r\n]+?){_SPACE}[\]］】]",
    re.IGNORECASE,
)
POKE_MARKER_RE = re.compile(
    rf"[\[［【]{_SPACE}POKE{_SPACE}[:：]{_SPACE}(\d+){_SPACE}[\]］】]",
    re.IGNORECASE,
)


@dataclass(slots=True)
class InteractionMarkers:
    """从一段文本中剥离出来的交互标记。"""

    text: str
    sticker_names: list[str] = field(default_factory=list)
    poke_user_ids: list[str] = field(default_factory=list)


def _clean_name(raw: str) -> str:
    return raw.strip().strip(_NAME_QUOTES).strip()


def _iter_sticker_names(text: str) -> Iterable[str]:
    for raw in STICKER_MARKER_RE.findall(text or ""):
        for name in _NAME_SEPARATORS.split(raw):
            cleaned = _clean_name(name)
            if cleaned:
                yield cleaned


def extract_sticker_names(text: str, available: Collection[str] | None = None) -> list[str]:
    """按出现顺序返回词表内的表情包名称（已去重）。"""
    allowed = set(available) if available is not None else None
    names = [name for name in _iter_sticker_names(text) if allowed is None or name in allowed]
    return list(dict.fromkeys(names))


def extract_poke_user_ids(text: str, allowed: Collection[str] | None = None) -> list[str]:
    """返回文本里出现且被授权的 QQ 号（已去重）。"""
    allowed_ids = {str(value) for value in allowed} if allowed is not None else None
    ids = [
        value
        for value in POKE_MARKER_RE.findall(text or "")
        if allowed_ids is None or value in allowed_ids
    ]
    return list(dict.fromkeys(ids))


def strip_interaction_markers(text: str) -> str:
    """删除全部表情包/戳一戳标记，返回可以发送的正文。"""
    body = STICKER_MARKER_RE.sub("", text or "")
    body = POKE_MARKER_RE.sub("", body)
    return body.strip()


def parse_interaction_markers(
    text: str,
    *,
    available_stickers: Collection[str] | None = None,
    allowed_poke_ids: Collection[str] | None = None,
) -> InteractionMarkers:
    """一次取出正文、表情包名称与可戳对象。"""
    raw = text or ""
    return InteractionMarkers(
        text=strip_interaction_markers(raw),
        sticker_names=extract_sticker_names(raw, available_stickers),
        poke_user_ids=extract_poke_user_ids(raw, allowed_poke_ids),
    )


def collect_allowed_poke_user_ids(
    engine: Any,
    group_id: str,
    *,
    extra: Iterable[str] = (),
) -> set[str]:
    """收集当前会话里真实出现过的 QQ 号；私聊不产生可戳对象。"""
    allowed = {str(value).strip() for value in extra if str(value).strip().isdigit()}
    gid = str(group_id or "").strip()
    if not gid or gid.startswith("private_"):
        return allowed

    getter = getattr(engine, "get_qq_group_members_for_prompt", None)
    if callable(getter):
        try:
            members = getter(gid) or []
        except Exception:
            members = []
        for member in members:
            if not isinstance(member, dict):
                continue
            user_id = str(member.get("user_id", "")).strip()
            if user_id.isdigit():
                allowed.add(user_id)

    memory = getattr(engine, "basic_memory", None)
    if memory is not None:
        try:
            entries = memory.get_all(gid)
        except Exception:
            entries = []
        for entry in entries:
            for attr in ("channel_user_id", "user_id"):
                user_id = str(getattr(entry, attr, "") or "").strip()
                if user_id.isdigit():
                    allowed.add(user_id)
    return allowed
