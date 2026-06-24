"""Built-in skill for listing files within data/personaworkspace."""

from __future__ import annotations

import fnmatch
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sirius_pulse.config.config_builder import ConfigBuilder

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_ALLOWED_LIST_DIR = (_PROJECT_ROOT / "data" / "personaworkspace").resolve()

_config = ConfigBuilder()
_config.group("文件列表").add(
    "path",
    type="str",
    description="相对路径，例如 .、docs/、images/。不传则列出根目录",
    default=".",
)
_config.group("文件列表").add(
    "recursive",
    type="bool",
    description="是否递归列出子目录内容",
    default=False,
)
_config.group("文件列表").add(
    "pattern",
    type="str",
    description="glob 过滤模式，例如 *.py、*.md。不传则不过滤",
    default="",
)

SKILL_META = {
    "name": "file_list",
    "description": (
        "列出或搜索 data/personaworkspace 目录下的文件和目录，支持按路径、递归深度和 glob 模式过滤。"
        "只需提供相对路径（如 . 或 docs/），不传则列出根目录。"
    ),
    "version": "1.0.0",
    "tags": ["file", "io"],
    "developer_only": False,
    "dependencies": [],
    "parameters": _config.build(),
}

_MAX_RESULTS = 200

_SKIP_EXTENSIONS = {
    ".exe",
    ".dll",
    ".so",
    ".dylib",
    ".bin",
    ".obj",
    ".o",
    ".class",
    ".pyc",
    ".pyo",
    ".mp4",
    ".avi",
    ".mov",
    ".mkv",
    ".flv",
    ".mp3",
    ".wav",
    ".ogg",
    ".flac",
    ".aac",
    ".wma",
    ".zip",
    ".rar",
    ".7z",
    ".tar",
    ".gz",
    ".bz2",
    ".xz",
    ".ttf",
    ".woff",
    ".woff2",
    ".eot",
    ".otf",
    ".db",
    ".sqlite",
    ".sqlite3",
    ".lock",
    ".pkl",
    ".pickle",
    ".coverage",
    ".swp",
    ".swo",
    ".tmp",
    ".temp",
    ".DS_Store",
}


def run(
    path: str = ".",
    recursive: bool = False,
    pattern: str = "",
    data_store: Any = None,
    **kwargs: Any,
) -> dict[str, Any]:
    raw_path = (path or "").strip() or "."
    normalized = raw_path.replace("\\", "/")
    if ".." in normalized or normalized.startswith("/"):
        return {
            "success": False,
            "error": f"路径包含非法字符: {raw_path}",
            "summary": "文件查询失败：路径被拒绝",
        }

    target = (_ALLOWED_LIST_DIR / raw_path).resolve()
    try:
        target.relative_to(_ALLOWED_LIST_DIR)
    except ValueError:
        return {
            "success": False,
            "error": f"路径超出允许范围: {raw_path}",
            "summary": "文件查询失败：路径被拒绝",
        }

    if target.is_file():
        info = _describe_entry(target, target.parent)
        return {
            "success": True,
            "summary": f"'{raw_path}' 是一个文件",
            "text_blocks": [_format_entries([info])],
            "internal_metadata": {
                "path": str(target),
                "count": 1,
                "truncated": False,
            },
        }

    if not target.exists():
        return {
            "success": False,
            "error": f"路径不存在: {target}",
            "summary": "文件查询失败：路径不存在",
        }

    entries: list[dict[str, Any]] = []
    truncated = False
    glob_pat = pattern.strip() if pattern else "*"

    try:
        if recursive:
            for root, dirs, files in os.walk(target):
                for name in files:
                    if glob_pat != "*" and not fnmatch.fnmatch(name, glob_pat):
                        continue
                    if any(name.lower().endswith(ext) for ext in _SKIP_EXTENSIONS):
                        continue
                    full = Path(root) / name
                    entries.append(_describe_entry(full, target))
                    if len(entries) >= _MAX_RESULTS:
                        truncated = True
                        break
                for name in dirs:
                    full = Path(root) / name
                    entries.append(_describe_entry(full, target))
                    if len(entries) >= _MAX_RESULTS:
                        truncated = True
                        break
                if truncated:
                    break
        else:
            for item in sorted(target.iterdir()):
                if glob_pat != "*" and not fnmatch.fnmatch(item.name, glob_pat):
                    continue
                if item.is_file() and any(
                    item.name.lower().endswith(ext) for ext in _SKIP_EXTENSIONS
                ):
                    continue
                entries.append(_describe_entry(item, target))
                if len(entries) >= _MAX_RESULTS:
                    truncated = True
                    break
    except OSError as exc:
        return {
            "success": False,
            "error": f"遍历目录失败: {exc}",
            "summary": "文件查询失败：目录遍历错误",
        }

    summary = f"在 '{raw_path}' 下找到 {len(entries)} 项"
    if recursive:
        summary += "（递归）"
    if pattern:
        summary += f"，模式 '{pattern}'"
    if truncated:
        summary += f"，结果已截断至前 {_MAX_RESULTS} 项"

    return {
        "success": True,
        "summary": summary,
        "text_blocks": [_format_entries(entries)],
        "internal_metadata": {
            "path": str(target),
            "count": len(entries),
            "truncated": truncated,
            "recursive": recursive,
            "pattern": pattern,
        },
    }


def _describe_entry(entry: Path, base_path: Path) -> dict[str, Any]:
    """Build a metadata dict for a single file or directory."""
    try:
        rel = entry.relative_to(base_path).as_posix()
    except ValueError:
        rel = entry.as_posix()
    info: dict[str, Any] = {
        "path": rel,
        "type": "directory" if entry.is_dir() else "file",
    }
    try:
        st = entry.stat()
        info["size_bytes"] = st.st_size
        info["modified"] = datetime.fromtimestamp(st.st_mtime, tz=timezone.utc).strftime(
            "%Y-%m-%d %H:%M"
        )
    except OSError:
        pass
    return info


def _format_entries(entries: list[dict[str, Any]]) -> str:
    """Format entry list as a plain-text table."""
    if not entries:
        return "（无结果）"
    lines: list[str] = []
    for e in entries:
        t = "[D]" if e.get("type") == "directory" else "[F]"
        size = e.get("size_bytes", "-")
        mtime = e.get("modified", "-")
        lines.append(f"{t} {e['path']:<50} {size:>12} {mtime:>16}")
    return "\n".join(lines)
