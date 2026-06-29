"""Built-in skill for maintaining model-owned user persona profiles."""

from __future__ import annotations

import json
from typing import Any

from sirius_pulse.config.config_builder import ConfigBuilder

_config = ConfigBuilder()
_config.group("人物画像").add(
    "action",
    type="str",
    description="操作：get/update/mark/list_events。默认 get。",
    default="get",
)
_config.group("人物画像").add(
    "target_user_id",
    type="str",
    description="要读取或维护的人物 user_id。省略时默认当前发言者。",
    default="",
)
_config.group("人物画像").add(
    "display_name",
    type="str",
    description="可选：该人物稳定显示名或称呼。仅 update 时使用。",
    default="",
)
_config.group("人物画像").add(
    "short_impression",
    type="str",
    description="可选：一句话长期印象。必须基于明确证据，不要写猜测。",
    default="",
)
_config.group("人物画像").add(
    "updates_json",
    type="str",
    description=(
        "update 时填写 JSON 数组。每项包含 section/key/value/confidence/evidence/operation。"
        "section 可用 aliases/identity/interests/preferences/communication_style/relationship/"
        "social_relations/boundaries/emotional_pattern/notes。operation 默认 upsert。"
    ),
    default="[]",
)
_config.group("人物画像").add(
    "section",
    type="str",
    description="mark 时要标记的 section。",
    default="",
)
_config.group("人物画像").add(
    "key",
    type="str",
    description="mark 时要标记的 item key。",
    default="",
)
_config.group("人物画像").add(
    "status",
    type="str",
    description="mark 状态：rejected 或 stale。",
    default="rejected",
)
_config.group("人物画像").add(
    "reason",
    type="str",
    description="调用原因。简短说明为什么该信息值得长期记住、修正或删除。",
    default="",
)

SKILL_META = {
    "name": "user_profile",
    "description": (
        "群聊里用户明确说出长期稳定的身份、偏好、称呼、关系、边界，或要求你记住/忘记/纠正资料时使用；"
        "不要记录临时玩笑、一次性情绪或猜测。"
    ),
    "version": "1.0.0",
    "tags": ["memory", "profile", "identity"],
    "silent": True,
    "dependencies": [],
    "parameters": _config.build(),
}


def run(
    action: str = "get",
    target_user_id: str = "",
    display_name: str = "",
    short_impression: str = "",
    updates_json: str = "[]",
    section: str = "",
    key: str = "",
    status: str = "rejected",
    reason: str = "",
    chat_context: dict[str, Any] | None = None,
    engine_context: Any = None,
    **kwargs: Any,
) -> dict[str, Any]:
    if engine_context is None:
        return {"success": False, "error": "engine_context 未就绪，无法维护人物画像"}
    manager = getattr(engine_context, "profile_manager", None)
    if manager is None:
        return {"success": False, "error": "profile_manager 未就绪"}

    group_id = str((chat_context or {}).get("group_id", "") or "default")
    user_id = str(target_user_id or (chat_context or {}).get("user_id", "") or "").strip()
    if not user_id:
        return {"success": False, "error": "target_user_id 不能为空"}

    action_key = str(action or "get").strip().lower()
    if action_key == "get":
        profile = manager.get_profile(group_id, user_id, create=False)
        return {
            "success": True,
            "found": profile is not None,
            "profile": profile.to_dict() if profile else None,
            "profile_card": manager.render_profile_card(group_id, user_id),
        }

    if action_key == "list_events":
        return {
            "success": True,
            "events": manager.list_events(group_id, user_id, limit=20),
        }

    if action_key == "mark":
        result = manager.mark_item(
            group_id=group_id,
            user_id=user_id,
            section=section,
            key=key,
            status=status,
            reason=reason,
            created_by="user_profile_skill",
        )
        return _skill_response(result)

    if action_key != "update":
        return {"success": False, "error": f"不支持的 action: {action}"}

    try:
        raw_updates = json.loads(updates_json or "[]")
    except json.JSONDecodeError as exc:
        return {"success": False, "error": f"updates_json 不是合法 JSON: {exc}"}
    if isinstance(raw_updates, dict):
        raw_updates = [raw_updates]
    if not isinstance(raw_updates, list):
        return {"success": False, "error": "updates_json 必须是对象数组"}

    result = manager.update_profile(
        group_id=group_id,
        user_id=user_id,
        updates=[x for x in raw_updates if isinstance(x, dict)],
        display_name=display_name,
        short_impression=short_impression,
        reason=reason,
        created_by="user_profile_skill",
    )
    return _skill_response(result)


def _skill_response(result: dict[str, Any]) -> dict[str, Any]:
    if not result.get("success"):
        return result
    return {
        "success": True,
        "summary": "人物画像已更新",
        "text_blocks": [result.get("profile_card", "") or "人物画像已更新"],
        "internal_metadata": result,
    }
