"""OneBot v11 协议层 —— 纯协议转换工具，不依赖任何具体平台实现。

提供：
    - _QQ_FACE_NAMES / _face_to_text: QQ 表情映射
    - extract_text_from_segments / extract_image_urls: OneBot message 数组解析
    - extract_sender_names / sanitize_image_name / extract_image_name: 辅助函数
    - build_image_label: 图片标签生成
"""
from sirius_pulse.platforms.onebot_v11.protocol import (
    _QQ_FACE_NAMES,
    _face_to_text,
    extract_text_from_segments,
    extract_image_urls,
    extract_sender_names,
    sanitize_image_name,
    extract_image_name,
    dedupe_image_name,
    build_image_label,
)

__all__ = [
    "_QQ_FACE_NAMES",
    "_face_to_text",
    "extract_text_from_segments",
    "extract_image_urls",
    "extract_sender_names",
    "sanitize_image_name",
    "extract_image_name",
    "dedupe_image_name",
    "build_image_label",
]
