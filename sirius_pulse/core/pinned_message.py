"""消息钉住管理器。

提供消息钉住功能，支持：
- 多条消息同时钉住
- 基于最大携带次数的自动取消机制
- 持久化存储
- 资源限制（最大钉住数量）

存储布局::

    {work_path}/engine_state/
        └── pinned_messages.json
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from sirius_pulse.core.constants import (
    MAX_PINNED_MESSAGES,
    PINNED_MESSAGE_MAX_AGE_HOURS,
    PINNED_MESSAGE_MAX_CARRY_COUNT,
)

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PinnedMessage:
    """钉住的消息数据模型。

    Attributes:
        message_id: 消息唯一标识（由系统生成）
        content: 消息内容
        speaker: 发言者名称
        group_id: 所属群组 ID
        pinned_at: 钉住时间（ISO 8601 格式）
        expires_at: 过期时间（ISO 8601 格式，可选）
        reason: 钉住原因
        metadata: 额外元数据
        max_carry_count: 最大携带次数（超过后自动取消）
        current_carry_count: 当前已携带次数
    """

    message_id: str
    content: str
    speaker: str = ""
    group_id: str = "default"
    pinned_at: str = ""
    expires_at: str | None = None
    reason: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    max_carry_count: int = PINNED_MESSAGE_MAX_CARRY_COUNT
    current_carry_count: int = 0

    def __post_init__(self) -> None:
        """初始化后处理：设置默认钉住时间。"""
        if not self.pinned_at:
            self.pinned_at = datetime.now(timezone.utc).isoformat()

    @property
    def is_expired(self) -> bool:
        """检查消息是否已过期。"""
        if not self.expires_at:
            return False
        try:
            expire_time = datetime.fromisoformat(self.expires_at)
            return datetime.now(timezone.utc) >= expire_time
        except (ValueError, TypeError):
            return False

    @property
    def is_count_exceeded(self) -> bool:
        """检查消息是否已超过最大携带次数。"""
        return self.current_carry_count >= self.max_carry_count

    def increment_carry_count(self) -> None:
        """增加携带次数。"""
        self.current_carry_count += 1

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典。"""
        return {
            "message_id": self.message_id,
            "content": self.content,
            "speaker": self.speaker,
            "group_id": self.group_id,
            "pinned_at": self.pinned_at,
            "expires_at": self.expires_at,
            "reason": self.reason,
            "metadata": self.metadata,
            "max_carry_count": self.max_carry_count,
            "current_carry_count": self.current_carry_count,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PinnedMessage:
        """从字典反序列化。"""
        return cls(
            message_id=data.get("message_id", ""),
            content=data.get("content", ""),
            speaker=data.get("speaker", ""),
            group_id=data.get("group_id", "default"),
            pinned_at=data.get("pinned_at", ""),
            expires_at=data.get("expires_at"),
            reason=data.get("reason", ""),
            metadata=data.get("metadata", {}),
            max_carry_count=data.get("max_carry_count", PINNED_MESSAGE_MAX_CARRY_COUNT),
            current_carry_count=data.get("current_carry_count", 0),
        )


class PinnedMessageManager:
    """消息钉住管理器。

    管理所有钉住的消息，提供钉住/取消/查询等功能。
    支持持久化存储和基于携带次数的自动取消。

    Attributes:
        _pinned_messages: 钉住的消息字典 {message_id: PinnedMessage}
        _max_messages: 最大钉住消息数量限制
        _max_age_hours: 消息最大保留时间（小时）
        _max_carry_count: 最大携带次数
    """

    def __init__(
        self,
        max_messages: int = MAX_PINNED_MESSAGES,
        max_age_hours: float = PINNED_MESSAGE_MAX_AGE_HOURS,
        max_carry_count: int = PINNED_MESSAGE_MAX_CARRY_COUNT,
    ) -> None:
        """初始化消息钉住管理器。

        Args:
            max_messages: 最大钉住消息数量限制
            max_age_hours: 消息最大保留时间（小时）
            max_carry_count: 最大携带次数
        """
        self._pinned_messages: dict[str, PinnedMessage] = {}
        self._max_messages = max_messages
        self._max_age_hours = max_age_hours
        self._max_carry_count = max_carry_count

    def pin_message(
        self,
        content: str,
        speaker: str = "",
        group_id: str = "default",
        reason: str = "",
        metadata: dict[str, Any] | None = None,
        ttl_hours: float | None = None,
        max_carry_count: int | None = None,
    ) -> PinnedMessage:
        """钉住一条消息。

        Args:
            content: 消息内容
            speaker: 发言者名称
            group_id: 所属群组 ID
            reason: 钉住原因
            metadata: 额外元数据
            ttl_hours: 消息存活时间（小时），None 表示使用默认值
            max_carry_count: 最大携带次数，None 表示使用默认值

        Returns:
            钉住的消息对象

        Raises:
            ValueError: 超过最大钉住数量限制
        """
        # 清理过期和超过携带次数的消息
        self._cleanup_expired()
        self._cleanup_exceeded_count()

        # 检查数量限制
        if len(self._pinned_messages) >= self._max_messages:
            # 移除最早钉住的消息
            self._evict_oldest()

        # 生成消息 ID
        message_id = f"pin_{int(time.time() * 1000)}_{len(self._pinned_messages)}"

        # 计算过期时间
        expires_at = None
        if ttl_hours is not None:
            expire_time = datetime.now(timezone.utc) + timedelta(hours=ttl_hours)
            expires_at = expire_time.isoformat()
        elif self._max_age_hours > 0:
            expire_time = datetime.now(timezone.utc) + timedelta(hours=self._max_age_hours)
            expires_at = expire_time.isoformat()

        # 创建钉住消息
        pinned = PinnedMessage(
            message_id=message_id,
            content=content,
            speaker=speaker,
            group_id=group_id,
            expires_at=expires_at,
            reason=reason,
            metadata=metadata or {},
            max_carry_count=max_carry_count or self._max_carry_count,
        )

        self._pinned_messages[message_id] = pinned
        logger.info("消息已钉住: %s (原因: %s)", message_id, reason)

        return pinned

    def unpin_message(self, message_id: str) -> bool:
        """取消钉住一条消息。

        Args:
            message_id: 消息 ID

        Returns:
            是否成功取消
        """
        if message_id in self._pinned_messages:
            del self._pinned_messages[message_id]
            logger.info("消息已取消钉住: %s", message_id)
            return True
        return False

    def unpin_by_reason(self, reason: str) -> int:
        """根据原因取消钉住消息。

        Args:
            reason: 钉住原因

        Returns:
            取消的数量
        """
        to_remove = [
            mid for mid, msg in self._pinned_messages.items()
            if msg.reason == reason
        ]
        for mid in to_remove:
            del self._pinned_messages[mid]

        if to_remove:
            logger.info("根据原因取消钉住 %d 条消息: %s", len(to_remove), reason)

        return len(to_remove)

    def unpin_all(self, group_id: str | None = None) -> int:
        """取消所有钉住消息。

        Args:
            group_id: 过滤指定群组的消息，None 表示所有群组

        Returns:
            取消的数量
        """
        if group_id is not None:
            to_remove = [mid for mid, msg in self._pinned_messages.items() if msg.group_id == group_id]
        else:
            to_remove = list(self._pinned_messages.keys())

        for mid in to_remove:
            del self._pinned_messages[mid]

        if to_remove:
            logger.info("取消钉住 %d 条消息", len(to_remove))

        return len(to_remove)

    def unpin_by_content(self, content_keyword: str) -> int:
        """根据内容关键词取消钉住消息。

        Args:
            content_keyword: 内容关键词

        Returns:
            取消的数量
        """
        to_remove = [
            mid for mid, msg in self._pinned_messages.items()
            if content_keyword in msg.content
        ]
        for mid in to_remove:
            del self._pinned_messages[mid]

        if to_remove:
            logger.info("根据内容取消钉住 %d 条消息: %s", len(to_remove), content_keyword)

        return len(to_remove)

    def get_pinned_messages(
        self,
        group_id: str | None = None,
        include_expired: bool = False,
    ) -> list[PinnedMessage]:
        """获取钉住的消息列表。

        Args:
            group_id: 过滤指定群组的消息，None 表示所有群组
            include_expired: 是否包含已过期的消息

        Returns:
            钉住的消息列表
        """
        # 清理过期和超过携带次数的消息
        self._cleanup_expired()
        self._cleanup_exceeded_count()

        messages = list(self._pinned_messages.values())

        # 按群组过滤
        if group_id is not None:
            messages = [m for m in messages if m.group_id == group_id]

        return messages

    def get_pinned_messages_for_prompt(
        self,
        group_id: str,
    ) -> list[PinnedMessage]:
        """获取钉住的消息列表（用于 prompt 注入），并增加携带计数。

        每次调用此方法，所有返回的消息的携带计数都会增加。
        当携带计数超过最大携带次数时，消息会被自动取消钉住。

        Args:
            group_id: 群组 ID

        Returns:
            钉住的消息列表
        """
        messages = self.get_pinned_messages(group_id=group_id, include_expired=False)

        # 增加携带计数
        for msg in messages:
            msg.increment_carry_count()

        # 清理超过携带次数的消息
        self._cleanup_exceeded_count()

        return messages

    def get_context_for_prompt(
        self,
        group_id: str,
        max_tokens: int | None = None,
    ) -> str:
        """获取钉住消息的上下文文本（用于 prompt 注入）。

        Args:
            group_id: 群组 ID
            max_tokens: 最大 token 限制

        Returns:
            格式化的钉住消息上下文
        """
        messages = self.get_pinned_messages(group_id=group_id, include_expired=False)

        if not messages:
            return ""

        # 构建上下文
        lines = ["【钉住的重要消息】"]
        for msg in messages:
            speaker_info = f"（{msg.speaker}）" if msg.speaker else ""
            lines.append(f"- {speaker_info}{msg.content}")

        return "\n".join(lines)

    def get_statistics(self) -> dict[str, Any]:
        """获取钉住消息的统计信息。

        Returns:
            统计信息字典
        """
        messages = list(self._pinned_messages.values())
        by_group = {}

        for msg in messages:
            by_group[msg.group_id] = by_group.get(msg.group_id, 0) + 1

        return {
            "total": len(messages),
            "by_group": by_group,
            "max_messages": self._max_messages,
            "max_carry_count": self._max_carry_count,
        }

    def _cleanup_expired(self) -> int:
        """清理过期的消息。

        Returns:
            清理的数量
        """
        to_remove = [
            mid for mid, msg in self._pinned_messages.items()
            if msg.is_expired
        ]
        for mid in to_remove:
            del self._pinned_messages[mid]

        return len(to_remove)

    def _cleanup_exceeded_count(self) -> int:
        """清理超过最大携带次数的消息。

        Returns:
            清理的数量
        """
        to_remove = [
            mid for mid, msg in self._pinned_messages.items()
            if msg.is_count_exceeded
        ]
        for mid in to_remove:
            del self._pinned_messages[mid]
            logger.info("消息已超过最大携带次数，自动取消钉住: %s", mid)

        return len(to_remove)

    def _evict_oldest(self) -> bool:
        """移除最早钉住的消息以腾出空间。

        Returns:
            是否成功移除
        """
        if not self._pinned_messages:
            return False

        # 找到最早钉住的消息
        oldest = min(
            self._pinned_messages.values(),
            key=lambda m: m.pinned_at,
        )

        del self._pinned_messages[oldest.message_id]
        logger.info("移除最早钉住的消息以腾出空间: %s", oldest.message_id)
        return True

    def to_dict(self) -> dict[str, Any]:
        """序列化为字典（用于持久化）。"""
        return {
            "messages": {mid: msg.to_dict() for mid, msg in self._pinned_messages.items()},
            "max_messages": self._max_messages,
            "max_age_hours": self._max_age_hours,
            "max_carry_count": self._max_carry_count,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PinnedMessageManager:
        """从字典反序列化。

        Args:
            data: 序列化的数据

        Returns:
            恢复的管理器实例
        """
        manager = cls(
            max_messages=data.get("max_messages", MAX_PINNED_MESSAGES),
            max_age_hours=data.get("max_age_hours", PINNED_MESSAGE_MAX_AGE_HOURS),
            max_carry_count=data.get("max_carry_count", PINNED_MESSAGE_MAX_CARRY_COUNT),
        )

        messages_data = data.get("messages", {})
        for mid, msg_data in messages_data.items():
            try:
                manager._pinned_messages[mid] = PinnedMessage.from_dict(msg_data)
            except Exception as exc:
                logger.warning("反序列化钉住消息失败 %s: %s", mid, exc)

        return manager


# ---------------------------------------------------------------------------
# 钉住指令解析
# ---------------------------------------------------------------------------

# 钉住指令正则模式：
# [PIN_MESSAGE: {"content": "...", "reason": "..."}]  - 钉住指定内容
# [PIN_MESSAGE: {"reason": "..."}]  - 钉住当前用户消息
# [PIN_MESSAGE: {"index": -1, "reason": "..."}]  - 钉住最近的第N条消息
PIN_MESSAGE_PATTERN = re.compile(
    r'\[PIN_MESSAGE:\s*(\{.*?\})\s*\]',
    re.DOTALL,
)

# 取消钉住指令正则模式：
# [UNPIN_MESSAGE: {"reason": "..."}]  - 根据原因取消钉住
# [UNPIN_MESSAGE: {"content": "..."}]  - 根据内容关键词取消钉住
# [UNPIN_MESSAGE: {"all": true}]  - 取消所有钉住
UNPIN_MESSAGE_PATTERN = re.compile(
    r'\[UNPIN_MESSAGE:\s*(\{.*?\})\s*\]',
    re.DOTALL,
)


def parse_pin_messages(text: str) -> list[dict[str, Any]]:
    """从文本中解析钉住指令。

    指令格式：
    - [PIN_MESSAGE: {"content": "...", "reason": "..."}]  - 钉住指定内容
    - [PIN_MESSAGE: {"reason": "..."}]  - 钉住当前用户消息（content 为空时）
    - [PIN_MESSAGE: {"index": -1, "reason": "..."}]  - 钉住最近的第N条消息

    Args:
        text: 包含钉住指令的文本

    Returns:
        解析后的钉住指令列表
    """
    results: list[dict[str, Any]] = []
    for match in PIN_MESSAGE_PATTERN.finditer(text):
        params_raw = match.group(1)
        try:
            parsed = json.loads(params_raw)
            if isinstance(parsed, dict):
                result = {
                    "content": str(parsed.get("content", "")),
                    "reason": str(parsed.get("reason", "")),
                    "index": int(parsed.get("index", 0)),
                }
                results.append(result)
        except (json.JSONDecodeError, ValueError):
            logger.warning("PIN_MESSAGE 指令解析失败: %s", params_raw)
    return results


def parse_unpin_messages(text: str) -> list[dict[str, Any]]:
    """从文本中解析取消钉住指令。

    指令格式：
    - [UNPIN_MESSAGE: {"reason": "..."}]  - 根据原因取消钉住
    - [UNPIN_MESSAGE: {"content": "..."}]  - 根据内容关键词取消钉住
    - [UNPIN_MESSAGE: {"all": true}]  - 取消所有钉住

    Args:
        text: 包含取消钉住指令的文本

    Returns:
        解析后的取消钉住指令列表
    """
    results: list[dict[str, Any]] = []
    for match in UNPIN_MESSAGE_PATTERN.finditer(text):
        params_raw = match.group(1)
        try:
            parsed = json.loads(params_raw)
            if isinstance(parsed, dict):
                result = {
                    "reason": str(parsed.get("reason", "")),
                    "content": str(parsed.get("content", "")),
                    "all": bool(parsed.get("all", False)),
                }
                results.append(result)
        except (json.JSONDecodeError, ValueError):
            logger.warning("UNPIN_MESSAGE 指令解析失败: %s", params_raw)
    return results


def strip_pin_messages(text: str) -> str:
    """从文本中移除所有钉住指令标记。

    Args:
        text: 包含钉住指令的文本

    Returns:
        移除指令后的文本
    """
    text = PIN_MESSAGE_PATTERN.sub("", text)
    text = UNPIN_MESSAGE_PATTERN.sub("", text)
    return text.strip()
