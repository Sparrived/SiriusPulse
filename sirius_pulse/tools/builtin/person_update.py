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
    "description": ("有人在聊天里让你改称呼、记下或更正关于某人的信息（生日、职业等）、" "确认一条待确认的变更，或登记一个原创角色时用。每次修改都要附上提出它的那条消息。"),
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
                "set_names 改称呼/别名；propose_claim 记一条信息；correct_claim 更正；"
                "retire_claim 撤下；confirm_change 确认待确认的变更；create_character 登记原创角色。"
            ),
            "required": True,
        },
        "target": {
            "type": "object",
            "description": ("改谁，写法同 person_query；requester 指 evidence_ref 那条消息的作者。"),
            "required": True,
        },
        "evidence_ref": {
            "type": "string",
            "description": ("提出这次修改的那条消息的 evidence_ref（每条聊天消息都带着），原样复制。"),
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
            "description": ("可选：person_query 返回的 revision，防止盖掉别人刚做的修改。"),
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
            "description": "propose_claim：属性名，简短中文，如 生日、职业、所在城市。",
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
            "description": ("propose_claim：self_reported 本人说的；third_party 别人说的；inferred 你推断的。"),
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
