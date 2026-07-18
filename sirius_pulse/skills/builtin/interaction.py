"""Unified NapCat skill for non-administrative chat interactions."""

from __future__ import annotations

from typing import Any

from sirius_pulse.config.config_builder import ConfigBuilder
from sirius_pulse.skills.models import SkillInvocationContext, SkillResult

_config = ConfigBuilder()
_config.group("互动操作").add(
    "action",
    type="str",
    description="操作类型：poke 戳一戳；image 发送图片；sticker 发送表情包；file 上传文件。",
    required=True,
    choices=["poke", "image", "sticker", "file"],
)
_config.group("互动操作").add("user_id", type="int", description="action=poke 时的目标 QQ 号。")
_config.group("互动操作").add(
    "image_path", type="str", description="action=image 时的本地图片路径或网络 URL。"
)
_config.group("互动操作").add(
    "names", type="list", description="action=sticker 时的候选表情包名称列表。"
)
_config.group("互动操作").add(
    "file_path", type="str", description="action=file 时要上传的本地文件路径。"
)
_config.group("互动操作").add(
    "file_name", type="str", description="action=file 时在聊天中显示的文件名。"
)

SKILL_META = {
    "name": "interaction",
    "description": (
        "统一处理普通群聊互动：戳一戳、发送图片、发送表情包或上传文件。"
        "管理员操作、开发者私聊和工作区文件读写使用其他 Skill。"
    ),
    "version": "1.0.0",
    "tags": ["napcat", "qq", "messaging"],
    "adapter_types": ["napcat"],
    "parameters": _config.build(),
}

_ACTION_SKILLS = {
    "poke": "poke",
    "image": "send_image",
    "sticker": "send_sticker",
    "file": "upload_file",
}


async def run(
    action: str,
    user_id: int | None = None,
    image_path: str = "",
    names: list[str] | str | None = None,
    file_path: str = "",
    file_name: str = "",
    engine_context: Any = None,
    invocation_context: SkillInvocationContext | None = None,
    **kwargs: Any,
) -> SkillResult | dict[str, Any]:
    action_key = str(action or "").strip().lower()
    target_name = _ACTION_SKILLS.get(action_key)
    if target_name is None:
        return {"success": False, "error": f"不支持的互动 action: {action}"}

    executor = getattr(engine_context, "skill_executor", None)
    registry = getattr(engine_context, "skill_registry", None)
    target = registry.get(target_name) if registry is not None else None
    if executor is None or target is None:
        return {"success": False, "error": f"内部互动 Skill 未就绪: {target_name}"}

    params: dict[str, Any] = {}
    if action_key == "poke":
        if user_id is not None:
            params["user_id"] = user_id
    elif action_key == "image":
        params["image_path"] = image_path
    elif action_key == "sticker":
        params["names"] = names
    else:
        params["file_path"] = file_path
        params["file_name"] = file_name

    result = await executor.execute_async(target, params, invocation_context=invocation_context)
    result.internal_metadata = {
        **result.internal_metadata,
        "interaction_action": action_key,
        "interaction_skill": target_name,
    }
    return result
