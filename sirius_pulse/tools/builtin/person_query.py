"""Read-only, scoped person queries with structured candidates and field-level budgets."""

from __future__ import annotations

import json
from typing import Any

from sirius_pulse.people import PersonError

_SECTIONS = {
    "names",
    "claims",
    "relationships",
    "memories",
    "visual",
    "changes",
    "consents",
    "jobs",
}
TOOL_META = {
    "name": "person_query",
    "description": "查询当前可见人物资料。重名返回最多5个候选及受控游标；不返回原始聊天、账号列表或文件路径。",
    "version": "1.1.0",
    "side_effect": "read_only",
    "tags": ["person", "profile", "identity"],
    "parameters": {
        "target": {
            "type": "object",
            "description": "requester/persona/person_id/name/mention_ref，value 为对应值。",
            "required": True,
        },
        "sections": {
            "type": "array",
            "items": {"type": "string", "enum": sorted(_SECTIONS)},
            "required": False,
            "default": ["names", "visual"],
        },
        "detail": {
            "type": "string",
            "choices": ["summary", "detail"],
            "required": False,
            "default": "summary",
        },
        "query": {"type": "string", "required": False, "default": ""},
        "limit": {"type": "integer", "required": False, "default": 5},
        "cursor": {"type": "string", "required": False, "default": ""},
    },
}


def _bounded(value: Any, budget: int) -> tuple[Any, bool]:
    """Trim fields, never JSON text; bounded strings retain valid result structure."""
    if isinstance(value, str):
        trimmed = len(value) > budget
        return value[: max(0, budget - 1)] + ("…" if trimmed and budget else ""), trimmed
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            result[key], trimmed = _bounded(item, budget)
            if trimmed:
                result["_truncated"] = True
        return result, len(json.dumps(result, ensure_ascii=False)) > budget
    if isinstance(value, list):
        result = []
        for item in value[:5]:
            encoded, _ = _bounded(item, budget)
            if len(json.dumps(result + [encoded], ensure_ascii=False)) > budget:
                break
            result.append(encoded)
        return result, len(result) < len(value)
    return value, False


async def run(
    target: dict[str, Any],
    sections: list[str] | None = None,
    detail: str = "summary",
    query: str = "",
    limit: int = 5,
    cursor: str = "",
    engine_context: Any = None,
    invocation_context: Any = None,
    **kwargs: Any,
) -> dict[str, Any]:
    del kwargs
    try:
        requested = sections if sections is not None else ["names", "visual"]
        if (
            not isinstance(requested, list)
            or not requested
            or not all(isinstance(s, str) for s in requested)
        ):
            raise PersonError("invalid_sections")
        if set(requested) - _SECTIONS:
            raise PersonError("invalid_sections")
        if detail not in {"summary", "detail"}:
            raise PersonError("invalid_detail")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 5:
            raise PersonError("invalid_argument")
        if not isinstance(query, str) or not isinstance(cursor, str):
            raise PersonError("invalid_argument")
        service = engine_context.get_person_service()
        access = engine_context.get_person_access(invocation_context)
        if isinstance(target, dict) and target.get("ref") == "name":
            result = service.name_page(
                str(target.get("value") or ""), access, limit=limit, cursor=cursor
            )
            if result["status"] != "found" or cursor:
                return result
            # A single result is resolved below so its requested sections are present.
        person_id = service.resolve_target(target, access)
        person = service.get_person(person_id, access)
        output: dict[str, Any] = {
            "status": "found",
            "person_id": person_id,
            "kind": person["kind"],
            "display_name": person["display_name"],
            "revision": person["revision"],
            "sections": {},
            "missing_sections": [],
        }
        if "names" in requested:
            output["sections"]["names"] = person["names"]
        if "claims" in requested:
            output["sections"]["claims"] = person["claims"]
        if "relationships" in requested:
            output["sections"]["relationships"] = person["relationships"]
        if "memories" in requested:
            output["sections"]["memories"] = service.memory_summary(person_id, access, query)
        visual_summary = None
        visual = (
            engine_context.get_visual_service()
            if set(requested) & {"visual", "consents", "jobs"}
            else None
        )
        if visual is not None:
            visual_summary = await visual.get_summary(person_id, access, detail=detail)
            if set(requested) & {"consents", "jobs"}:
                if visual_summary.get("consents") is None or visual_summary.get("jobs") is None:
                    raise PersonError("permission_denied")
        for section in _SECTIONS - set(requested):
            if section != "changes":
                output["missing_sections"].append(section)
        if "visual" in requested:
            output["sections"]["visual"] = (
                visual_summary.get("visual") if visual_summary else {"state": "unavailable"}
            )
            if visual_summary:
                for section in ("active", "candidates", "drafts", "consents", "jobs"):
                    output["missing_sections"].extend(
                        item
                        for item in visual_summary.get("missing_sections", [])
                        if item == section
                    )
        for section in ("consents", "jobs"):
            if section in requested:
                output["sections"][section] = (
                    visual_summary.get(section, []) if visual_summary else []
                )
        if "changes" in requested:
            output["sections"]["changes"] = service.list_changes(person_id, access)
        output["missing_sections"] = sorted(set(output["missing_sections"]))
        budget = 1500 if detail == "summary" else 6000
        encoded, _ = _bounded(output, budget)
        if len(json.dumps(encoded, ensure_ascii=False)) > budget:
            return {"status": "error", "error": "result_budget_exceeded", "hint": "减少 sections 后重试"}
        return encoded
    except Exception as exc:
        code = getattr(exc, "code", "person_query_failed")
        return {
            "status": "not_found_or_not_visible" if code == "not_found_or_not_visible" else "error",
            "error": code,
        }
