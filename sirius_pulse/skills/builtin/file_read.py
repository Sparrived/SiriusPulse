"""Built-in skill for reading files within data/personaworkspace."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sirius_pulse.config.config_builder import ConfigBuilder

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_ALLOWED_READ_DIR = (_PROJECT_ROOT / "data" / "personaworkspace").resolve()

_config = ConfigBuilder()
_config.group("文件读取").add(
    "path",
    type="str",
    description="文件名，可含子目录，例如 notes.md、docs/readme.txt",
    required=True,
)

SKILL_META = {
    "name": "file_read",
    "description": (
        "读取 data/personaworkspace 目录下的文本文件内容。"
        "只需提供文件名（可含子目录，如 notes.md 或 docs/readme.txt），"
        "skill 会自动从 data/personaworkspace 下读取。"
        "支持 UTF-8 编码的文本文件和图片，自动拒绝二进制文件。"
    ),
    "version": "1.0.0",
    "tags": ["file", "io"],
    "developer_only": False,
    "dependencies": [],
    "parameters": _config.build(),
}

_MAX_SIZE_BYTES = 1 * 1024 * 1024  # 1 MB


def run(
    path: str = "",
    data_store: Any = None,
    **kwargs: Any,
) -> dict[str, Any]:
    if not path or not path.strip():
        return {
            "success": False,
            "error": "文件名不能为空",
            "summary": "文件读取失败：未提供文件名",
        }

    name = path.strip()
    normalized = name.replace("\\", "/")
    if ".." in normalized or normalized.startswith("/"):
        return {
            "success": False,
            "error": f"文件名包含非法字符: {name}",
            "summary": "文件读取失败：文件名被拒绝",
        }

    target = (_ALLOWED_READ_DIR / name).resolve()
    try:
        target.relative_to(_ALLOWED_READ_DIR)
    except ValueError:
        return {
            "success": False,
            "error": f"文件名超出允许范围: {name}",
            "summary": "文件读取失败：文件名被拒绝",
        }

    if not target.exists():
        return {
            "success": False,
            "error": f"文件不存在: {target}",
            "summary": "文件读取失败：文件不存在",
        }

    if target.is_dir():
        try:
            entries = []
            for entry in sorted(target.iterdir()):
                entry_str = f"{entry.name}/" if entry.is_dir() else entry.name
                entries.append(entry_str)
            return {
                "success": True,
                "summary": f"'{name}' 是一个目录，共 {len(entries)} 项",
                "text_blocks": ["\n".join(entries)],
                "internal_metadata": {
                    "path": str(target),
                    "is_directory": True,
                    "entry_count": len(entries),
                },
            }
        except OSError as exc:
            return {
                "success": False,
                "error": f"无法列出目录: {exc}",
                "summary": "文件读取失败：目录访问错误",
            }

    try:
        size = target.stat().st_size
    except OSError as exc:
        return {
            "success": False,
            "error": f"无法获取文件大小: {exc}",
            "summary": "文件读取失败：文件访问错误",
        }

    if size > _MAX_SIZE_BYTES:
        return {
            "success": False,
            "error": (
                f"文件过大 ({size / 1024 / 1024:.2f} MB)，"
                f"超过限制 {_MAX_SIZE_BYTES / 1024 / 1024:.0f} MB"
            ),
            "summary": "文件读取失败：文件过大",
        }

    mime_type = _guess_image_mime(target.name)
    if mime_type:
        return {
            "success": True,
            "summary": f"已读取图片 '{name}'（{size} 字节）",
            "text_blocks": [f"【图片】{name} — 已通过多模态通道发送给模型分析"],
            "multimodal_blocks": [
                {
                    "type": "image",
                    "label": "local_image",
                    "value": str(target),
                    "mime_type": mime_type,
                }
            ],
            "internal_metadata": {
                "path": str(target),
                "size_bytes": size,
                "mime_type": mime_type,
            },
        }

    try:
        raw = target.read_bytes()
        if b"\x00" in raw:
            return {
                "success": False,
                "error": "检测到二进制文件，拒绝读取",
                "summary": "文件读取失败：二进制文件",
            }
        content = raw.decode("utf-8", errors="replace")
    except OSError as exc:
        return {
            "success": False,
            "error": f"读取文件失败: {exc}",
            "summary": "文件读取失败：IO 错误",
        }

    return {
        "success": True,
        "summary": f"已读取 '{name}'（{size} 字节，约 {content.count(chr(10)) + 1} 行）",
        "text_blocks": [content],
        "internal_metadata": {
            "path": str(target),
            "size_bytes": size,
            "line_count": content.count("\n") + 1,
        },
    }


def _guess_image_mime(filename: str) -> str | None:
    """Return MIME type for known image extensions, or None."""
    ext = Path(filename).suffix.lower()
    mapping = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".bmp": "image/bmp",
        ".webp": "image/webp",
        ".ico": "image/x-icon",
        ".tiff": "image/tiff",
        ".svg": "image/svg+xml",
    }
    return mapping.get(ext)
