"""Basic memory data models."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class BasicMemoryEntry:
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "entry_id": self.entry_id,
            "group_id": self.group_id,
            "user_id": self.user_id,
            "speaker_name": self.speaker_name,
            "role": self.role,
            "content": self.content,
            "timestamp": self.timestamp,
            "system_prompt": self.system_prompt,
            "channel_user_id": self.channel_user_id,
            "multimodal_inputs": self.multimodal_inputs,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BasicMemoryEntry":
        return cls(
            entry_id=data.get("entry_id", ""),
            group_id=data.get("group_id", ""),
            user_id=data.get("user_id", ""),
            speaker_name=data.get("speaker_name", ""),
            role=data.get("role", "human"),
            content=data.get("content", ""),
            timestamp=data.get("timestamp", ""),
            system_prompt=data.get("system_prompt", ""),
            channel_user_id=data.get("channel_user_id", ""),
            multimodal_inputs=[
                dict(item) for item in data.get("multimodal_inputs", [])
                if isinstance(item, dict)
            ],
        )


@dataclass(slots=True)
class HeatState:
    """Per-group heat tracking for cold-detection."""

    message_count_5min: int = 0
    last_message_at: str = ""
    unique_speakers_5min: int = 0
    avg_interval_sec: float = 0.0
