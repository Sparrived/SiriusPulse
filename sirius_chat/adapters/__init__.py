"""平台适配器框架。

提供：
    - MessageSegment / MessageGroup：跨平台统一消息类型
    - BaseAdapter：适配器抽象基类
"""
from sirius_chat.adapters.models import (
    MessageGroup,
    MessageSegment,
    TextSegment,
    AtSegment,
    ImageSegment,
    VoiceSegment,
    FileSegment,
    ReplySegment,
    text,
    at,
    image,
    voice,
    file,
    reply,
)
from sirius_chat.adapters.base import BaseAdapter

__all__ = [
    "MessageGroup",
    "MessageSegment",
    "TextSegment",
    "AtSegment",
    "ImageSegment",
    "VoiceSegment",
    "FileSegment",
    "ReplySegment",
    "text",
    "at",
    "image",
    "voice",
    "file",
    "reply",
    "BaseAdapter",
]
