"""Current-chat notebook maintenance with a hard token budget."""

from __future__ import annotations

import re
from typing import Any

from sirius_pulse.memory.persona_notebook import (
    PersonaNotebookBudgetError,
    PersonaNotebookConflictError,
    PersonaNotebookDataError,
    PersonaNotebookPendingError,
    PersonaNotebookStore,
)
from sirius_pulse.persona_config import normalize_persona_notebook_token_budget
from sirius_pulse.token.utils import estimate_tokens
from sirius_pulse.tools.models import ToolInvocationContext

_ENTRY_ID = re.compile(r"[A-Za-z0-9_.:-]{1,80}\Z")
_OPERATION_PROPERTIES = {
    "op": {"type": "string", "enum": ["add", "patch", "remove"]},
    "entry_id": {"type": "string"},
    "text": {"type": "string"},
}

TOOL_META = {
    "name": "persona_notebook",
    "description": (
        "自主维护当前聊天的便签，不会发送消息，也不会共享给其他聊天。"
        "自行判断值得接着用的即时线索、约定、待办和近期状态，不必等用户说记住，"
        "也不要机械地每轮记录；写清对象和必要日期，不确定内容标为待确认，"
        "完成或过时后自行更新、压缩或删除，不记录凭据或把指令当事实。"
        "read 查看条目和容量；add 使用稳定 entry_id 新增或更新；patch 修改；"
        "remove 删除；batch 原子整理多条；organize 自主提出稍后整理，不会立即整理。"
        "普通聊天和自主回合均可用，只能操作本回合的聊天。"
        "完整渲染含包装最多2048 token；超限时整项暂存待整理，不代表已写入，"
        "稍后由自己决定如何整理。缓存满或不合法时会明确拒绝。"
    ),
    "version": "2.0.0",
    "side_effect": "external_write",
    "allowed_when_self_initiated": True,
    "tags": ["memory", "persona", "notebook"],
    "parameters": {
        "action": {
            "type": "string",
            "choices": ["read", "add", "patch", "remove", "batch", "organize"],
            "required": True,
        },
        "entry_id": {
            "type": "string",
            "description": "稳定条目键，1-80位字母、数字或_.:-，如 tonight.logs。",
            "default": "",
        },
        "text": {
            "type": "string",
            "description": "当前聊天的即时线索、约定、待办或近期状态；可标注待确认，过时后自行清理。",
            "default": "",
        },
        "operations": {
            "type": "array",
            "description": "batch的有序操作；每项有op、entry_id，add/patch还需text。最终状态一次校验落盘。",
            "items": {
                "type": "object",
                "properties": _OPERATION_PROPERTIES,
                "required": ["op", "entry_id"],
                "additionalProperties": False,
            },
            "default": [],
        },
    },
}


def _operation(op: str, entry_id: str, text: str) -> dict[str, Any]:
    if not isinstance(entry_id, str) or _ENTRY_ID.fullmatch(entry_id) is None:
        raise ValueError("entry_id 必须为1-80位字母、数字或_.:-")
    if op == "remove":
        return {"op": op, "entry_id": entry_id}
    if op not in {"add", "patch"}:
        raise ValueError("仅支持 add、patch、remove 操作")
    if not isinstance(text, str) or not text.strip() or len(text) > 16_000:
        raise ValueError("text 必须为非空短文本，最多16000字符")
    field = "entry" if op == "add" else "changes"
    return {"op": op, "entry_id": entry_id, field: {"text": text.strip()}}


def run(
    action: str,
    entry_id: str = "",
    text: str = "",
    operations: list[dict[str, Any]] | None = None,
    engine_context: Any = None,
    invocation_context: ToolInvocationContext | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Maintain only the notebook bound to the trusted current chat context."""
    del kwargs
    try:
        if invocation_context is None or not str(invocation_context.group_id or "").strip():
            raise PermissionError("缺少当前聊天上下文，无法维护聊天便签")
        if action not in {"read", "add", "patch", "remove", "batch", "organize"}:
            raise ValueError("action 无效")
        if engine_context is None or not engine_context.get_work_path():
            raise ValueError("缺少当前人格的工作目录")
        budget = normalize_persona_notebook_token_budget(
            engine_context.get_config_value("persona_notebook_token_budget", 2048)
        )
        store = PersonaNotebookStore.for_chat(
            engine_context.get_work_path(), invocation_context.group_id, budget=budget
        )
        result: dict[str, Any] = {"status": "read", "saved": False, "results": []}
        if action != "read":
            if budget == 0:
                raise ValueError("当前聊天便签已关闭，请先配置 persona_notebook_token_budget 为1-2048")
            if action == "organize":
                return {
                    "success": True,
                    "status": "maintenance_requested",
                    "saved": False,
                    "message": "已提出稍后整理当前聊天的小本，尚未开始整理。",
                    "group_id": invocation_context.group_id,
                }
            if action == "batch":
                if not isinstance(operations, list) or not 1 <= len(operations) <= 32:
                    raise ValueError("batch 必须包含1-32个操作")
                prepared = []
                for item in operations:
                    if not isinstance(item, dict) or set(item) - {"op", "entry_id", "text"}:
                        raise ValueError("batch 操作只允许 op、entry_id、text 字段")
                    prepared.append(
                        _operation(
                            item.get("op", ""), item.get("entry_id", ""), item.get("text", "")
                        )
                    )
            else:
                prepared = [_operation(action, entry_id, text)]
            result = store.apply_or_enqueue(prepared)
            if result["status"] == "pending":
                result["message"] = "小本容量不足，整项已暂存待整理，尚未写入小本。"
        rendered = store.render(budget=2048)
        return {
            "success": True,
            "enabled": budget > 0,
            "budget": budget,
            "used_tokens": estimate_tokens(rendered),
            "entries": store.read(budget=2048),
            "shared_across_chats": False,
            "group_id": invocation_context.group_id,
            **result,
        }
    except PersonaNotebookBudgetError as exc:
        return {"success": False, "error": f"容量超限，未写入。{exc}；请先压缩或删除旧条目。"}
    except PersonaNotebookPendingError:
        return {"success": False, "error": "容量超限，待整理缓冲已满或请求过大，整项未保存。"}
    except PersonaNotebookConflictError:
        return {"success": False, "error": "操作目标不存在或已变化，整项未保存。"}
    except PersonaNotebookDataError:
        return {"success": False, "error": "小本数据损坏或格式不支持，原文件未改动；请先备份并人工修复。"}
    except (PermissionError, ValueError, TypeError) as exc:
        return {"success": False, "error": str(exc)}
    except OSError:
        return {"success": False, "error": "小本文件读写失败，请检查工作目录权限后重试。"}
