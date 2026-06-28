"""Built-in skill for sending persona stickers to the current chat."""

from __future__ import annotations

from typing import Any

from sirius_pulse.config.config_builder import ConfigBuilder

_config = ConfigBuilder()
_config.group("表情包发送").add(
    "names",
    type="list",
    description=("候选表情包名称列表。请只填写可选表情包名称中的原始名称，" "系统会从前 3 个候选中随机选择 1 个发送。"),
    required=True,
)

SKILL_META = {
    "name": "send_sticker",
    "description": ("发送一张当前人格表情包到当前聊天。必须配合文字回复一起使用（例如边说边发表情包），" "不要单独发送。"),
    "version": "1.0.0",
    "tags": ["sticker", "messaging", "napcat"],
    "adapter_types": ["napcat"],
    "dependencies": [],
    "parameters": _config.build(),
}


async def run(
    names: list[str] | str | None = None,
    chat_context: dict[str, Any] | None = None,
    engine_context: Any = None,
    **kwargs: Any,
) -> dict[str, Any]:
    if engine_context is None:
        return {
            "success": False,
            "error": "engine_context 未就绪，无法发送表情包",
        }

    group_id = (chat_context or {}).get("group_id", "")
    if not group_id:
        return {
            "success": False,
            "error": "当前聊天上下文缺少 group_id",
        }

    candidates = _normalize_names(names)
    if not candidates:
        return {
            "success": False,
            "error": "names 不能为空",
        }

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
