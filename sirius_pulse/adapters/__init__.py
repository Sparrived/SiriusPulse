"""平台适配器框架。

提供：
    - MessageSegment / MessageGroup：跨平台统一消息类型
    - BaseAdapter：适配器抽象基类
"""
from sirius_pulse.adapters.base import BaseAdapter
from sirius_pulse.adapters.models import (
    AtSegment,
    FileSegment,
    ImageSegment,
    MessageGroup,
    MessageSegment,
    ParsedEvent,
    ReplySegment,
    TextSegment,
    VoiceSegment,
    at,
    file,
    image,
    reply,
    text,
    voice,
)

__all__ = [
    "MessageGroup",
    "MessageSegment",
    "TextSegment",
    "AtSegment",
    "ImageSegment",
    "VoiceSegment",
    "FileSegment",
    "ReplySegment",
    "ParsedEvent",
    "text",
    "at",
    "image",
    "voice",
    "file",
    "reply",
    "BaseAdapter",
]
