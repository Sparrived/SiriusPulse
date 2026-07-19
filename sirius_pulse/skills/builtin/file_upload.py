"""Unified NapCat skill for sending images and uploading files."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from sirius_pulse.config.config_builder import ConfigBuilder
from sirius_pulse.skills.models import SkillInvocationContext

LOG = logging.getLogger(__name__)

_config = ConfigBuilder()
_config.group("图片与文件").add(
    "action",
    type="str",
    description=(
        "操作类型：image 发送图片；file 上传文件。你正在以当前人格参与群聊，"
        "当角色想分享截图、图片、资料或把一份文件交给对方时，直接使用这个工具完成互动。"
    ),
    required=True,
    choices=["image", "file"],
)
_config.group("图片与文件").add(
    "image_path", type="str", description="action=image 时的本地图片路径或网络 URL。"
)
_config.group("图片与文件").add(
    "file_path", type="str", description="action=file 时要上传的本地文件路径。"
)
_config.group("图片与文件").add(
    "file_name", type="str", description="action=file 时在聊天中显示的文件名。"
)

SKILL_META = {
    "name": "file_upload",
    "description": (
        "以当前人格参与聊天时用于发送图片或上传文件的互动工具：当图片、截图、资料或文件"
        "能让角色的表达更具体、更自然时主动调用，不要只在正文里描述一个本地路径。"
        "纯文字回复直接写在正文中，不要每轮强行调用。"
    ),
    "version": "1.0.0",
    "side_effect": "external_write",
    "tags": ["napcat", "qq", "file", "messaging"],
    "adapter_types": ["napcat"],
    "parameters": _config.build(),
}


async def run(
    action: str,
    image_path: str = "",
    file_path: str = "",
    file_name: str = "",
    bridge: Any = None,
    chat_context: dict[str, Any] | None = None,
    invocation_context: SkillInvocationContext | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    action_key = str(action or "").strip().lower()
    if action_key == "image":
        result = await _send_image(bridge, chat_context, image_path)
    elif action_key == "file":
        result = await _upload_file(bridge, chat_context, file_path, file_name)
    else:
        return {"success": False, "error": f"不支持的文件上传 action: {action}"}

    metadata = result.get("internal_metadata")
    result["internal_metadata"] = {
        **(metadata if isinstance(metadata, dict) else {}),
        "file_upload_action": action_key,
    }
    return result


async def _send_image(
    bridge: Any,
    chat_context: dict[str, Any] | None,
    image_path: str,
) -> dict[str, Any]:
    if not bridge:
        return {
            "success": False,
            "error": "bridge 未就绪，无法发送图片",
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

    image_path = (image_path or "").strip()
    if not image_path:
        return {
            "success": False,
            "error": "image_path 不能为空",
            "summary": "发送失败：缺少图片路径",
        }

    if "://" not in image_path and not image_path.startswith("file://"):
        path = Path(image_path)
        if path.exists():
            image_path = str(path.resolve())

    if image_path.startswith(("http://", "https://")):
        cache_fn = getattr(adapter, "cache_image", None)
        if cache_fn is not None:
            try:
                local_path = await cache_fn(image_path)
                if local_path and not local_path.startswith(("http://", "https://")):
                    image_path = local_path
                    LOG.info("远程图片已缓存到本地: %s", local_path)
            except Exception as exc:
                LOG.warning("远程图片缓存失败，直接使用原始 URL: %s | %s", exc, image_path[:80])

    message = [{"type": "image", "data": {"file": image_path}}]
    try:
        if target_type == "group":
            result = await adapter.send_group_msg(target_id, message)
        else:
            result = await adapter.send_private_msg(target_id, message)

        data = result.get("data", {}) if isinstance(result, dict) else {}
        return {
            "success": True,
            "summary": f"图片已发送到 {target_type} {target_id}",
            "text_blocks": [f"图片发送成功: {image_path}"],
            "internal_metadata": {
                "target_type": target_type,
                "target_id": target_id,
                "message_id": data.get("message_id") if isinstance(data, dict) else None,
            },
        }
    except Exception as exc:
        return {"success": False, "error": str(exc), "summary": f"图片发送失败: {exc}"}


async def _upload_file(
    bridge: Any,
    chat_context: dict[str, Any] | None,
    file_path: str,
    file_name: str,
) -> dict[str, Any]:
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

    path = Path(file_path)
    if not path.exists():
        return {
            "success": False,
            "error": f"文件不存在: {file_path}",
            "summary": "上传失败：文件不存在",
        }

    resolved_path = str(path.resolve())
    display_name = (file_name or "").strip() or path.name
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
        return {"success": False, "error": str(exc), "summary": f"文件上传失败: {exc}"}
