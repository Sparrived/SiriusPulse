"""Unified NapCat skill for social chat interactions."""

from __future__ import annotations

from typing import Any

from sirius_pulse.config.config_builder import ConfigBuilder
from sirius_pulse.skills.builtin._qq_ops import (
    bridge_error,
    current_group_id,
    failure_from_exception,
    get_adapter,
    group_error,
    success_result,
)
from sirius_pulse.skills.models import SkillInvocationContext

_config = ConfigBuilder()
_config.group("互动操作").add(
    "action",
    type="str",
    description=(
        "操作类型：poke 戳一戳；sticker 发送表情包。你正在以当前人格参与群聊，"
        "当角色想打招呼、轻轻提醒、催促、卖萌、安慰或接梗时，优先用这个工具表达动作；"
        "图片和文件使用 file_upload。"
    ),
    required=True,
    choices=["poke", "sticker"],
)
_config.group("互动操作").add("user_id", type="int", description="action=poke 时的目标 QQ 号。")
_config.group("互动操作").add(
    "names", type="list", description="action=sticker 时的候选表情包名称列表。"
)

SKILL_META = {
    "name": "interaction",
    "description": (
        "以当前人格参与群聊时使用的社交互动工具：当角色的情绪、语气或关系适合时，"
        "自然、主动地在回复中使用戳一戳或表情包让回应更像真实互动。"
    ),
    "version": "1.0.0",
    "side_effect": "external_write",
    "tags": ["napcat", "qq", "messaging"],
    "adapter_types": ["napcat"],
    "parameters": _config.build(),
}


async def run(
    action: str,
    user_id: int | None = None,
    names: list[str] | str | None = None,
    bridge: Any = None,
    chat_context: dict[str, Any] | None = None,
    engine_context: Any = None,
    invocation_context: SkillInvocationContext | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    action_key = str(action or "").strip().lower()
    if action_key == "poke":
        result = await _run_poke(user_id, bridge, chat_context)
    elif action_key == "sticker":
        result = await _run_sticker(names, chat_context, engine_context)
    else:
        return {"success": False, "error": f"不支持的互动 action: {action}"}

    metadata = result.get("internal_metadata")
    result["internal_metadata"] = {
        **(metadata if isinstance(metadata, dict) else {}),
        "interaction_action": action_key,
    }
    return result


async def _run_poke(
    user_id: int | None,
    bridge: Any,
    chat_context: dict[str, Any] | None,
) -> dict[str, Any]:
    if user_id is None:
        return {"success": False, "error": "缺少必填参数: user_id"}
    adapter = get_adapter(bridge)
    if adapter is None:
        return bridge_error("戳一戳")
    group_id = current_group_id(chat_context)
    if not group_id:
        return group_error("戳一戳")
    try:
        result = await adapter.send_poke(str(user_id), group_id)
        return success_result("已发送戳一戳", user_id=user_id, group_id=group_id, raw=result)
    except Exception as exc:
        return failure_from_exception("戳一戳", exc)


async def _run_sticker(
    names: list[str] | str | None,
    chat_context: dict[str, Any] | None,
    engine_context: Any,
) -> dict[str, Any]:
    if engine_context is None:
        return {"success": False, "error": "engine_context 未就绪，无法发送表情包"}

    group_id = (chat_context or {}).get("group_id", "")
    if not group_id:
        return {"success": False, "error": "当前聊天上下文缺少 group_id"}

    candidates = _normalize_names(names)
    if not candidates:
        return {"success": False, "error": "names 不能为空"}

    available = set(engine_context.list_sticker_names())
    filtered = [name for name in candidates if name in available]
    if not filtered:
        return {
            "success": False,
            "error": f"没有匹配的表情包名称，可选名称：{', '.join(sorted(available)[:30])}",
        }

    result = await engine_context.send_sticker_by_names(group_id, filtered[:3])
    if result.get("success"):
        return {
            "success": True,
            "summary": f"已发送表情包：{result.get('sticker_name', filtered[0])}",
            "internal_metadata": result,
        }
    return {
        "success": False,
        "error": str(result.get("error", "表情包发送失败")),
        "internal_metadata": result,
    }


def _normalize_names(value: list[str] | str | None) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        raw = value.replace("，", ",")
        return [part.strip().strip("'\"") for part in raw.split(",") if part.strip()]
    if isinstance(value, list):
        return [str(item).strip().strip("'\"") for item in value if str(item).strip()]
    return []
