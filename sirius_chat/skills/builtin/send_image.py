"""Built-in NapCat-specific skill for sending images to the current chat."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

LOG = logging.getLogger(__name__)

SKILL_META = {
    "name": "send_image",
    "description": (
        "发送图片到当前对话。"
    ),
    "version": "1.0.0",
    "tags": ["napcat", "image", "messaging"],
    "silent": True,
    "adapter_types": ["napcat"],
    "dependencies": [],
    "parameters": {
        "image_path": {
            "type": "str",
            "description": "本地图片绝对路径或网络图片 URL",
            "required": True,
        },
    },
}


async def run(
    bridge: Any,
    chat_context: dict[str, Any] | None = None,
    image_path: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Send an image to the current chat via NapCat.

    Args:
        bridge: The NapCatBridge instance injected by SkillExecutor.
        chat_context: Current chat context injected by SkillExecutor.
        image_path: Absolute path to a local image or a remote URL.
        caption: Optional text caption to send before the image.
        sub_type: Optional sub_type for the image segment (e.g. "1" for sticker).
    """
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

    # Normalize local paths to absolute on Windows
    if "://" not in image_path and not image_path.startswith("file://"):
        p = Path(image_path)
        if p.exists():
            image_path = str(p.resolve())

    if image_path.startswith(("http://", "https://")):
        adapter = getattr(bridge, "adapter", None) or bridge
        cache_fn = getattr(adapter, "cache_image", None) if adapter else None
        if cache_fn is not None:
            try:
                local_path = await cache_fn(image_path)
                if local_path and not local_path.startswith(("http://", "https://")):
                    image_path = local_path
                    LOG.info("远程图片已缓存到本地: %s", local_path)
            except Exception as exc:
                LOG.warning("远程图片缓存失败，直接使用原始 URL: %s | %s", exc, image_path[:80])

    msg: list[dict[str, Any]] = []
    image_data: dict[str, Any] = {"file": image_path}
    msg.append({"type": "image", "data": image_data})

    try:
        if target_type == "group":
            result = await adapter.send_group_msg(target_id, msg)
        else:
            result = await adapter.send_private_msg(target_id, msg)

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
        return {
            "success": False,
            "error": str(exc),
            "summary": f"图片发送失败: {exc}",
        }
