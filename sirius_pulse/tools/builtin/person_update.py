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
        "action": {
            "type": "string",
            "choices": _ACTIONS,
            "description": (
                "set_names 设称呼/别名（preferred_name、add_aliases、retire_names）；"
                "propose_claim 记一条属性声明（field、value、source）；"
                "correct_claim 更正已有声明（claim_id、value）；retire_claim 撤下声明（claim_id）；"
                "confirm_change 确认一条待确认的变更（change_id）；"
                "create_character 登记原创/虚构角色（name）。"
            ),
            "required": True,
        },
        "target": {
            "type": "object",
            "description": (
                "改谁，写法同 person_query：{'ref': 'requester'}、{'ref': 'person_id', 'value': '<UUID>'}"
                " 等；requester 必须是 evidence_ref 那条消息的作者。"
            ),
            "required": True,
        },
        "evidence_ref": {
            "type": "string",
            "description": (
                "提出这次修改的那条消息的 evidence_ref（聊天记录里每条消息都带着），原样复制，不能编造。"
            ),
            "required": True,
        },
        "preferred_name": {
            "type": "string",
            "description": "set_names：对方希望被怎么称呼；留空则不改。",
            "required": False,
            "default": "",
        },
        "add_aliases": {
            "type": "array",
            "items": {"type": "string"},
            "description": "set_names：要新增的别名或昵称。",
            "required": False,
            "default": [],
        },
        "retire_names": {
            "type": "array",
            "items": {"type": "string"},
            "description": "set_names：不再使用的称呼。",
            "required": False,
            "default": [],
        },
        "expected_revision": {
            "type": "integer",
            "description": (
                "可选：person_query 返回的 revision。填了可以避免盖掉别人刚做的修改，"
                "资料已被改过时会返回 revision_conflict，重新查一次再改。"
            ),
            "required": False,
        },
        "claim_id": {
            "type": "string",
            "description": "correct_claim / retire_claim：那条声明的 ID（person_query 的 claims 里有）。",
            "required": False,
            "default": "",
        },
        "change_id": {
            "type": "string",
            "description": "confirm_change：待确认变更的 ID（person_query 的 changes 里有）。",
            "required": False,
            "default": "",
        },
        "field": {
            "type": "string",
            "description": "propose_claim：属性名，用简短中文，如 生日、职业、所在城市；不能是账号或权限类字段。",
            "required": False,
        },
        "value": {
            "type": "string",
            "description": "propose_claim：属性值；correct_claim：更正后的值。",
            "required": False,
        },
        "source": {
            "type": "string",
            "choices": ["self_reported", "third_party", "inferred"],
            "description": (
                "propose_claim：信息从哪来。self_reported 本人说的；third_party 别人说的；"
                "inferred 你自己推断的。"
            ),
            "required": False,
            "default": "self_reported",
        },
        "name": {
            "type": "string",
            "description": "create_character：角色名称。",
            "required": False,
        },
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
