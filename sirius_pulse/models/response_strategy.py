"""Response strategy models: four-layer decision system."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ResponseStrategy(Enum):
    """Four-layer response strategy (paper §2.3 / §6)."""

    IMMEDIATE = "immediate"
    DELAYED = "delayed"
    SILENT = "silent"
    PROACTIVE = "proactive"
    PLUGIN = "plugin"  # Plugin 命令快速路径（v1.2+）


@dataclass(slots=True)
class StrategyDecision:
    """Decision produced by ResponseStrategyEngine."""

    strategy: ResponseStrategy = ResponseStrategy.SILENT
    score: float = 0.0
    threshold: float = 0.5
    urgency: float = 0.0
    relevance: float = 0.0
    reason: str = ""
    estimated_delay_seconds: float = 0.0
    context: dict[str, Any] = field(default_factory=dict)

    # === Plugin 命令字段（v1.2+）===
    plugin_intent: str | None = None  # Plugin 名称
    plugin_slots: dict[str, Any] = field(default_factory=dict)  # 参数槽位
    plugin_render_mode: str = "direct"  # 渲染模式


@dataclass(slots=True)
class DelayedResponseItem:
    """Item queued in DelayedResponseQueue."""

    item_id: str = ""
    group_id: str = ""
    user_id: str = ""
    channel: str | None = None
    channel_user_id: str | None = None
    message_content: str = ""
    speaker_name: str = ""
    strategy_decision: StrategyDecision = field(default_factory=StrategyDecision)
    emotion_state: dict[str, Any] = field(default_factory=dict)
    candidate_memories: list[str] = field(default_factory=list)
    enqueue_time: str = ""
    window_seconds: float = 30.0
    status: str = "pending"  # pending | triggered | cancelled | sent
    multimodal_inputs: list[dict[str, str]] = field(default_factory=list)
    adapter_type: str | None = None
    heat_level: str = "warm"  # cold | warm | hot | overheated
    pace: str = "steady"  # accelerating | steady | decelerating | silent
    related_user_ids: list[str] = field(
        default_factory=list
    )  # merged messages may involve multiple users

    def to_dict(self) -> dict[str, Any]:
        return {
            "item_id": self.item_id,
            "group_id": self.group_id,
            "user_id": self.user_id,
            "channel": self.channel,
            "channel_user_id": self.channel_user_id,
            "message_content": self.message_content,
            "strategy_decision": self.strategy_decision.__dict__,
            "emotion_state": self.emotion_state,
            "candidate_memories": self.candidate_memories,
            "enqueue_time": self.enqueue_time,
            "window_seconds": self.window_seconds,
            "status": self.status,
            "multimodal_inputs": self.multimodal_inputs,
            "heat_level": self.heat_level,
            "pace": self.pace,
            "related_user_ids": list(self.related_user_ids),
        }
