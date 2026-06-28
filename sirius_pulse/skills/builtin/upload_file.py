"""Built-in NapCat-specific skill for uploading files to the current chat."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sirius_pulse.config.config_builder import ConfigBuilder

_config = ConfigBuilder()
_config.group("文件上传").add(
    "file_path",
    type="str",
    description="本地文件绝对路径",
    required=True,
)
_config.group("文件上传").add(
    "file_name",
    type="str",
    description="在聊天中显示的文件名（不传则使用原文件名）",
)

SKILL_META = {
    "name": "upload_file",
    "description": ("上传本地文件到当前对话"),
    "version": "1.0.0",
    "tags": ["napcat", "file", "messaging"],
    "adapter_types": ["napcat"],
    "dependencies": [],
    "parameters": _config.build(),
}


async def run(
    bridge: Any,
    chat_context: dict[str, Any] | None = None,
    file_path: str = "",
    file_name: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Upload a file to the current chat via NapCat.

    Args:
        bridge: The NapCatAdapter instance injected by SkillExecutor.
        chat_context: Current chat context injected by SkillExecutor.
        file_path: Absolute path to a local file.
        file_name: Optional display name for the uploaded file.
    """
    if not bridge:
        return {
            "success": False,
            "error": "bridge 未就绪，无法上传文件",
            "summary": "上传失败：平台桥接未初始化",
        }

    adapter = getattr(bridge, "adapter", None) or bridge
    if adapter is None:
        return {
            "success": False,
            "error": "adapter 未就绪",
            "summary": "上传失败：NapCat 适配器未连接",
        }

    chat_context = chat_context or {}
    target_type = chat_context.get("chat_type", "")
    target_id = chat_context.get("chat_id", "")
    if not target_type or not target_id:
        return {
            "success": False,
            "error": "当前对话上下文缺失，无法确定发送目标",
            "summary": "上传失败：缺少对话上下文",
        }

    file_path = (file_path or "").strip()
    if not file_path:
        return {
            "success": False,
            "error": "file_path 不能为空",
            "summary": "上传失败：缺少文件路径",
        }

    p = Path(file_path)
    if not p.exists():
        return {
            "success": False,
            "error": f"文件不存在: {file_path}",
            "summary": "上传失败：文件不存在",
        }

    resolved_path = str(p.resolve())
    display_name = (file_name or "").strip() or p.name

    try:
        if target_type == "group":
            result = await adapter.upload_group_file(target_id, resolved_path, display_name)
        else:
            result = await adapter.upload_private_file(target_id, resolved_path, display_name)

        data = result.get("data", {}) if isinstance(result, dict) else {}
        return {
            "success": True,
            "summary": f"文件「{display_name}」已上传到 {target_type} {target_id}",
            "text_blocks": [f"文件上传成功: {resolved_path}"],
            "internal_metadata": {
                "target_type": target_type,
                "target_id": target_id,
                "file_name": display_name,
                "message_id": data.get("message_id") if isinstance(data, dict) else None,
            },
        }
    except Exception as exc:
        return {
            "success": False,
            "error": str(exc),
            "summary": f"文件上传失败: {exc}",
        }
