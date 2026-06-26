"""助手-管家通信协议定义。

管家端 (Butler) 与助手端 (Assistant) 通过 WebSocket 长连接通信，
协调人格的 NapCat 消息处理控制权。
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Any


class MessageType(str, Enum):
    """协议消息类型。"""

    # 助手端 → 管家端
    HELLO = "hello"  # 握手，携带身份信息
    TAKEOVER = "takeover"  # 请求接管指定人格
    RELEASE = "release"  # 主动释放控制权
    HEARTBEAT = "heartbeat"  # 心跳保活

    # 管家端 → 助手端
    TAKEOVER_ACK = "takeover_ack"  # 接管确认
    TAKEOVER_NACK = "takeover_nack"  # 接管拒绝
    RELEASE_ACK = "release_ack"  # 释放确认
    ERROR = "error"  # 错误响应


@dataclass(frozen=True)
class ButlerMessage:
    """协议消息。"""

    type: MessageType
    persona_name: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)

    def to_json(self) -> str:
        """序列化为 JSON 字符串。"""
        d = asdict(self)
        d["type"] = self.type.value
        return json.dumps(d, ensure_ascii=False)

    @classmethod
    def from_json(cls, raw: str) -> ButlerMessage:
        """从 JSON 字符串反序列化。

        Raises:
            ValueError: 消息格式无效或 type 不合法。
        """
        try:
            d = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ValueError(f"无效的 JSON: {e}") from e

        msg_type_raw = d.get("type")
        if msg_type_raw is None:
            raise ValueError("缺少 type 字段")

        try:
            msg_type = MessageType(msg_type_raw)
        except ValueError as e:
            raise ValueError(f"未知消息类型: {msg_type_raw}") from e

        return cls(
            type=msg_type,
            persona_name=d.get("persona_name", ""),
            payload=d.get("payload", {}),
            timestamp=d.get("timestamp", time.time()),
        )


def make_takeover(*, token: str | None = None) -> ButlerMessage:
    """构建 takeover 请求。"""
    payload: dict[str, Any] = {}
    if token:
        payload["token"] = token
    return ButlerMessage(type=MessageType.TAKEOVER, payload=payload)


def make_release() -> ButlerMessage:
    """构建 release 请求。"""
    return ButlerMessage(type=MessageType.RELEASE)


def make_heartbeat() -> ButlerMessage:
    """构建心跳消息。"""
    return ButlerMessage(type=MessageType.HEARTBEAT)


def make_takeover_ack(
    *,
    success: bool = True,
    data_api_url: str = "",
) -> ButlerMessage:
    """构建 takeover 响应。

    Args:
        success: 是否成功。
        data_api_url: 管家端数据同步 API 的地址，供助手端读写运行时数据。
    """
    payload: dict[str, Any] = {}
    if data_api_url:
        payload["data_api_url"] = data_api_url
    return ButlerMessage(
        type=MessageType.TAKEOVER_ACK if success else MessageType.TAKEOVER_NACK,
        payload=payload,
    )


def make_error(reason: str) -> ButlerMessage:
    """构建错误消息。"""
    return ButlerMessage(
        type=MessageType.ERROR,
        payload={"reason": reason},
    )
