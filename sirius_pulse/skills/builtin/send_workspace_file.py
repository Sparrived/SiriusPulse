"""Built-in NapCat-specific skill for sending files from data/personaworkspace."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sirius_pulse.config.config_builder import ConfigBuilder

_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent.parent
_WORKSPACE_DIR = (_PROJECT_ROOT / "data" / "personaworkspace").resolve()

_config = ConfigBuilder()
_config.group("文件发送").add(
    "file_name",
    type="str",
    description=(
        "data/personaworkspace 目录下的文件路径（可含子目录），"
        "例如 notes.md、images/photo.png"
    ),
    required=True,
)
_config.group("文件发送").add(
    "display_name",
    type="str",
    description="在聊天中显示的文件名（不传则使用原文件名）",
)

SKILL_META = {
    "name": "send_workspace_file",
    "description": (
        "从 data/personaworkspace 目录发送文件到当前对话（群聊或私聊）。"
    ),
    "version": "1.0.0",
    "tags": ["napcat", "file", "messaging"],
    "adapter_types": ["napcat"],
    "dependencies": [],
    "parameters": _config.build(),
}


async def run(
    bridge: Any,
    chat_context: dict[str, Any] | None = None,
    file_name: str = "",
    display_name: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Send a file from data/personaworkspace to the current chat via NapCat.

    Args:
        bridge: The NapCatAdapter instance injected by SkillExecutor.
        chat_context: Current chat context injected by SkillExecutor.
        file_name: File path relative to data/personaworkspace.
        display_name: Optional display name for the uploaded file.
    """
    if not bridge:
        return {
            "success": False,
            "error": "bridge 未就绪，无法发送文件",
            "summary": "发送失败：平台桥接未初始化",
        }

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
        return {
            "success": False,
            "error": "当前对话上下文缺失，无法确定发送目标",
            "summary": "发送失败：缺少对话上下文",
        }

    file_name = (file_name or "").strip()
    if not file_name:
        return {
            "success": False,
            "error": "file_name 不能为空",
            "summary": "发送失败：缺少文件名",
        }

    # Resolve file path within the workspace directory
    file_path = _WORKSPACE_DIR / file_name
    try:
        resolved = file_path.resolve()
        resolved.relative_to(_WORKSPACE_DIR)
    except ValueError:
        return {
            "success": False,
            "error": f"文件路径超出允许范围: {file_name}",
            "summary": "发送失败：路径不在 data/personaworkspace 内",
        }

    if not resolved.exists():
        return {
            "success": False,
            "error": f"文件不存在: {resolved}",
            "summary": "发送失败：文件不存在",
        }

    if resolved.is_dir():
        return {
            "success": False,
            "error": f"路径是目录，不是文件: {resolved}",
            "summary": "发送失败：目标是目录",
        }

    display = (display_name or "").strip() or resolved.name

    try:
        if target_type == "group":
            result = await adapter.upload_group_file(target_id, str(resolved), display)
        else:
            result = await adapter.upload_private_file(target_id, str(resolved), display)

        data = result.get("data", {}) if isinstance(result, dict) else {}
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
        return {
            "success": False,
            "error": str(exc),
            "summary": f"文件发送失败: {exc}",
        }
