"""Basic memory data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sirius_pulse.mixins import JsonSerializable


@dataclass(slots=True)
class BasicMemoryEntry(JsonSerializable):
    """A single entry in the basic memory window.

    All messages (human, assistant, system) are stored verbatim
    for full archival and later diary promotion.
    """

    entry_id: str
    group_id: str
    user_id: str           # real user_id, "assistant", or "system"
    role: str              # "human" | "assistant" | "system"
    content: str
    timestamp: str         # ISO 8601
    speaker_name: str = "" # display name (nickname, card, or persona name)
    system_prompt: str = ""  # system prompt used for this assistant turn
    channel_user_id: str = ""  # platform raw id (e.g. QQ number) for name constraints
    multimodal_inputs: list[dict[str, str]] = field(default_factory=list)
    tags: list[dict[str, str]] = field(default_factory=list)  # 内容标签（表情包、钉住等）
    conversation_chain: list[dict[str, Any]] = field(default_factory=list)  # LLM 消息链


@dataclass(slots=True)
class HeatState(JsonSerializable):
    """Per-group heat tracking for cold-detection."""

    message_count_5min: int = 0
    last_message_at: str = ""
    unique_speakers_5min: int = 0
    avg_interval_sec: float = 0.0
