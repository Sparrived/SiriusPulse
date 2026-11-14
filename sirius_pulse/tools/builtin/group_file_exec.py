"""Unified NapCat tool for images and group-file management."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from sirius_pulse.config.config_builder import ConfigBuilder
from sirius_pulse.tools.models import ToolInvocationContext
from sirius_pulse.visual.tooling import visual_context

LOG = logging.getLogger(__name__)

_config = ConfigBuilder()
_config.group("图片与文件").add(
    "action",
    type="str",
    description=(
        "操作类型：image 发送图片；file 上传文件；list 读取群文件列表；"
        "download 下载聊天里收到的文件或群文件。"
    ),
    required=True,
    choices=["image", "file", "list", "download"],
)
_config.group("图片与文件").add("image_path", type="str", description="action=image 时的本地图片路径或网络 URL。")
_config.group("图片与文件").add(
    "file_path",
    type="str",
    description="action=file 时要上传的本地文件路径；~ 展开为用户主目录。",
)
_config.group("图片与文件").add(
    "file_name",
    type="str",
    description="action=file 时在聊天中显示的文件名；action=download 时的本地文件名，留空沿用原文件名。",
)
_config.group("群文件管理").add("folder_id", type="str", description="action=list 时要读取的群文件夹 ID；留空读取根目录。")
_config.group("群文件管理").add(
    "file_count", type="int", description="action=list 时最多读取的文件数。", default=50
)
_config.group("群文件管理").add(
    "file_id",
    type="str",
    description="action=download 时的文件 ID：聊天消息里 [文件：… file_id=…] 标签上的，或群文件列表里的。",
)
_config.group("群文件管理").add(
    "download_dir",
    type="str",
    description="action=download 时的本地保存目录；留空保存到当前人格的 group_files。",
)
TOOL_META = {
    "name": "group_file_exec",
    "description": (
        "往这个聊天发图片或文件、下载别人发来的文件、查看群文件。纯文字回复直接写在正文里。"
        '聊天里收到的文件会显示成 [文件："名字" 大小 file_id=…]：'
        "要看内容就 download 这个 file_id，再用 read_document 读下载到的路径。"
        "list 只在群聊能用；在线传输的文件下载不了。"
    ),
    "version": "1.3.0",
    "side_effect": "external_write",
    # 发图本身就是回复：同轮正文不必再单独发；上传文件仍照常发正文。
    "actions": {"image": {"silent": True}},
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
    data_store: Any = None,
    invocation_context: ToolInvocationContext | None = None,
    engine_context: Any = None,
    **kwargs: Any,
) -> dict[str, Any]:
    action_key = str(action or "").strip().lower()
    if action_key == "image":
        result = await _send_image(
            bridge,
            chat_context,
            image_path,
            data_store=data_store,
            invocation_context=invocation_context,
            engine_context=engine_context,
        )
    elif action_key == "file":
        result = await _upload_file(
            bridge,
            chat_context,
            file_path,
            file_name,
            data_store=data_store,
            invocation_context=invocation_context,
            engine_context=engine_context,
        )
    elif action_key == "list":
        result = await _list_group_files(bridge, chat_context, folder_id, file_count)
    elif action_key == "download":
        result = await _download_file(bridge, chat_context, file_id, file_name, download_dir)
    else:
        return {"success": False, "error": f"不支持的文件管理 action: {action}"}

    metadata = result.get("internal_metadata")
    result["internal_metadata"] = {
        **(metadata if isinstance(metadata, dict) else {}),
        "group_file_exec_action": action_key,
    }
    return result


def _delivery_receipt(kind: str, label: str, message_id: Any) -> dict[str, Any]:
    """已经送达聊天的图片/文件写回历史的一条回执。

    发图、传文件不产生聊天文本，她下一轮看不到自己发过，就会重发同一张图。
    只有真正投递成功才生成；列目录、下载这类本地动作没有回执。
    """
    if kind == "file":
        subject = f"文件「{label}」" if label else "文件"
    else:
        subject = f"图片 {label}" if label else "图片"
    return {
        "content": f"（已发送{subject}；除非用户明确要求重发，否则不要再发）",
        "tags": [{"type": kind, "label": label}],
        "platform_message_id": str(message_id or ""),
    }


async def _send_image(
    bridge: Any,
    chat_context: dict[str, Any] | None,
    image_path: str,
    *,
    data_store: Any = None,
    invocation_context: ToolInvocationContext | None = None,
    engine_context: Any = None,
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

    if image_path.startswith(("http://", "https://")):
        service, access = visual_context(engine_context, invocation_context)
        if service is not None and access is not None:
            # 外链也核验实际字节，不能在缓存失败时退回不受管的 URL 转发。
            import base64

            from sirius_pulse.tools.builtin.read_image import _download_image

            try:
                image_path = "base64://" + base64.b64encode(
                    await _download_image(image_path)
                ).decode("ascii")
            except Exception:
                return {"success": False, "error": "远程图片无法验证，未向平台发送"}

    # ``~`` must mean the persona's own home here too, not the process home:
    # the two are different once HOME is redirected into the mounted data dir.
    image_path = _resolve_persona_path(image_path.removeprefix("file://"), data_store)
    blocked = getattr(invocation_context, "blocked_image_paths", ())
    if (
        blocked
        and not image_path.startswith(("base64://", "data:"))
        and Path(image_path).resolve()
        in {
            Path(_resolve_persona_path(path.removeprefix("file://"), data_store)).resolve()
            for path in blocked
        }
    ):
        return {
            "success": False,
            "error": "旧产物已发送或投递状态无法确认，禁止本次回调重复发送；可继续加工新产物",
            "summary": "旧产物不重复发送",
        }

    display_path = image_path
    image_reference = _to_image_reference(image_path)
    visual_guard = await _guard_managed_path(
        engine_context, invocation_context, image_path, data_store
    )
    if visual_guard is not None and not visual_guard[2].get("allowed"):
        code = str(visual_guard[2].get("code") or "consent_required")
        return {
            "success": False,
            "error": "这张受管素材不可用或不在当前对话可见范围内",
            "summary": "发送失败：素材不可用或超出范围",
            "data": {"code": code},
        }

    message = [{"type": "image", "data": {"file": image_reference}}]
    try:
        callback_id = getattr(invocation_context, "background_event_id", "")
        if callback_id:
            # 同一回调明确选中的同一图片复用回执；未知 ACK 不因模型再次调用而重发。
            import hashlib

            if engine_context is None:
                raise RuntimeError("后台回调缺少带回执的发送上下文")
            image_key = hashlib.sha256(image_reference.encode("utf-8")).hexdigest()[:20]
            accepted = await engine_context.dispatch_proactive_message(
                group_id=invocation_context.group_id,
                text="",
                adapter_type=invocation_context.adapter_type,
                event_id=f"{callback_id}:image:{image_key}",
                image_path=image_path,
            )
            if accepted is not True:
                await _record_visual_delivery(
                    visual_guard,
                    success=False,
                    target=f"{target_type}:{target_id}",
                )
                return {
                    "success": False,
                    "error": "图片未确认送达；可能被拒收或回执未知，不要声称发送成功",
                    "summary": "图片未确认送达",
                }
            result = {}
        elif target_type == "group":
            result = await adapter.send_group_msg(target_id, message)
        else:
            result = await adapter.send_private_msg(target_id, message)

        data = result.get("data", {}) if isinstance(result, dict) else {}
        message_id = data.get("message_id") if isinstance(data, dict) else None
        await _record_visual_delivery(
            visual_guard,
            success=True,
            target=f"{target_type}:{target_id}",
            message_id=str(message_id or ""),
        )
        return {
            "success": True,
            "summary": f"图片已发送到 {target_type} {target_id}",
            "text_blocks": [f"图片发送成功: {display_path}"],
            "internal_metadata": {
                "target_type": target_type,
                "target_id": target_id,
                "message_id": message_id,
                "history_receipt": _delivery_receipt(
                    "image", str(image_path or "").strip(), message_id
                ),
            },
        }
    except Exception as exc:
        await _record_visual_delivery(
            visual_guard, success=False, target=f"{target_type}:{target_id}"
        )
        return {"success": False, "error": str(exc), "summary": f"图片发送失败: {exc}"}


async def _upload_file(
    bridge: Any,
    chat_context: dict[str, Any] | None,
    file_path: str,
    file_name: str,
    *,
    data_store: Any = None,
    invocation_context: ToolInvocationContext | None = None,
    engine_context: Any = None,
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
    file_path = _resolve_persona_path(file_path, data_store)
    path = Path(file_path).expanduser()
    try:
        problem = "" if path.exists() else f"文件不存在: {file_path}（展开后 {path}）"
    except PermissionError:
        problem = f"没有权限访问 {file_path}"
    if problem:
        return {
            "success": False,
            "error": f"{problem}；你的家目录是 {_home_hint(data_store)}，请确认路径。",
            "summary": f"上传失败：{problem}",
        }

    resolved_path = str(path.resolve())
    display_name = (file_name or "").strip() or path.name
    visual_guard = await _guard_managed_path(
        engine_context, invocation_context, resolved_path, data_store
    )
    if visual_guard is not None and not visual_guard[2].get("allowed"):
        code = str(visual_guard[2].get("code") or "consent_required")
        return {
            "success": False,
            "error": "这个受管文件不可用或不在当前对话可见范围内",
            "summary": "上传失败：素材不可用或超出范围",
            "data": {"code": code},
        }
    try:
        if target_type == "group":
            result = await adapter.upload_group_file(target_id, resolved_path, display_name)
        else:
            result = await adapter.upload_private_file(target_id, resolved_path, display_name)

        data = result.get("data", {}) if isinstance(result, dict) else {}
        message_id = data.get("message_id") if isinstance(data, dict) else None
        await _record_visual_delivery(
            visual_guard,
            success=True,
            target=f"{target_type}:{target_id}",
            message_id=str(message_id or ""),
        )
        return {
            "success": True,
            "summary": f"文件「{display_name}」已上传到 {target_type} {target_id}",
            "text_blocks": [f"文件上传成功: {resolved_path}"],
            "internal_metadata": {
                "target_type": target_type,
                "target_id": target_id,
                "file_name": display_name,
                "message_id": message_id,
                "history_receipt": _delivery_receipt(
                    "file", str(display_name or file_path or "").strip(), message_id
                ),
            },
        }
    except Exception as exc:
        await _record_visual_delivery(
            visual_guard, success=False, target=f"{target_type}:{target_id}"
        )
        return {"success": False, "error": str(exc), "summary": f"文件上传失败: {exc}"}


async def _guard_managed_path(
    engine_context: Any,
    invocation_context: Any,
    path: str,
    data_store: Any,
) -> tuple[Any, Any, dict[str, Any]] | None:
    """受管人物素材外发前按实际能力校验；普通文件路径保持原行为。"""
    service, access = visual_context(engine_context, invocation_context)
    if service is None or access is None:
        return None
    text = str(path or "").strip()
    if not text or text.startswith(("http://", "https://")):
        return None
    resolved = _resolve_persona_path(text, data_store)
    try:
        guard = await service.guard_path(resolved, access, "external_send", consume=True)
    except Exception as exc:  # noqa: BLE001 - 守卫异常按拒绝处理更安全
        LOG.warning("受管路径校验失败: %s", exc)
        return service, access, {"managed": True, "allowed": False, "code": "service_unavailable"}
    if not guard.get("managed"):
        return None
    return service, access, guard


async def _record_visual_delivery(
    context: tuple[Any, Any, dict[str, Any]] | None,
    *,
    success: bool,
    target: str,
    message_id: str = "",
) -> None:
    """独立送达回执：只有平台确认或回调 ACK 后才记成功。"""
    if context is None:
        return
    service, access, guard = context
    try:
        await service.record_delivery(
            access=access,
            asset_id=str(guard.get("asset_id") or ""),
            job_id=str(guard.get("job_id") or ""),
            channel="napcat",
            target=target,
            message_id=message_id,
            success=success,
            code="" if success else "delivery_failed",
        )
    except Exception as exc:  # noqa: BLE001 - 回执失败不能反过来阻断已完成的发送
        LOG.warning("记录视觉资产送达回执失败: %s", exc)


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


async def _download_file(
    bridge: Any,
    chat_context: dict[str, Any] | None,
    file_id: str,
    file_name: str,
    download_dir: str,
) -> dict[str, Any]:
    """下载聊天里收到的文件：群聊走群文件接口，私聊走私聊文件接口。"""
    adapter = getattr(bridge, "adapter", None) or bridge
    target_type, target_id = _chat_target(chat_context)
    if adapter is None:
        return {
            "success": False,
            "error": "adapter 未就绪",
            "summary": "下载失败：NapCat 适配器未连接",
        }
    if target_type not in {"group", "private"} or not target_id:
        return {
            "success": False,
            "error": "只能在群聊或私聊里下载收到的文件",
            "summary": "下载失败：没有聊天上下文",
        }

    file_id = str(file_id or "").strip()
    if not file_id:
        return {"success": False, "error": "file_id 不能为空", "summary": "下载失败：缺少文件 ID"}
    method = "download_group_file" if target_type == "group" else "download_private_file"
    download_fn = getattr(adapter, method, None)
    if not callable(download_fn):
        return {
            "success": False,
            "error": "当前适配器不支持下载文件",
            "summary": "下载失败：适配器不支持下载文件",
        }

    try:
        result = await download_fn(target_id, file_id, file_name, str(download_dir or "").strip())
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
            "summary": f"文件「{display_name}」已下载到 {output_path}",
            "text_blocks": [
                f"文件下载成功: {output_path}{size_text}。要看文字内容，用 read_document 读这个路径。"
            ],
            "data": payload,
            "internal_metadata": {
                "target_type": target_type,
                "target_id": target_id,
                "file_id": file_id,
                "file_name": display_name,
                "path": output_path,
                "size": size,
            },
        }
    except Exception as exc:
        return {"success": False, "error": str(exc), "summary": f"文件下载失败: {exc}"}


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


def _home_hint(data_store: Any) -> str:
    """The home to name in an error message: hers when known, else the process's."""
    from sirius_pulse.tools.builtin._internal import _bash_runtime

    return str(_bash_runtime.home_root_path(data_store) or Path.home())


def _resolve_persona_path(value: str, data_store: Any) -> str:
    """Expand a model-supplied local path against the persona's own home.

    URLs and non-local references are returned untouched.  ``~`` has to mean the
    same thing to this tool as it does to Bash: the persona home inside the
    mounted data directory, which survives a container rebuild, rather than the
    process home baked into the image.
    """
    text = str(value or "").strip()
    if not text or text.startswith(("http://", "https://", "base64://", "data:")):
        return text
    from sirius_pulse.tools.builtin._internal import _bash_runtime

    home = _bash_runtime.home_root_path(data_store)
    if home is None or not text.startswith("~"):
        return text
    return str(_bash_runtime.expand_persona_path(text, home=home))


def _to_image_reference(image_path: str) -> str:
    """Encode local images so NapCat need not access this container's filesystem."""
    from sirius_pulse.tools.builtin._internal._markdown_image import to_image_reference

    return to_image_reference(image_path)
