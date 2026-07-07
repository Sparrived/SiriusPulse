"""Built-in skill for data/personaworkspace file operations."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sirius_pulse.config.config_builder import ConfigBuilder

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_WORKSPACE_DIR = (_PROJECT_ROOT / "data" / "personaworkspace").resolve()
_MAX_RESULTS = 200
_MAX_READ_SIZE_BYTES = 1 * 1024 * 1024
_MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024
_MAX_WRITE_SIZE_BYTES = 1 * 1024 * 1024
_SKIP_EXTENSIONS = {".exe", ".dll", ".so", ".dylib", ".bin", ".obj", ".o", ".pyc"}

_config = ConfigBuilder()
_config.group("工作区文件").add(
    "action",
    type="str",
    description="操作类型：list 列出/搜索；read 读取；write 写入；send 发送到当前聊天。",
    required=True,
    choices=["list", "read", "write", "send"],
)
_config.group("工作区文件").add(
    "path",
    type="str",
    description="data/personaworkspace 下的相对路径；list 默认 .，read/write/send 时填写文件路径。",
)
_config.group("工作区文件").add(
    "content",
    type="str",
    description="action=write 时写入的文本内容。",
)
_config.group("工作区文件").add(
    "mode",
    type="str",
    description="action=write 时的写入模式：write 覆盖；append 追加。",
    default="write",
    choices=["write", "append"],
)
_config.group("工作区文件").add(
    "recursive",
    type="bool",
    description="action=list 时是否递归列出子目录。",
    default=False,
)
_config.group("工作区文件").add(
    "pattern",
    type="str",
    description="action=list 时的 glob 过滤模式，例如 *.py、*.md。",
)
_config.group("工作区文件").add(
    "display_name",
    type="str",
    description="action=send 时在聊天中显示的文件名；不传则使用原文件名。",
)

SKILL_META = {
    "name": "workspace_file",
    "description": (
        "群聊里需要查看、整理、保存或发送工作区文件时使用；适合列出文件、读笔记、写长文/报告/计划，"
        "或把 data/personaworkspace 下已整理好的文件发给大家。"
    ),
    "version": "1.0.0",
    "tags": ["file", "io", "messaging", "napcat"],
    "dependencies": [],
    "parameters": _config.build(),
}


async def run(
    action: str,
    path: str = "",
    content: str = "",
    mode: str = "write",
    recursive: bool = False,
    pattern: str = "",
    display_name: str = "",
    bridge: Any = None,
    chat_context: dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    action_key = str(action or "").strip().lower()
    if action_key == "list":
        return _list_files(path or ".", recursive, pattern)
    if action_key == "read":
        return _read_file(path)
    if action_key == "write":
        return _write_file(path, content, mode)
    if action_key == "send":
        return await _send_file(path, display_name, bridge, chat_context)
    return {"success": False, "error": "action 必须是 list/read/write/send"}


def _resolve_workspace_path(relative_path: str) -> Path | None:
    requested = str(relative_path or ".").strip() or "."
    try:
        resolved = (_WORKSPACE_DIR / requested).resolve()
        resolved.relative_to(_WORKSPACE_DIR)
        return resolved
    except ValueError:
        return None


def _list_files(path: str = ".", recursive: bool = False, pattern: str = "") -> dict[str, Any]:
    base = _resolve_workspace_path(path)
    if base is None:
        return {"success": False, "error": f"路径超出允许范围: {path}"}
    if not base.exists():
        return {"success": False, "error": f"路径不存在: {path}"}
    if base.is_file():
        return {
            "success": True,
            "summary": f"找到文件: {path}",
            "text_blocks": [_format_entry(base)],
        }

    glob_pattern = pattern.strip() if pattern else "*"
    iterator = base.rglob(glob_pattern) if recursive else base.glob(glob_pattern)
    entries = sorted(iterator, key=lambda p: (p.is_file(), str(p).lower()))
    lines: list[str] = []
    total = 0
    for entry in entries:
        if entry.name.startswith("."):
            continue
        total += 1
        if len(lines) < _MAX_RESULTS:
            lines.append(_format_entry(entry))
    if not lines:
        return {
            "success": True,
            "summary": "没有找到匹配的文件",
            "text_blocks": ["没有找到匹配的文件"],
        }
    if total > len(lines):
        lines.append(f"...还有 {total - len(lines)} 项未显示")
    return {
        "success": True,
        "summary": f"列出 {len(lines)} 项，路径 {path}",
        "text_blocks": ["\n".join(lines)],
        "internal_metadata": {"path": path, "total": total},
    }


def _read_file(path: str) -> dict[str, Any]:
    resolved = _resolve_workspace_path(path)
    if resolved is None:
        return {"success": False, "error": f"路径超出允许范围: {path}"}
    if not resolved.exists():
        return {"success": False, "error": f"文件不存在: {path}"}
    if resolved.is_dir():
        return {"success": False, "error": f"路径是目录，不是文件: {path}"}
    if resolved.suffix.lower() in _SKIP_EXTENSIONS:
        return {"success": False, "error": f"拒绝读取二进制/可执行文件: {path}"}
    if resolved.stat().st_size > _MAX_READ_SIZE_BYTES:
        return {"success": False, "error": "文件过大，超过 1MB 限制"}
    try:
        text = resolved.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return {"success": False, "error": "文件不是 UTF-8 文本"}
    return {
        "success": True,
        "summary": f"已读取 {path}",
        "text_blocks": [text],
        "internal_metadata": {"path": str(resolved), "size": resolved.stat().st_size},
    }


def _write_file(path: str, content: str, mode: str = "write") -> dict[str, Any]:
    if not str(path or "").strip():
        return {"success": False, "error": "path 不能为空"}
    resolved = _resolve_workspace_path(path)
    if resolved is None:
        return {"success": False, "error": f"路径超出允许范围: {path}"}
    if resolved.exists() and resolved.is_dir():
        return {"success": False, "error": f"路径是目录，不是文件: {path}"}
    if resolved.exists() and resolved.stat().st_size > _MAX_FILE_SIZE_BYTES:
        return {"success": False, "error": "目标文件过大，超过 10MB 限制"}
    encoded = str(content or "").encode("utf-8")
    if len(encoded) > _MAX_WRITE_SIZE_BYTES:
        return {"success": False, "error": "写入内容过大，超过 1MB 限制"}
    write_mode = str(mode or "write").strip().lower()
    if write_mode not in {"write", "append"}:
        return {"success": False, "error": "mode 必须是 write 或 append"}
    resolved.parent.mkdir(parents=True, exist_ok=True)
    if write_mode == "append":
        with resolved.open("a", encoding="utf-8") as file:
            file.write(str(content or ""))
    else:
        resolved.write_text(str(content or ""), encoding="utf-8")
    return {
        "success": True,
        "summary": f"已{'追加' if write_mode == 'append' else '写入'} {path}",
        "text_blocks": [f"文件已保存: {path}"],
        "internal_metadata": {"path": str(resolved), "bytes": len(encoded), "mode": write_mode},
    }


async def _send_file(
    path: str,
    display_name: str = "",
    bridge: Any = None,
    chat_context: dict[str, Any] | None = None,
) -> dict[str, Any]:
    adapter = getattr(bridge, "adapter", None) or bridge
    if adapter is None:
        return {
            "success": False,
            "error": "adapter 未就绪",
            "summary": "发送失败：NapCat 适配器未连接",
        }
    chat_context = chat_context or {}
    target_type = chat_context.get("chat_type", "")
    target_id = chat_context.get("chat_id", "")
    if not target_type or not target_id:
        return {"success": False, "error": "当前对话上下文缺失，无法确定发送目标"}
    resolved = _resolve_workspace_path(path)
    if resolved is None:
        return {"success": False, "error": f"路径超出允许范围: {path}"}
    if not resolved.exists() or resolved.is_dir():
        return {"success": False, "error": f"文件不存在或不是文件: {path}"}
    display = (display_name or "").strip() or resolved.name
    try:
        if target_type == "group":
            raw = await adapter.upload_group_file(target_id, str(resolved), display)
        else:
            raw = await adapter.upload_private_file(target_id, str(resolved), display)
        data = raw.get("data", {}) if isinstance(raw, dict) else {}
        return {
            "success": True,
            "summary": f"文件「{display}」已发送到 {target_type} {target_id}",
            "text_blocks": [f"文件发送成功: {resolved.name}"],
            "internal_metadata": {
                "target_type": target_type,
                "target_id": target_id,
                "file_name": display,
                "message_id": data.get("message_id") if isinstance(data, dict) else None,
            },
        }
    except Exception as exc:
        return {"success": False, "error": f"发送文件失败: {exc}"}


def _format_entry(path: Path) -> str:
    rel = path.relative_to(_WORKSPACE_DIR).as_posix()
    if path.is_dir():
        return f"[目录] {rel}/"
    return f"[文件] {rel} ({path.stat().st_size} bytes)"
