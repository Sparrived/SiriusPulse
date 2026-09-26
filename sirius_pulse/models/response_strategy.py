"""Response strategy models: decision system."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class ResponseStrategy(Enum):
    """Response strategy (paper §2.3 / §6)."""

    IMMEDIATE = "immediate"
    DELAYED = "delayed"
    SILENT = "silent"
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

    def to_dict(self) -> dict[str, Any]:
        """序列化为可 JSON 化的字典。

        dataclass 用了 ``slots=True``，所以没有 ``__dict__`` 可取；这里显式列举
        字段，避免 ``to_dict()`` 在延迟队列持久化路径上抛 AttributeError。
        """
        return {
            "strategy": self.strategy.value
            if isinstance(self.strategy, ResponseStrategy)
            else str(self.strategy),
            "score": self.score,
            "threshold": self.threshold,
            "urgency": self.urgency,
            "relevance": self.relevance,
            "reason": self.reason,
            "estimated_delay_seconds": self.estimated_delay_seconds,
            "context": dict(self.context or {}),
            "plugin_intent": self.plugin_intent,
            "plugin_slots": dict(self.plugin_slots or {}),
            "plugin_render_mode": self.plugin_render_mode,
        }


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
    candidate_memories: list[str] = field(default_factory=list)
    enqueue_time: str = ""
    window_seconds: float = 30.0
    status: str = "pending"  # pending | triggered | cancelled | sent
    multimodal_inputs: list[dict[str, str]] = field(default_factory=list)
    adapter_type: str | None = None
    adapter_route_id: str | None = None
    heat_level: str = "warm"  # cold | warm | hot | overheated
    pace: str = "steady"  # accelerating | steady | decelerating | silent
    related_user_ids: list[str] = field(
        default_factory=list
    )  # merged messages may involve multiple users
    retry_count: int = 0  # 生成/投递失败后重新入队的次数，用于封顶避免死循环

    def to_dict(self) -> dict[str, Any]:
        # StrategyDecision 是 slots=True 的 dataclass，没有 __dict__；早先这里直接
        # 取 __dict__ 会 AttributeError，所以延迟队列的持久化一直没能接上。
        return {
            "item_id": self.item_id,
            "group_id": self.group_id,
            "user_id": self.user_id,
            "channel": self.channel,
            "channel_user_id": self.channel_user_id,
            "message_content": self.message_content,
            "speaker_name": self.speaker_name,
            "strategy_decision": self.strategy_decision.to_dict(),
            "candidate_memories": self.candidate_memories,
            "enqueue_time": self.enqueue_time,
            "window_seconds": self.window_seconds,
            "status": self.status,
            "multimodal_inputs": self.multimodal_inputs,
            "adapter_type": self.adapter_type,
            "adapter_route_id": self.adapter_route_id,
            "heat_level": self.heat_level,
            "pace": self.pace,
            "related_user_ids": list(self.related_user_ids),
            "retry_count": self.retry_count,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "DelayedResponseItem":
        """从持久化字典恢复条目；坏字段逐项降级而不是丢整条。

        恢复路径只在进程重启时走一次，宁可少错一个字段也不能让整条消息
        无声消失，因此所有取值都做兜底。
        """
        sd_raw = data.get("strategy_decision")
        sd_raw = sd_raw if isinstance(sd_raw, dict) else {}
        try:
            strategy = ResponseStrategy(sd_raw.get("strategy", "silent"))
        except (ValueError, TypeError):
            strategy = ResponseStrategy.SILENT

        def _float(key: str, default: float) -> float:
            try:
                return float(sd_raw.get(key, default))
            except (TypeError, ValueError):
                return default

        context = sd_raw.get("context")
        strategy_decision = StrategyDecision(
            strategy=strategy,
            score=_float("score", 0.0),
            threshold=_float("threshold", 0.5),
            urgency=_float("urgency", 0.0),
            relevance=_float("relevance", 0.0),
            reason=str(sd_raw.get("reason", "") or ""),
            estimated_delay_seconds=_float("estimated_delay_seconds", 0.0),
            context=dict(context) if isinstance(context, dict) else {},
        )

        def _list(key: str) -> list[Any]:
            value = data.get(key)
            return list(value) if isinstance(value, list) else []

        try:
            window_seconds = float(data.get("window_seconds", 30.0))
        except (TypeError, ValueError):
            window_seconds = 30.0
        try:
            retry_count = max(0, int(data.get("retry_count", 0) or 0))
        except (TypeError, ValueError):
            retry_count = 0

        channel = data.get("channel")
        channel_user_id = data.get("channel_user_id")
        adapter_type = data.get("adapter_type")
        adapter_route_id = data.get("adapter_route_id")
        return cls(
            item_id=str(data.get("item_id", "") or ""),
            group_id=str(data.get("group_id", "") or ""),
            user_id=str(data.get("user_id", "") or ""),
            channel=str(channel) if channel is not None else None,
            channel_user_id=str(channel_user_id) if channel_user_id is not None else None,
            message_content=str(data.get("message_content", "") or ""),
            speaker_name=str(data.get("speaker_name", "") or ""),
            strategy_decision=strategy_decision,
            candidate_memories=[str(m) for m in _list("candidate_memories")],
            enqueue_time=str(data.get("enqueue_time", "") or ""),
            window_seconds=window_seconds,
            status=str(data.get("status", "pending") or "pending"),
            multimodal_inputs=[dict(m) for m in _list("multimodal_inputs") if isinstance(m, dict)],
            adapter_type=str(adapter_type) if adapter_type is not None else None,
            adapter_route_id=str(adapter_route_id) if adapter_route_id is not None else None,
            heat_level=str(data.get("heat_level", "warm") or "warm"),
            pace=str(data.get("pace", "steady") or "steady"),
            related_user_ids=[str(u) for u in _list("related_user_ids") if u],
            retry_count=retry_count,
        )
