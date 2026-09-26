"""Persona data models for EmotionalGroupChatEngine."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class PersonaProfile:
    """Prompt-backed persona metadata used by the runtime."""

    # Identity metadata and the complete prompt written by the user.
    name: str = "小星"
    aliases: list[str] = field(default_factory=list)
    full_system_prompt: str = ""

    # Runtime controls are separate from the persona prompt.
    # 采样参数（max_tokens / temperature）不在此处：它们由 AMKR 的任务定义持有。
    reply_frequency: str = "moderate"

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------
    version: str = "1.0"
    created_at: str = ""
    source: str = "manual"  # manual/roleplay_bridge

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "aliases": list(self.aliases),
            "full_system_prompt": self.full_system_prompt,
            "reply_frequency": self.reply_frequency,
            "version": self.version,
            "created_at": self.created_at,
            "source": self.source,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PersonaProfile":
        """从磁盘/WebUI 的字典构造。

        历史上写入的 ``max_tokens_preference`` / ``temperature_preference`` 会被
        忽略：采样参数已归 AMKR 的任务定义所有，本框架不再持有。
        """
        return cls(
            name=data.get("name", "小星"),
            aliases=list(data.get("aliases", [])),
            full_system_prompt=data.get("full_system_prompt", ""),
            reply_frequency=data.get("reply_frequency", "moderate"),
            version=data.get("version", "1.0"),
            created_at=data.get("created_at", ""),
            source=data.get("source", "template"),
        )

    # ------------------------------------------------------------------
    # Prompt builder
    # ------------------------------------------------------------------

    def build_system_prompt(self) -> str:
        """构建发送给 LLM 的角色 prompt。委托 PromptFactory。"""
        from sirius_pulse.core.prompt_factory import PromptFactory

        return PromptFactory.build_persona_prompt(
            name=self.name,
            aliases=self.aliases,
            full_system_prompt=self.full_system_prompt,
        )
