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


def parse_sticker_tags(text: str) -> tuple[str, list[str]]:
    """从回复文本中解析 [STICKERS: "name1", "name2"] 格式的标签。

    Returns:
        (清理后的文本, 选中的表情包名称列表，最多 3 个)
    """
    pattern = r"\[STICKERS:\s*(.+?)\s*\]"
    match = re.search(pattern, text)
    if not match:
        return text, []

    raw = match.group(1)
    names: list[str] = []
    for part in re.split(r"\s*,\s*", raw):
        part = part.strip()
        while part and part[0] in "'\"\u201c\u2018\u300c":
            part = part[1:]
        while part and part[-1] in "'\"\u201d\u2019\u300d":
            part = part[:-1]
        if part:
            names.append(part)

    chosen = names[:3]
    prefix = text[: match.start()].rstrip()
    suffix = text[match.end():].lstrip()
    cleaned_text = f"{prefix} {suffix}".strip() if prefix and suffix else (prefix + suffix)
    return cleaned_text, chosen
