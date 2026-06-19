"""Built-in skill for confirmed person-alias management."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sirius_pulse.config.config_builder import ConfigBuilder
from sirius_pulse.memory.alias_policy import validate_person_alias

MIN_CONFIDENCE = 0.70

_config = ConfigBuilder()
_config.group("人物别称").add(
    "action",
    type="str",
    description="操作类型：add=登记/更新别称，remove=删除别称，resolve=查询别称指向，list=列出当前聊天已确认别称。",
    required=True,
    choices=["add", "remove", "resolve", "list"],
)
_config.group("人物别称").add(
    "alias",
    type="str",
    description="要管理的别称。不能是哥、姐、弟、妹、大哥、姐姐、老师、同学、朋友等宽泛称呼。",
    default="",
)
_config.group("人物别称").add(
    "target_user_id",
    type="str",
    description="别称指向的唯一用户 ID。add 时优先提供；remove 时可选，用于确认要删除的目标。",
    default="",
)
_config.group("人物别称").add(
    "target_name",
    type="str",
    description="当不知道 target_user_id 时填写群内用户的精确显示名。不要填写被登记的别称本身。",
    default="",
)
_config.group("人物别称").add(
    "confidence",
    type="float",
    description="模型确认该别称映射的置信度，0 到 1。add 必须不低于 0.70；不确定时不要登记。",
    default=0.0,
)
_config.group("人物别称").add(
    "evidence",
    type="str",
    description="登记依据，简短说明为什么能确认这是某个具体人的别称。",
    default="",
)

SKILL_META = {
    "name": "person_alias",
    "description": (
        "管理不同人的已确认别称映射。仅当聊天中明确说明某个具体人有某个别称，"
        "或用户明确要求记录/删除/查询别称时使用。禁止把宽泛称呼登记为别称，"
        "例如哥、姐、弟、妹、大哥、姐姐、老师、同学、朋友、宝贝等。"
        "每个别称只能指向一个人；如果同一别称改指向其他人，会替换旧映射。"
    ),
    "version": "1.0.0",
    "tags": ["memory", "identity", "alias"],
    "silent": True,
    "dependencies": [],
    "parameters": _config.build(),
}


def run(
    action: str = "",
    alias: str = "",
    target_user_id: str = "",
    target_name: str = "",
    confidence: float = 0.0,
    evidence: str = "",
    chat_context: dict[str, Any] | None = None,
    engine_context: Any = None,
    data_store: Any = None,
    **kwargs: Any,
) -> dict[str, Any]:
    if engine_context is None:
        return {"success": False, "error": "engine_context 未就绪，无法管理人物别称"}

    action_key = str(action or "").strip().lower()
    group_id = str((chat_context or {}).get("group_id", "") or "default")
    alias_ok, alias_key, reason = validate_person_alias(alias)
    if action_key in {"add", "remove", "resolve"} and not alias_ok:
        return {"success": False, "error": reason, "alias": alias_key}

    conf = _coerce_confidence(confidence)
    if action_key == "add" and conf < MIN_CONFIDENCE:
        return {
            "success": False,
            "error": f"置信度 {conf:.2f} 低于 {MIN_CONFIDENCE:.2f}，别称未登记",
            "alias": alias_key,
        }

    result = engine_context.manage_person_alias(
        action=action_key,
        alias=alias_key,
        target_user_id=target_user_id,
        target_name=target_name,
        group_id=group_id,
        confidence=conf,
        evidence=evidence.strip(),
    )
    if not result.get("success"):
        return result

    _update_self_records(
        data_store=data_store,
        result=result,
        action=action_key,
        alias=alias_key,
        group_id=group_id,
        confidence=conf,
        evidence=evidence.strip(),
    )

    return _format_result(result, action_key)


def _coerce_confidence(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value)))
    except (TypeError, ValueError):
        return 0.0


def _update_self_records(
    *,
    data_store: Any,
    result: dict[str, Any],
    action: str,
    alias: str,
    group_id: str,
    confidence: float,
    evidence: str,
) -> None:
    if data_store is None:
        return
    records = data_store.get("aliases", {})
    if not isinstance(records, dict):
        records = {}
    now = datetime.now(timezone.utc).isoformat()
    if action == "add" and result.get("success"):
        records[alias] = {
            "alias": alias,
            "user_id": result.get("user_id", ""),
            "user_name": result.get("user_name", ""),
            "group_id": group_id,
            "confidence": confidence,
            "evidence": evidence,
            "updated_at": now,
        }
    elif action == "remove" and result.get("removed"):
        records.pop(alias, None)
    else:
        return
    data_store.set("aliases", records)


def _format_result(result: dict[str, Any], action: str) -> dict[str, Any]:
    if action == "list":
        aliases = result.get("aliases", {})
        lines = ["当前聊天已确认人物别称："]
        if isinstance(aliases, dict) and aliases:
            for alias, item in sorted(aliases.items()):
                lines.append(
                    f"- {alias} -> {item.get('user_name') or item.get('user_id')} "
                    f"(confidence={float(item.get('confidence', 0.0)):.2f})"
                )
        else:
            lines.append("- 无")
        return {"success": True, "text_blocks": ["\n".join(lines)], "internal_metadata": result}

    if action == "resolve":
        if not result.get("found"):
            return {
                "success": True,
                "text_blocks": [f"未找到别称「{result.get('alias', '')}」的已确认映射。"],
                "internal_metadata": result,
            }
        return {
            "success": True,
            "text_blocks": [
                f"别称「{result.get('alias', '')}」指向 {result.get('user_name') or result.get('user_id')} "
                f"(confidence={float(result.get('confidence', 0.0)):.2f})"
            ],
            "internal_metadata": result,
        }

    if action == "remove":
        alias = result.get("alias", "")
        removed = "已删除" if result.get("removed") else "未找到"
        return {
            "success": True,
            "text_blocks": [f"{removed}别称「{alias}」。"],
            "internal_metadata": result,
        }

    return {
        "success": True,
        "text_blocks": [
            f"已记录别称「{result.get('alias', '')}」 -> {result.get('user_name') or result.get('user_id')} "
            f"(confidence={float(result.get('confidence', 0.0)):.2f})"
        ],
        "internal_metadata": result,
    }
