"""Shared utilities for the core engine layer."""

from __future__ import annotations

import re
from datetime import datetime, timezone


def now_iso() -> str:
    """Return the current UTC timestamp as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()


def strip_conversation_history_xml(text: str) -> str:
    """移除 LLM 模型可能回显的 conversation_history XML 块。

    因为短期记忆以 XML 格式嵌入 system prompt，部分模型会在输出中
    模仿该格式。此函数清理这些意外出现的 XML 块。
    """
    if not text:
        return text
    cleaned = re.sub(
        r"<\s*conversation_history\s*[^>]*>.*?</\s*conversation_history\s*>",
        "",
        text,
        flags=re.DOTALL | re.IGNORECASE,
    )
    return cleaned.strip()


def parse_sticker_tags(
    text: str,
    sticker_names: list[str] | None = None,
) -> tuple[str, list[str]]:
    """从回复文本中解析表情包标签。

    支持两种格式：
    1. ``[STICKERS: "name1", "name2"]``  — 标准格式（只解析第一个）
    2. ``[keyword]``                      — 当 keyword 精确匹配已有表情包名称时，
       视为表情包信号（模型有时误用此格式）

    两种格式可同时出现，结果合并（最多 3 个）。
    所有匹配到的标签均从文本中移除。

    Args:
        text: 待解析的回复文本
        sticker_names: 已有的表情包名称列表，用于匹配 ``[keyword]`` 格式。
            为 None 或空列表时跳过 ``[keyword]`` 匹配。

    Returns:
        (清理后的文本, 选中的表情包名称列表，最多 3 个)
    """
    names: list[str] = []
    cleaned = text

    # 1. 解析标准 [STICKERS: ...] 标签（只取第一个）
    sticker_pattern = r"\[STICKERS[：:]\s*(.+?)\s*\]"
    match = re.search(sticker_pattern, cleaned)
    if match:
        raw = match.group(1)
        for part in re.split(r"\s*,\s*", raw):
            part = part.strip()
            while part and part[0] in "'\"\u201c\u2018\u300c":
                part = part[1:]
            while part and part[-1] in "'\"\u201d\u2019\u300d":
                part = part[:-1]
            if part:
                names.append(part)
        cleaned = re.sub(sticker_pattern, "", cleaned)

    # 2. 解析 [keyword] 格式：仅当 keyword 精确匹配已有表情包名称时识别
    if sticker_names and len(names) < 3:
        known = set(sticker_names)
        bracket_pattern = r"\[([^\[\]]+)\]"
        remaining_slots = 3 - len(names)
        for m in re.finditer(bracket_pattern, cleaned):
            if remaining_slots <= 0:
                break
            kw = m.group(1).strip()
            if kw in known:
                names.append(kw)
                remaining_slots -= 1
        # 从文本中移除所有匹配到的表情包 [keyword]
        if known:

            def _replace_bracket(m: re.Match[str]) -> str:
                return "" if m.group(1).strip() in known else m.group(0)

            cleaned = re.sub(bracket_pattern, _replace_bracket, cleaned)

    chosen = names[:3]
    cleaned = cleaned.strip()
    return cleaned, chosen
