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
        "【适用场景】维护当前会话的短期即时便签、约定、待办与临时状态，不向聊天发送消息。\n"
        "【使用方法】当前便签已自动放在上下文的 <persona_notebook> 中（每行为 [条目ID] 正文；"
        "没有这个块就说明还是空的），无需读取，直接操作：add 写入/更新条目（需稳定 entry_id 与 text）；"
        "patch 修改局部内容；remove 删除条目；batch 批量原子操作；organize 登记整理提议。\n"
        "【注意事项】仅作用于当前会话，不跨群或跨聊天共享；受 2048 tokens 容量硬上限限制；严禁记录密码或凭据等敏感信息。"
    ),
    "version": "2.0.0",
    "side_effect": "external_write",
    "allowed_when_self_initiated": True,
    "tags": ["memory", "persona", "notebook"],
    "parameters": {
        "action": {
            "type": "string",
            "choices": ["add", "patch", "remove", "batch", "organize"],
            "required": True,
        },
        "entry_id": {
            "type": "string",
            "description": "稳定条目键（1-80 位字母、数字或 _.:-）。",
            "default": "",
        },
        "text": {
            "type": "string",
            "description": "便签正文，记录当前会话的即时线索、约定或待办状态。",
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
        if action == "read":
            # 便签每轮都自动注入上下文，读取只会白占一个工具轮。
            raise ValueError("便签已在上下文的 <persona_notebook> 中，无需读取；请直接 add/patch/remove")
        if action not in {"add", "patch", "remove", "batch", "organize"}:
            raise ValueError("action 无效")
        if engine_context is None or not engine_context.get_work_path():
            raise ValueError("缺少当前人格的工作目录")
        budget = normalize_persona_notebook_token_budget(
            engine_context.get_config_value("persona_notebook_token_budget", 2048)
        )
        store = PersonaNotebookStore.for_chat(
            engine_context.get_work_path(), invocation_context.group_id, budget=budget
        )
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
                    _operation(item.get("op", ""), item.get("entry_id", ""), item.get("text", ""))
                )
        else:
            prepared = [_operation(action, entry_id, text)]
        result: dict[str, Any] = store.apply_or_enqueue(prepared)
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
