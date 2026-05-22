"""Built-in skill for writing files within the workspace."""

from __future__ import annotations

from pathlib import Path
from typing import Any

SKILL_META = {
    "name": "file_write",
    "description": (
        "在 data/personaworkspace 目录下创建或修改文本文件。"
        "只需提供文件名（可含子目录，如 notes.md 或 docs/report.txt），"
        "skill 会自动写入到 data/personaworkspace 下。"
        "支持覆盖写入或追加到现有文件末尾。"
    ),
    "version": "1.0.0",
    "tags": ["file", "io"],
    "developer_only": False,
    "dependencies": [],
    "parameters": {
        "path": {
            "type": "str",
            "description": "文件名，可含子目录，例如 notes.md、docs/report.txt",
            "required": True,
        },
        "content": {
            "type": "str",
            "description": "要写入的文本内容",
            "required": True,
        },
        "mode": {
            "type": "str",
            "description": "写入模式：'write' 覆盖写入（默认），'append' 追加到末尾",
            "required": False,
            "default": "write",
        },
    },
}

_MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB
_MAX_WRITE_SIZE_BYTES = 1 * 1024 * 1024  # 1 MB

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_ALLOWED_WRITE_DIR = (_PROJECT_ROOT / "data" / "personaworkspace").resolve()


def run(
    path: str = "",
    content: str = "",
    mode: str = "write",
    data_store: Any = None,
    **kwargs: Any,
) -> dict[str, Any]:
    name = (path or "").strip()
    if not name:
        return {
            "success": False,
            "error": "文件名不能为空",
            "summary": "文件写入失败：未提供文件名",
        }

    # Basic traversal guard: reject '..' and absolute-looking paths
    normalized = name.replace("\\", "/")
    if ".." in normalized or normalized.startswith("/"):
        return {
            "success": False,
            "error": f"文件名包含非法字符: {name}",
            "summary": "文件写入失败：文件名被拒绝",
        }

    target = (_ALLOWED_WRITE_DIR / name).resolve()
    # Final safety: ensure resolved path is still inside allowed dir
    try:
        target.relative_to(_ALLOWED_WRITE_DIR)
    except ValueError:
        return {
            "success": False,
            "error": f"文件名超出允许范围: {name}",
            "summary": "文件写入失败：文件名被拒绝",
        }

    if target.exists() and target.is_dir():
        return {
            "success": False,
            "error": f"目标路径是一个目录，无法写入: {target}",
            "summary": "文件写入失败：目标是目录",
        }

    # Guard against overwriting large existing files
    if target.exists() and mode.lower() == "write":
        try:
            existing_size = target.stat().st_size
        except OSError as exc:
            return {
                "success": False,
                "error": f"无法检查现有文件: {exc}",
                "summary": "文件写入失败：无法访问目标文件",
            }
        if existing_size > _MAX_FILE_SIZE_BYTES:
            return {
                "success": False,
                "error": (
                    f"现有文件过大 ({existing_size / 1024 / 1024:.2f} MB)，"
                    f"超过安全覆盖限制 {_MAX_FILE_SIZE_BYTES / 1024 / 1024:.0f} MB，拒绝覆盖"
                ),
                "summary": "文件写入失败：现有文件过大",
            }

    content_bytes = content.encode("utf-8")
    if len(content_bytes) > _MAX_WRITE_SIZE_BYTES:
        return {
            "success": False,
            "error": (
                f"写入内容过大 ({len(content_bytes) / 1024 / 1024:.2f} MB)，"
                f"超过单次限制 {_MAX_WRITE_SIZE_BYTES / 1024 / 1024:.0f} MB"
            ),
            "summary": "文件写入失败：内容过大",
        }

    if target.exists():
        try:
            header = target.read_bytes()[:8192]
            if b"\x00" in header:
                return {
                    "success": False,
                    "error": "目标文件是二进制文件，拒绝覆盖",
                    "summary": "文件写入失败：目标是二进制文件",
                }
        except OSError as exc:
            return {
                "success": False,
                "error": f"无法读取目标文件头: {exc}",
                "summary": "文件写入失败：文件访问错误",
            }

    try:
        _ALLOWED_WRITE_DIR.mkdir(parents=True, exist_ok=True)
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return {
            "success": False,
            "error": f"无法创建父目录: {exc}",
            "summary": "文件写入失败：目录创建错误",
        }

    write_mode = "a" if mode.lower() == "append" else "w"
    try:
        with target.open(write_mode, encoding="utf-8") as f:
            f.write(content)
    except OSError as exc:
        return {
            "success": False,
            "error": f"写入文件失败: {exc}",
            "summary": "文件写入失败：IO 错误",
        }

    try:
        final_size = target.stat().st_size
    except OSError:
        final_size = -1

    action = "追加" if write_mode == "a" else "写入"
    return {
        "success": True,
        "summary": f"已{action} '{name}'（{len(content_bytes)} 字节）",
        "text_blocks": [f"{action}完成：{name}\n最终大小：{final_size} 字节"],
        "internal_metadata": {
            "path": str(target),
            "mode": write_mode,
            "bytes_written": len(content_bytes),
            "final_size_bytes": final_size,
        },
    }
