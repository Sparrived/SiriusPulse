"""平台无关的消息片段模型。

定义跨平台统一的消息组件类型。每个 Adapter 实现负责将
这些标准片段转换为平台特定的格式（如 OneBot array、Discord embed 等）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass(slots=True)
class TextSegment:
    """纯文本片段。"""

    text: str


@dataclass(slots=True)
class AtSegment:
    """@提及片段。"""

    user_id: str


@dataclass(slots=True)
class ImageSegment:
    """图片片段。

    file_path: 本地文件路径
    url: 远程 URL（可选）
    sub_type: 子类型（如 QQ 表情贴纸为 "1"）
    """

    file_path: str
    url: str = ""
    sub_type: str = ""


@dataclass(slots=True)
class VoiceSegment:
    """语音片段。"""

    file_path: str


@dataclass(slots=True)
class FileSegment:
    """文件片段。"""

    file_path: str
    name: str = ""


@dataclass(slots=True)
class ReplySegment:
    """回复引用片段。"""

    message_id: str


# ── 联合类型 ──

MessageSegment = TextSegment | AtSegment | ImageSegment | VoiceSegment | FileSegment | ReplySegment


@dataclass
class MessageGroup:
    """一组有序的消息片段。

    使用示例:
        msg = MessageGroup([
            AtSegment("123456"),
            TextSegment(" 你好，这是你要的图片："),
            ImageSegment("/tmp/photo.jpg"),
        ])
        await adapter.send_group_message("789", msg)
    """

    segments: list[MessageSegment] = field(default_factory=list)

    def __post_init__(self) -> None:
        # 单个 str → TextSegment
        if isinstance(self.segments, str):
            self.segments = [TextSegment(self.segments)]

    @classmethod
    def from_str(cls, text: str) -> "MessageGroup":
        """从纯文本创建消息组。"""
        return cls([TextSegment(text)])

    def __len__(self) -> int:
        return len(self.segments)

    def __iter__(self):
        return iter(self.segments)

    def __getitem__(self, idx: int) -> MessageSegment:
        return self.segments[idx]

    def __add__(self, other: "MessageGroup") -> "MessageGroup":
        return MessageGroup(self.segments + other.segments)


def text(text: str) -> TextSegment:
    """快捷构造文本片段。"""
    return TextSegment(text)


def at(user_id: str) -> AtSegment:
    """快捷构造 @提及片段。"""
    return AtSegment(user_id)


def image(file_path: str, url: str = "", sub_type: str = "") -> ImageSegment:
    """快捷构造图片片段。"""
    return ImageSegment(file_path=file_path, url=url, sub_type=sub_type)


def voice(file_path: str) -> VoiceSegment:
    """快捷构造语音片段。"""
    return VoiceSegment(file_path)


def file(file_path: str, name: str = "") -> FileSegment:
    """快捷构造文件片段。"""
    return FileSegment(file_path=file_path, name=name)


def reply(message_id: str) -> ReplySegment:
    """快捷构造回复引用片段。"""
    return ReplySegment(message_id)
