"""Unified NapCat tool for images and group-file management."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from sirius_pulse.config.config_builder import ConfigBuilder
from sirius_pulse.tools.models import ToolInvocationContext

LOG = logging.getLogger(__name__)

_config = ConfigBuilder()
_config.group("图片与文件").add(
    "action",
    type="str",
    description=(
        "操作类型：image 发送图片；file 上传文件；list 读取当前群文件列表；"
        "download 下载当前群文件。你正在以当前人格参与对话，"
        "当角色想分享截图、图片、资料或管理群文件时，直接使用这个工具完成互动。"
    ),
    required=True,
    choices=["image", "file", "list", "download"],
)
_config.group("图片与文件").add("image_path", type="str", description="action=image 时的本地图片路径或网络 URL。")
_config.group("图片与文件").add(
    "file_path",
    type="str",
    description="action=file 时要上传的本地文件路径；~ 展开为当前进程家目录。",
)
_config.group("图片与文件").add(
    "file_name",
    type="str",
    description="action=file 时在聊天中显示的文件名；action=download 时的本地文件名。",
)
_config.group("群文件管理").add("folder_id", type="str", description="action=list 时要读取的群文件夹 ID；留空读取根目录。")
_config.group("群文件管理").add(
    "file_count", type="int", description="action=list 时最多读取的文件数。", default=50
)
_config.group("群文件管理").add("file_id", type="str", description="action=download 时从群文件列表得到的文件 ID。")
_config.group("群文件管理").add(
    "download_dir",
    type="str",
    description="action=download 时的本地保存目录；留空保存到当前人格的 group_files。",
)
TOOL_META = {
    "name": "group_file_exec",
    "description": (
        "以当前人格参与聊天时用于发送图片或上传文件的互动工具：当图片、截图、资料或文件"
        "能让角色的表达更具体、更自然时主动调用，不要只在正文里描述一个本地路径。"
        "也可读取当前群文件列表或下载群文件到本地。"
        "纯文字回复直接写在正文中，不要每轮强行调用。"
    ),
    "version": "1.2.0",
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
    folder_id: str = "",
    file_count: int = 50,
    file_id: str = "",
    download_dir: str = "",
    bridge: Any = None,
    chat_context: dict[str, Any] | None = None,
    invocation_context: ToolInvocationContext | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    action_key = str(action or "").strip().lower()
    if action_key == "image":
        result = await _send_image(bridge, chat_context, image_path)
    elif action_key == "file":
        result = await _upload_file(bridge, chat_context, file_path, file_name)
    elif action_key == "list":
        result = await _list_group_files(bridge, chat_context, folder_id, file_count)
    elif action_key == "download":
        result = await _download_group_file(bridge, chat_context, file_id, file_name, download_dir)
    else:
        return {"success": False, "error": f"不支持的文件管理 action: {action}"}

    metadata = result.get("internal_metadata")
    result["internal_metadata"] = {
        **(metadata if isinstance(metadata, dict) else {}),
        "group_file_exec_action": action_key,
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
    target_type, target_id = _chat_target(chat_context)
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

    display_path = image_path
    image_reference = _to_image_reference(image_path)

    message = [{"type": "image", "data": {"file": image_reference}}]
    try:
        if target_type == "group":
            result = await adapter.send_group_msg(target_id, message)
        else:
            result = await adapter.send_private_msg(target_id, message)

        data = result.get("data", {}) if isinstance(result, dict) else {}
        return {
            "success": True,
            "summary": f"图片已发送到 {target_type} {target_id}",
            "text_blocks": [f"图片发送成功: {display_path}"],
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
    target_type, target_id = _chat_target(chat_context)
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

    # path.exists() raises (rather than returning False) on an unreadable parent,
    # so the bare errno must be turned into a hint the model can act on.
    path = Path(file_path).expanduser()
    try:
        problem = "" if path.exists() else f"文件不存在: {file_path}（展开后 {path}）"
    except PermissionError:
        problem = f"没有权限访问 {file_path}"
    if problem:
        return {
            "success": False,
            "error": f"{problem}；当前进程家目录是 {Path.home()}，请确认路径。",
            "summary": f"上传失败：{problem}",
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


async def _list_group_files(
    bridge: Any,
    chat_context: dict[str, Any] | None,
    folder_id: str,
    file_count: int,
) -> dict[str, Any]:
    adapter = getattr(bridge, "adapter", None) or bridge
    target_type, group_id = _chat_target(chat_context)
    if adapter is None:
        return {
            "success": False,
            "error": "adapter 未就绪",
            "summary": "读取失败：NapCat 适配器未连接",
        }
    if target_type != "group" or not group_id:
        return {
            "success": False,
            "error": "群文件只能在群聊上下文中读取",
            "summary": "读取失败：当前不是群聊",
        }

    list_fn = getattr(adapter, "get_group_file_list", None)
    if not callable(list_fn):
        return {
            "success": False,
            "error": "当前适配器不支持群文件列表",
            "summary": "读取失败：适配器不支持群文件列表",
        }

    try:
        folder_id = str(folder_id or "").strip()
        count = _clamp_file_count(file_count)
        result = await list_fn(group_id, folder_id, count)
        payload = result if isinstance(result, dict) else {}
        files = payload.get("files") if isinstance(payload.get("files"), list) else []
        folders = payload.get("folders") if isinstance(payload.get("folders"), list) else []
        lines = [f"群 {group_id} 的群文件列表（{'根目录' if not folder_id else f'文件夹 {folder_id}'}）："]
        lines.extend(_format_group_folder(item) for item in folders if isinstance(item, dict))
        lines.extend(_format_group_file(item) for item in files if isinstance(item, dict))
        if len(lines) == 1:
            lines.append("（暂无文件或文件夹）")
        return {
            "success": True,
            "summary": f"已读取群 {group_id} 的群文件列表，共 {len(files)} 个文件、{len(folders)} 个文件夹",
            "text_blocks": lines,
            "data": {
                "group_id": group_id,
                "folder_id": folder_id,
                "files": files,
                "folders": folders,
            },
            "internal_metadata": {"target_type": "group", "target_id": group_id},
        }
    except Exception as exc:
        return {"success": False, "error": str(exc), "summary": f"群文件列表读取失败: {exc}"}


async def _download_group_file(
    bridge: Any,
    chat_context: dict[str, Any] | None,
    file_id: str,
    file_name: str,
    download_dir: str,
) -> dict[str, Any]:
    adapter = getattr(bridge, "adapter", None) or bridge
    target_type, group_id = _chat_target(chat_context)
    if adapter is None:
        return {
            "success": False,
            "error": "adapter 未就绪",
            "summary": "下载失败：NapCat 适配器未连接",
        }
    if target_type != "group" or not group_id:
        return {
            "success": False,
            "error": "群文件只能在群聊上下文中下载",
            "summary": "下载失败：当前不是群聊",
        }

    file_id = str(file_id or "").strip()
    if not file_id:
        return {"success": False, "error": "file_id 不能为空", "summary": "下载失败：缺少群文件 ID"}
    download_fn = getattr(adapter, "download_group_file", None)
    if not callable(download_fn):
        return {
            "success": False,
            "error": "当前适配器不支持群文件下载",
            "summary": "下载失败：适配器不支持群文件下载",
        }

    try:
        result = await download_fn(group_id, file_id, file_name, str(download_dir or "").strip())
        payload = result if isinstance(result, dict) else {}
        output_path = str(payload.get("path", "")).strip()
        if not output_path:
            return {
                "success": False,
                "error": "适配器未返回下载文件路径",
                "summary": "下载失败：未生成本地文件",
            }
        display_name = str(payload.get("file_name") or file_name or file_id)
        size = payload.get("size")
        size_text = f"，大小 {size} 字节" if isinstance(size, int) else ""
        return {
            "success": True,
            "summary": f"群文件「{display_name}」已下载到 {output_path}",
            "text_blocks": [f"群文件下载成功: {output_path}{size_text}"],
            "data": payload,
            "internal_metadata": {
                "target_type": "group",
                "target_id": group_id,
                "file_id": file_id,
                "file_name": display_name,
                "path": output_path,
                "size": size,
            },
        }
    except Exception as exc:
        return {"success": False, "error": str(exc), "summary": f"群文件下载失败: {exc}"}


def _clamp_file_count(value: Any) -> int:
    try:
        return max(1, min(int(value), 200))
    except (TypeError, ValueError):
        return 50


def _format_group_folder(folder: dict[str, Any]) -> str:
    name = str(folder.get("folder_name") or folder.get("name") or "未命名文件夹")
    folder_id = str(folder.get("folder_id") or "")
    suffix = f"，folder_id={folder_id}" if folder_id else ""
    return f"[文件夹] {name}{suffix}"


def _format_group_file(file_info: dict[str, Any]) -> str:
    name = str(file_info.get("file_name") or file_info.get("name") or "未命名文件")
    file_id = str(file_info.get("file_id") or "")
    size = file_info.get("file_size")
    details = [f"file_id={file_id}"] if file_id else []
    if size is not None:
        details.append(f"size={size} 字节")
    suffix = f"（{'，'.join(details)}）" if details else ""
    return f"[文件] {name}{suffix}"


def _chat_target(chat_context: dict[str, Any] | None) -> tuple[str, str]:
    """Resolve group and private targets from runtime and direct-call contexts."""
    context = chat_context or {}
    chat_type = str(context.get("chat_type") or "").strip().lower()
    chat_id = str(context.get("chat_id") or "").strip()
    group_id = str(context.get("group_id") or "").strip()
    user_id = str(context.get("user_id") or "").strip()

    if chat_type and chat_type not in {"group", "private"}:
        return "", ""
    if not chat_type:
        if group_id.startswith("private_") or user_id:
            chat_type = "private"
        elif group_id:
            chat_type = "group"
        else:
            return "", ""

    if chat_type == "group":
        target_id = chat_id or group_id
        if not target_id or target_id.startswith("private_"):
            return "", ""
        return "group", target_id

    for candidate in (chat_id, user_id, group_id):
        target_id = _private_target_id(candidate)
        if target_id:
            return "private", target_id
    return "", ""


def _private_target_id(value: str) -> str:
    target_id = value.strip()
    if target_id.startswith("private_"):
        target_id = target_id.removeprefix("private_")
    if target_id.startswith("qq_"):
        target_id = target_id.removeprefix("qq_")
    return target_id


def _to_image_reference(image_path: str) -> str:
    """Encode local images so NapCat need not access this container's filesystem."""
    from sirius_pulse.tools.builtin._internal._markdown_image import to_image_reference

    return to_image_reference(image_path)
