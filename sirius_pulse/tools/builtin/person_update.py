"""Evidence-bound profile changes, with no arbitrary field or account patch surface."""

from __future__ import annotations

from typing import Any

from sirius_pulse.people import PersonError
from sirius_pulse.tools.engine_context import PeopleAccess

_ACTIONS = [
    "set_names",
    "propose_claim",
    "correct_claim",
    "retire_claim",
    "confirm_change",
    "create_character",
]

TOOL_META = {
    "name": "person_update",
    "description": (
        "【适用场景】依据当前原消息指令维护人物称谓、更正属性声明、确认画像变更或登记原创角色。\n"
        "【使用方法】action 选择操作（set_names/propose_claim/correct_claim/"
        "retire_claim/confirm_change/create_character）；"
        "target 传入主体引用；evidence_ref 传入来源消息证据引用。\n"
        "【注意事项】必须具备有效证据引用；仅能维护授权范围内的资料，严禁篡改账号绑定、系统权限或任意画像字段。"
    ),
    "version": "1.1.0",
    "side_effect": "external_write",
    "redact_logs": True,
    "engagement_exempt": True,
    "request_source": True,
    "tags": ["person", "profile", "update"],
    "parameters": {
        "action": {"type": "string", "choices": _ACTIONS, "required": True},
        "target": {"type": "object", "description": "人物引用；requester 必须是来源消息作者。", "required": True},
        "evidence_ref": {"type": "string", "required": True},
        "preferred_name": {"type": "string", "required": False, "default": ""},
        "add_aliases": {
            "type": "array",
            "items": {"type": "string"},
            "required": False,
            "default": [],
        },
        "retire_names": {
            "type": "array",
            "items": {"type": "string"},
            "required": False,
            "default": [],
        },
        "expected_revision": {"type": "integer", "required": False},
        "claim_id": {"type": "string", "required": False, "default": ""},
        "change_id": {"type": "string", "required": False, "default": ""},
        "field": {"type": "string", "required": False},
        "value": {"type": "string", "required": False},
        "source": {
            "type": "string",
            "choices": ["self_reported", "third_party", "inferred"],
            "required": False,
            "default": "self_reported",
        },
        "name": {"type": "string", "required": False},
    },
}


async def run(
    action: str,
    target: dict[str, Any],
    evidence_ref: str,
    preferred_name: str = "",
    add_aliases: list[str] | None = None,
    retire_names: list[str] | None = None,
    expected_revision: int | None = None,
    claim_id: str = "",
    change_id: str = "",
    field: str = "",
    value: str = "",
    source: str = "self_reported",
    name: str = "",
    engine_context: PeopleAccess | None = None,
    invocation_context: Any = None,
    **kwargs: Any,
) -> dict[str, Any]:
    del kwargs
    try:
        if action not in _ACTIONS:
            raise PersonError("action_invalid")
        if isinstance(expected_revision, bool) or (
            expected_revision is not None and expected_revision < 0
        ):
            raise PersonError("invalid_argument")
        service = engine_context.get_person_service()
        access = engine_context.get_person_access(invocation_context)
        person_id = service.resolve_target(target, access, source_ref=evidence_ref)
        if action == "set_names":
            result = service.update_names(
                person_id,
                access,
                evidence_ref,
                preferred_name=preferred_name,
                add_aliases=add_aliases,
                retire_names=retire_names,
                expected_revision=expected_revision,
            )
        elif action == "propose_claim":
            result = service.propose_claim(
                person_id,
                field,
                value,
                access,
                evidence_ref,
                source=source,
                expected_revision=expected_revision,
            )
        elif action in {"correct_claim", "retire_claim"}:
            result = service.change_claim(
                action,
                person_id,
                str(claim_id),
                value,
                access,
                evidence_ref,
                expected_revision=expected_revision,
            )
        elif action == "confirm_change":
            if not change_id:
                raise PersonError("invalid_argument", "confirm_change 需要 change_id")
            result = service.confirm_change(
                person_id,
                str(change_id),
                access,
                evidence_ref,
                expected_revision=expected_revision,
            )
        else:
            result = service.create_character(name, access, evidence_ref)
        return {"success": True, **result}
    except Exception as exc:
        return {"success": False, "error": getattr(exc, "code", "person_update_failed")}
