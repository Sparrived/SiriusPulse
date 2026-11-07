"""原图入库的安全检查与不可变存储。

本模块只做三件事，全部围绕 PRD ASSET-01／NFR-02：

1. 按**字节**判定图片格式，并用 Pillow 真正解码验证（不信任后缀与模型声明）；
2. 校验单图 20MB、像素上限、非空与真实尺寸，返回 sha256；
3. 把字节原子写入受控根目录（先临时文件再 ``os.replace``），相对路径入库。

远程 URL 不在本模块实现导入：PRD 要求限制协议、地址范围、重定向与流式大小，
但当前没有可靠的 DNS 绑定实现来抵御 SSRF 重绑定，因此任意 URL 导入被**明确禁用**；
受控入库路径是「本回合聊天附件」或「已授权资产引用」（见 ``VisualService._load_source``）。
"""

from __future__ import annotations

import hashlib
import io
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from sirius_pulse.visual.errors import VisualError

MAX_IMAGE_BYTES: int = 20 * 1024 * 1024
MAX_IMAGE_PIXELS: int = 60_000_000

_MAGIC: tuple[tuple[bytes, str, str], ...] = (
    (bytes.fromhex("89504e470d0a1a0a"), "image/png", "png"),
    (bytes.fromhex("ffd8ff"), "image/jpeg", "jpg"),
    (b"GIF87a", "image/gif", "gif"),
    (b"GIF89a", "image/gif", "gif"),
)
_EXT_BY_MIME = {
    "image/png": "png",
    "image/jpeg": "jpg",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/bmp": "bmp",
}
_SAFE_SEGMENT = re.compile(r"[^A-Za-z0-9._-]+")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sniff_mime(data: bytes) -> str:
    """按魔数返回媒体类型；认不出返回空字符串。"""
    for prefix, mime, _ext in _MAGIC:
        if data.startswith(prefix):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data.startswith(b"BM"):
        return "image/bmp"
    return ""


def inspect_image(data: bytes, *, max_bytes: int = MAX_IMAGE_BYTES) -> dict[str, Any]:
    """验证字节是真实可解码的图片，返回尺寸、格式、哈希。

    Raises:
        VisualError: ``asset_invalid`` —— 空/超大/非图片/解码失败/像素超限。
    """
    if not data:
        raise VisualError("asset_invalid", "图片内容为空")
    if len(data) > max_bytes:
        raise VisualError(
            "asset_invalid",
            f"图片过大（{len(data)} 字节），单张最多 {max_bytes // 1024 // 1024}MB",
        )
    mime = sniff_mime(data)
    if not mime:
        raise VisualError("asset_invalid", "文件内容不是可识别的图片格式")
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - 项目声明依赖 Pillow
        raise VisualError("asset_invalid", "缺少 Pillow，无法验证图片") from exc

    try:
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
            if width <= 0 or height <= 0:
                raise VisualError("asset_invalid", "图片尺寸无效")
            if width * height > MAX_IMAGE_PIXELS:
                raise VisualError(
                    "asset_invalid",
                    f"图片像素过大（{width}x{height}），最多 {MAX_IMAGE_PIXELS} 像素",
                )
            image.load()
    except VisualError:
        raise
    except Exception as exc:  # noqa: BLE001 - Pillow 异常类型随版本变化
        raise VisualError("asset_invalid", f"图片无法解码：{exc}") from exc
    return {
        "mime": mime,
        "ext": _EXT_BY_MIME.get(mime, "img"),
        "width": width,
        "height": height,
        "bytes": len(data),
        "sha256": sha256_bytes(data),
    }


def optimize_image_bytes(
    data: bytes,
    *,
    max_side: int = 1280,
    quality: int = 88,
    min_bytes: int = 600_000,
) -> bytes:
    """生成给模型/上游传输用的轻量副本。

    超过 ``max_side`` 或 ``min_bytes`` 的图会等比缩小；有 alpha 的存 PNG，
    其余存 JPEG。压缩后反而更大或解码失败时原样返回，绝不破坏调用方数据。
    """
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - 项目声明依赖 Pillow
        return data
    try:
        with Image.open(io.BytesIO(data)) as image:
            width, height = image.size
            if max(width, height) <= max_side and len(data) <= min_bytes:
                return data
            image.load()
            if max(width, height) > max_side:
                scale = max_side / float(max(width, height))
                size = (max(1, int(width * scale)), max(1, int(height * scale)))
                image = image.resize(size, Image.LANCZOS)
            buffer = io.BytesIO()
            has_alpha = image.mode in {"RGBA", "LA"} or (
                image.mode == "P" and "transparency" in image.info
            )
            if has_alpha:
                image.save(buffer, format="PNG", optimize=True)
            else:
                image.convert("RGB").save(buffer, format="JPEG", quality=quality, optimize=True)
            optimized = buffer.getvalue()
            return optimized if 0 < len(optimized) < len(data) else data
    except Exception:  # noqa: BLE001 - 优化失败不影响原图传输
        return data


def safe_segment(value: str, fallback: str = "x") -> str:
    cleaned = _SAFE_SEGMENT.sub("_", str(value or "").strip())[:64].strip("._-")
    return cleaned or fallback


def is_within(root: Path, candidate: Path) -> bool:
    """判断 candidate 是否真的落在 root 内（解析软链接与重解析点）。"""
    try:
        root_resolved = root.resolve(strict=False)
        candidate_resolved = candidate.resolve(strict=False)
        return os.path.commonpath([str(root_resolved), str(candidate_resolved)]) == str(
            root_resolved
        )
    except (OSError, ValueError):
        return False


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """先写同目录临时文件再原子替换，避免半截文件被当成可用资产。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temp_name = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".part")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def relative_asset_path(person_id: str, sha256: str, ext: str, *, kind: str = "originals") -> str:
    """受控根内的相对路径：``<person>/originals/<sha 前两位>/<sha>.<ext>``。"""
    person_segment = safe_segment(person_id, "person")
    return f"{person_segment}/{kind}/{sha256[:2]}/{sha256}.{safe_segment(ext, 'img')}"
