"""Read-only, scoped person queries with structured candidates and field-level budgets."""

from __future__ import annotations

import json
from typing import Any

from sirius_pulse.people import PersonError
from sirius_pulse.tools.engine_context import PeopleAccess

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
    "description": (
        "【适用场景】查询当前会话可见范围内的人物资料、画像设定、别名称谓及视觉信息。\n"
        "【使用方法】target 指定查询主体（支持 requester/persona/person_id/name/mention_ref 等键值对）；"
        "sections 筛选信息模块；detail 指定 summary 或 detail 级别。\n"
        "【注意事项】只读工具；同名主体最多返回 5 个候选对象；不暴露底层账号、原始聊天或内部文件路径。"
    ),
    "version": "1.1.0",
    "side_effect": "read_only",
    "redact_logs": True,
    "engagement_exempt": True,
    "request_source": True,
    "tags": ["person", "profile", "identity"],
    "parameters": {
        "target": {
            "type": "object",
            "description": (
                "查谁（JSON 对象）：{'ref': 'requester'} 本回合向你提要求的人；"
                "{'ref': 'persona'} 你自己；{'ref': 'person_id', 'value': '<UUID>'}；"
                "{'ref': 'name', 'value': '<称呼>'}（重名时返回候选，再用 person_id 查）；"
                "{'ref': 'mention_ref', 'value': '<某条消息的 evidence_ref>'} 那条消息的发送者。"
            ),
            "required": True,
        },
        "sections": {
            "type": "array",
            "items": {"type": "string", "enum": sorted(_SECTIONS)},
            "description": (
                "要哪些信息，默认 names + visual。names 称呼与别名；claims 属性声明"
                "（如生日、职业，含 claim_id）；relationships 与他人的关系；memories 与此人有关的"
                "记忆摘要（可配合 query）；visual 当前形象；changes 待确认的资料变更（含 change_id）；"
                "consents / jobs 视觉授权与生图任务（需要相应权限）。只要用到的，别全选。"
            ),
            "required": False,
            "default": ["names", "visual"],
        },
        "detail": {
            "type": "string",
            "choices": ["summary", "detail"],
            "description": "summary 精简（约 1500 字以内）；detail 更完整（约 6000 字以内），确实需要细节时再用。",
            "required": False,
            "default": "summary",
        },
        "query": {
            "type": "string",
            "description": "只对 sections 里的 memories 起作用：只看与这个关键词有关的记忆。",
            "required": False,
            "default": "",
        },
        "limit": {
            "type": "integer",
            "description": "按名字查（ref=name）时最多返回几个候选，1 到 5。",
            "required": False,
            "default": 5,
        },
        "cursor": {
            "type": "string",
            "description": "按名字查的翻页：把上次结果里的 next_cursor 原样传回；其他情况留空。",
            "required": False,
            "default": "",
        },
    },
}


#: 按明确标识查人：不需要知道是谁提的要求。
_SOURCE_FREE_REFS = frozenset({"person_id", "name", "persona"})


def request_source_optional(params: dict[str, Any]) -> bool:
    """按明确的人物 ID / 名字 / 自己查资料时，可以不选指令来源。

    框架仍保留调用者用于可见范围判断，但不会因此授予任何写权限；``requester``
    这类要靠来源才能确定是谁的查询不在此列。
    """
    target = params.get("target")
    return isinstance(target, dict) and str(target.get("ref") or "") in _SOURCE_FREE_REFS


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
    engine_context: PeopleAccess | None = None,
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
