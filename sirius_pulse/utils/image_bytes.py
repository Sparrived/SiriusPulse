"""图片字节的体积控制工具。

多模态输入最终会以 base64 data URL 内联进请求体，体积会膨胀约 4/3。平台原图
（尤其手机截图与长图）常在 10MB 以上，直接内联会撑爆请求体或超出上游限制。
这里统一把过大的图片降采样成 JPEG，而不是放弃这张图——一旦放弃内联，多数
平台图片地址带短时效签名，上游模型自行下载必然失败，最后表现为「看不到图」。
"""

from __future__ import annotations

import io
import logging
from typing import Any

logger = logging.getLogger(__name__)

#: 单张内联图片的字节上限（压缩前判定阈值与压缩目标共用此默认值）。
MAX_INLINE_IMAGE_BYTES: int = 10 * 1024 * 1024

#: 长边上限。多数视觉编码器会把长边缩到 1568 像素以内，超过的部分纯属浪费。
MAX_INLINE_IMAGE_EDGE: int = 1568

#: 逐级下调的 JPEG 质量，先试高画质，超限再降。
_JPEG_QUALITY_LADDER: tuple[int, ...] = (85, 70, 55, 40)

#: 质量降到底仍超限时，按此比例继续缩小长边。
_SHRINK_FACTOR: float = 0.75

#: 缩小的下限：再小就会丢失可辨识的细节，不如接受超限。
_MIN_EDGE: int = 320


def _lanczos() -> Any:
    """返回 Lanczos 重采样常量，兼容 Pillow 的两种暴露方式。

    Pillow 9.0.x 只有 ``Image.LANCZOS``，10.0 起该别名被移除、只剩
    ``Image.Resampling.LANCZOS``；本项目声明支持 Pillow>=9，两者都要能用。
    """
    from PIL import Image

    resampling = getattr(Image, "Resampling", None)
    if resampling is not None:
        return resampling.LANCZOS
    return getattr(Image, "LANCZOS")


def downscale_image_bytes(
    data: bytes,
    *,
    max_bytes: int = MAX_INLINE_IMAGE_BYTES,
    max_edge: int = MAX_INLINE_IMAGE_EDGE,
) -> bytes | None:
    """把过大的图片降采样为 JPEG 字节。

    先按长边上限缩略，再沿质量阶梯下压；若仅降质量仍超过 ``max_bytes``，
    继续按 ``_SHRINK_FACTOR`` 缩小长边（不低于 ``_MIN_EDGE``）。目标是尽量让
    图片留在内联通道里——丢图意味着模型彻底看不到它。

    Args:
        data: 原始图片字节。
        max_bytes: 目标字节上限。
        max_edge: 长边像素上限。

    Returns:
        压缩后的 JPEG 字节；Pillow 不可用或解码失败时返回 ``None``，
        调用方应据此放弃内联而不是退回原始网络地址。
    """
    try:
        from PIL import Image
    except ImportError:
        logger.debug("未安装 Pillow，无法压缩过大图片")
        return None

    try:
        with Image.open(io.BytesIO(data)) as image:
            image.load()
            frame = image.convert("RGB")
    except Exception as exc:  # noqa: BLE001 - 解码失败不应影响聊天主流程
        logger.warning("图片压缩失败: %s", exc)
        return None

    edge = max_edge
    if max(frame.size) > edge:
        frame.thumbnail((edge, edge), _lanczos())

    smallest: bytes | None = None
    while True:
        for quality in _JPEG_QUALITY_LADDER:
            buffer = io.BytesIO()
            frame.save(buffer, format="JPEG", quality=quality, optimize=True)
            encoded = buffer.getvalue()
            if smallest is None or len(encoded) < len(smallest):
                smallest = encoded
            if len(encoded) <= max_bytes:
                return encoded

        next_edge = int(edge * _SHRINK_FACTOR)
        if next_edge < _MIN_EDGE or max(frame.size) <= _MIN_EDGE:
            # 已经缩到下限仍超限：返回最小的一份，仍好过丢图。
            return smallest
        edge = next_edge
        frame.thumbnail((edge, edge), _lanczos())
