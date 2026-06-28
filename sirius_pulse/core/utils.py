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


