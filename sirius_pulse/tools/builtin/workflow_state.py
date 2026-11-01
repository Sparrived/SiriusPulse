"""Reliable, chat-scoped checkpoints for multi-turn workflows."""

from __future__ import annotations

import copy
import json
import re
import time
import uuid
from typing import Any

from sirius_pulse.config.config_builder import ConfigBuilder
from sirius_pulse.tools.models import ToolInvocationContext

_SCHEMA_VERSION = 2
_MAX_KEY_LENGTH = 80
_MAX_VERSION_LENGTH = 64
_MAX_STEP_LENGTH = 80
_MAX_IDEMPOTENCY_LENGTH = 160
_MAX_CLAIM_TOKEN_LENGTH = 80
_MAX_STATE_CHARS = 8_000
_MAX_SUMMARY_CHARS = 2_000
_MAX_ERROR_CHARS = 2_000
_MAX_STEPS = 64
_MAX_HISTORY = 8
_MAX_LIST_ITEMS = 100
_MAX_MODEL_PAYLOAD_CHARS = 2_000
_MIN_LEASE_SECONDS = 30
_MAX_LEASE_SECONDS = 3_600
_KEY_RE = re.compile(r"^[^/\\]+$")
_SENSITIVE_PARTS = {
    "apikey",
    "authorization",
    "accesstoken",
    "clientsecret",
    "cookie",
    "credential",
    "password",
    "privatekey",
    "secret",
    "token",
}

_config = ConfigBuilder()
_config.group("流程状态").add(
    "action",
    type="str",
    description=(
        "固定流程：先 list 检查目录，再 resume；found=false 时 begin 并登记；然后 claim；"
        "只有 claimed=true 才调用专用工具；"
        "专用工具成功用 checkpoint，失败用 fail；checkpoint 的 next_step 为空时自动完成。"
        "list/resume 只读，restart 开新一轮。"
    ),
    required=True,
    choices=[
        "list",
        "resume",
        "begin",
        "claim",
        "checkpoint",
        "fail",
        "restart",
    ],
)
_config.group("流程状态").add(
    "key",
    type="str",
    description="同一聊天内一项具体业务的稳定键；list 可留空列出全部流程，例如 napcat.like:123456。",
    default="",
)
_config.group("流程状态").add(
    "version",
    type="str",
    description="流程参数和步骤契约版本；通常填 1，只有契约改变时才递增。",
    default="1",
)
_config.group("流程步骤").add(
    "step",
    type="str",
    description="当前要执行的稳定步骤；resume/checkpoint 返回的 next_step 非空时必须原样使用。",
    default="",
)
_config.group("流程步骤").add(
    "tool_name",
    type="str",
    description="claim 时填写实际要调用的专用 Tool 名称，例如 qq_like；checkpoint 时原样带回。",
    default="",
)
_config.group("流程步骤").add(
    "idempotency_key",
    type="str",
    description="claim/checkpoint/fail 使用同一个稳定幂等键；由业务事实组成，不能每次随机生成。",
    default="",
)
_config.group("流程步骤").add(
    "claim_token",
    type="str",
    description="仅从 claim 返回值复制；checkpoint/fail 必须原样带回，不能猜测或重新生成。",
    default="",
)
_config.group("流程步骤").add(
    "next_step",
    type="str",
    description="只在 checkpoint 时填写下一步；没有下一步必须留空，工具会自动完成流程。",
    default="",
)
_config.group("流程步骤").add(
    "lease_seconds",
    type="int",
    description="claim 租约秒数；模型或进程中断后超过租约才允许接管，范围 30-3600。",
    default=300,
)
_config.group("并发保护").add(
    "expected_revision",
    type="int",
    description="复制最近一次 workflow_state 返回的顶层 revision；所有写操作都必须填写。",
    default=-1,
)
_config.group("流程数据").add(
    "state_json",
    type="str",
    description=("JSON 字符串而不是嵌套对象；begin/claim 放目标事实，checkpoint 放外部 ID 或结果摘要，" "只保存下次复用需要的最小数据。"),
    default="{}",
)
_config.group("流程数据").add(
    "summary",
    type="str",
    description="步骤或流程的短摘要，不要填完整日志。",
    default="",
)
_config.group("流程数据").add(
    "error",
    type="str",
    description="fail 操作的可恢复错误摘要，不要填密钥或完整堆栈。",
    default="",
)

TOOL_META = {
    "name": "workflow_state",
    "description": (
        "【适用场景】多步骤外部操作的状态持久化与幂等执行，防止重复执行外部副作用。\n"
        "【使用方法】按生命周期顺序调用：① list 查询当前会话流程；② resume 恢复流程；③ 未命中时 begin 登记新流程；"
        "④ claim 获取步骤租约；⑤ claimed=true 后执行目标专用工具；⑥ 成功调用 checkpoint（next_step 为空时自动完成），失败调用 fail。\n"
        "【注意事项】严格遵循状态机约束：already_done=true 必须跳过重复执行；in_progress=true 不得并发抢占；"
        "必须原样传递 revision、claim_token 与幂等键，严禁绕过流程直接执行副作用。"
    ),
    "version": "2.2.0",
    "retry_safe": False,
    "side_effect": "external_write",
    "tags": ["workflow", "state", "reuse", "idempotency"],
    "parameters": _config.build(),
}


def run(
    action: str,
    key: str = "",
    version: str = "1",
    step: str = "",
    tool_name: str = "",
    idempotency_key: str = "",
    claim_token: str = "",
    next_step: str = "",
    lease_seconds: int = 300,
    expected_revision: int = -1,
    state_json: str = "{}",
    summary: str = "",
    error: str = "",
    data_store: Any = None,
    chat_context: dict[str, Any] | None = None,
    invocation_context: ToolInvocationContext | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Run one state transition while holding the store transaction lock."""
    if data_store is None:
        return copy.deepcopy(
            _enrich_result(
                _run_locked(
                    action,
                    key,
                    version,
                    step,
                    tool_name,
                    idempotency_key,
                    claim_token,
                    next_step,
                    lease_seconds,
                    expected_revision,
                    state_json,
                    summary,
                    error,
                    data_store,
                    chat_context,
                    invocation_context,
                    **kwargs,
                )
            )
        )
    transaction = getattr(data_store, "transaction", None)
    if not callable(transaction):
        result = _run_locked(
            action,
            key,
            version,
            step,
            tool_name,
            idempotency_key,
            claim_token,
            next_step,
            lease_seconds,
            expected_revision,
            state_json,
            summary,
            error,
            data_store,
            chat_context,
            invocation_context,
            **kwargs,
        )
        save = getattr(data_store, "save", None)
        if callable(save):
            save()
        return copy.deepcopy(_enrich_result(result))
    with transaction():
        result = _run_locked(
            action,
            key,
            version,
            step,
            tool_name,
            idempotency_key,
            claim_token,
            next_step,
            lease_seconds,
            expected_revision,
            state_json,
            summary,
            error,
            data_store,
            chat_context,
            invocation_context,
            **kwargs,
        )
        data_store.save()
        return copy.deepcopy(_enrich_result(result))


def _run_locked(
    action: str,
    key: str,
    version: str = "1",
    step: str = "",
    tool_name: str = "",
    idempotency_key: str = "",
    claim_token: str = "",
    next_step: str = "",
    lease_seconds: int = 300,
    expected_revision: int = -1,
    state_json: str = "{}",
    summary: str = "",
    error: str = "",
    data_store: Any = None,
    chat_context: dict[str, Any] | None = None,
    invocation_context: ToolInvocationContext | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Advance or inspect one workflow in the current chat scope."""
    if data_store is None:
        return {"success": False, "error": "流程状态存储未初始化"}

    action_key = str(action or "").strip().lower()
    state_key = str(key or "").strip()
    version_key = str(version or "1").strip()
    step_key = str(step or "").strip()
    tool_key = str(tool_name or "").strip()
    idem_key = str(idempotency_key or "").strip()
    claim_token_key = str(claim_token or "").strip()
    next_step_key = str(next_step or "").strip()

    if action_key not in {"list", "resume", "begin", "claim", "checkpoint", "fail", "restart"}:
        return {"success": False, "error": f"不支持的流程状态 action: {action}"}
    key_error = None
    if action_key != "list" or state_key:
        key_error = _validate_name(state_key, "key", _MAX_KEY_LENGTH)
    if key_error:
        return {"success": False, "error": key_error}
    version_error = _validate_name(version_key, "version", _MAX_VERSION_LENGTH)
    if version_error:
        return {"success": False, "error": version_error}

    scope = _scope_key(chat_context, invocation_context)
    if action_key == "list":
        return _list_result(data_store, scope, state_key)

    storage_key = _storage_key(scope, state_key)
    record = _load_record(data_store.get(storage_key))

    if action_key == "resume":
        if record is None:
            return {
                "success": True,
                "scope": scope,
                "key": state_key,
                "found": False,
                "resumable": False,
                "next_step": "",
            }
        if record.get("version") != version_key:
            return _conflict(
                scope,
                state_key,
                "流程版本不匹配，不能安全复用；请使用新 key 或更新 version。",
                record,
                expected_version=version_key,
            )
        return _resume_result(scope, state_key, record)

    if action_key == "begin":
        return _begin(
            data_store,
            storage_key,
            scope,
            state_key,
            version_key,
            state_json,
            summary,
            record,
        )

    if action_key == "restart":
        return _restart(
            data_store,
            storage_key,
            scope,
            state_key,
            version_key,
            state_json,
            summary,
            record,
            expected_revision,
        )

    if record is None:
        return {
            "success": False,
            "scope": scope,
            "key": state_key,
            "error": "流程不存在；请先使用 begin 创建流程。",
        }
    if record.get("version") != version_key:
        return _conflict(
            scope,
            state_key,
            "流程版本不匹配，拒绝覆盖已有状态。",
            record,
            expected_version=version_key,
        )

    if action_key in {"claim", "fail"} and record.get("status") == "completed":
        return {
            "success": False,
            "scope": scope,
            "key": state_key,
            "error": "流程已经 completed；需要再次执行时先 restart，不能直接 claim 或写入。",
            "workflow": record,
        }

    revision_error = _check_revision(record, expected_revision)
    if revision_error:
        return _conflict(
            scope,
            state_key,
            revision_error,
            record,
            expected_revision=expected_revision,
        )

    if action_key in {"claim", "checkpoint", "fail"} and int(expected_revision) < 0:
        return {
            "success": False,
            "scope": scope,
            "key": state_key,
            "error": "写操作必须提供最近一次 resume/claim 返回的 expected_revision。",
            "workflow": record,
        }

    if action_key in {"claim", "checkpoint"}:
        step_error = _validate_name(step_key, "step", _MAX_STEP_LENGTH)
        if step_error:
            return {"success": False, "scope": scope, "key": state_key, "error": step_error}
        if action_key == "claim":
            if not idem_key:
                return {
                    "success": False,
                    "scope": scope,
                    "key": state_key,
                    "error": "claim 必须提供稳定的 idempotency_key。",
                }
            idem_error = _validate_name(idem_key, "idempotency_key", _MAX_IDEMPOTENCY_LENGTH)
            if idem_error:
                return {"success": False, "scope": scope, "key": state_key, "error": idem_error}
            lease_error = _validate_lease(lease_seconds)
            if lease_error:
                return {"success": False, "scope": scope, "key": state_key, "error": lease_error}
            tool_error = _validate_name(tool_key, "tool_name", _MAX_KEY_LENGTH)
            if tool_error:
                return {"success": False, "scope": scope, "key": state_key, "error": tool_error}
            expected_step = str(record.get("current_step") or "").strip()
            if expected_step and step_key != expected_step:
                return {
                    "success": False,
                    "scope": scope,
                    "key": state_key,
                    "conflict": True,
                    "error": f"当前应执行步骤 {expected_step}，不能先 claim {step_key}。",
                    "workflow": record,
                }
        if action_key == "checkpoint" and not idem_key:
            return {
                "success": False,
                "scope": scope,
                "key": state_key,
                "error": "checkpoint 必须复用 claim 时的 idempotency_key。",
            }
        if action_key == "checkpoint" and not claim_token_key:
            return {
                "success": False,
                "scope": scope,
                "key": state_key,
                "error": "checkpoint 必须带回 claim 返回的 claim_token。",
            }
        if action_key == "checkpoint" and idem_key:
            idem_error = _validate_name(idem_key, "idempotency_key", _MAX_IDEMPOTENCY_LENGTH)
            if idem_error:
                return {"success": False, "scope": scope, "key": state_key, "error": idem_error}
        if action_key == "checkpoint":
            token_error = _validate_name(claim_token_key, "claim_token", _MAX_CLAIM_TOKEN_LENGTH)
            if token_error:
                return {"success": False, "scope": scope, "key": state_key, "error": token_error}
            if next_step_key:
                next_step_error = _validate_name(next_step_key, "next_step", _MAX_STEP_LENGTH)
                if next_step_error:
                    return {
                        "success": False,
                        "scope": scope,
                        "key": state_key,
                        "error": next_step_error,
                    }

        state, state_error = _parse_state(state_json)
        if state_error:
            return {"success": False, "scope": scope, "key": state_key, "error": state_error}
        if action_key == "claim":
            return _claim(
                data_store,
                storage_key,
                scope,
                state_key,
                record,
                step_key,
                tool_key,
                idem_key,
                state,
                int(lease_seconds),
            )
        return _checkpoint(
            data_store,
            storage_key,
            scope,
            state_key,
            record,
            step_key,
            tool_key,
            idem_key,
            next_step_key,
            state,
            claim_token_key,
            summary,
        )

    if action_key == "fail":
        step_error = _validate_name(step_key, "step", _MAX_STEP_LENGTH)
        if step_error:
            return {"success": False, "scope": scope, "key": state_key, "error": step_error}
        if not idem_key:
            return {
                "success": False,
                "scope": scope,
                "key": state_key,
                "error": "fail 必须复用 claim 时的 idempotency_key。",
            }
        if not claim_token_key:
            return {
                "success": False,
                "scope": scope,
                "key": state_key,
                "error": "fail 必须带回 claim 返回的 claim_token。",
            }
        idem_error = _validate_name(idem_key, "idempotency_key", _MAX_IDEMPOTENCY_LENGTH)
        if idem_error:
            return {"success": False, "scope": scope, "key": state_key, "error": idem_error}
        token_error = _validate_name(claim_token_key, "claim_token", _MAX_CLAIM_TOKEN_LENGTH)
        if token_error:
            return {"success": False, "scope": scope, "key": state_key, "error": token_error}
        return _fail(
            data_store,
            storage_key,
            scope,
            state_key,
            record,
            step_key,
            idem_key,
            claim_token_key,
            error,
            summary,
        )

    return {"success": False, "scope": scope, "key": state_key, "error": "未处理的流程 action"}


def _enrich_result(result: dict[str, Any]) -> dict[str, Any]:
    """Expose a bounded workflow view instead of the storage record."""
    workflow = result.get("workflow")
    if not isinstance(workflow, dict):
        return result
    workflow_view = _model_workflow(workflow)
    summary = {
        "workflow_id": workflow_view.get("workflow_id", ""),
        "version": workflow_view.get("version", ""),
        "revision": workflow_view.get("revision", 0),
        "status": workflow_view.get("status", ""),
        "next_step": workflow_view.get("current_step", ""),
        "last_success": workflow_view.get("last_success"),
        "last_error": workflow_view.get("last_error"),
    }
    enriched: dict[str, Any] = {}
    for key, value in result.items():
        if key == "workflow":
            enriched.update(summary)
            enriched[key] = workflow_view
        else:
            enriched[key] = value
    return enriched


def _load_record(value: Any) -> dict[str, Any] | None:
    """Normalize current and previously persisted workflow records."""
    if not isinstance(value, dict) or not value.get("workflow_id") or not value.get("key"):
        return None
    try:
        schema_version = int(value.get("schema_version", 1))
    except (TypeError, ValueError):
        return None
    if schema_version not in {1, _SCHEMA_VERSION}:
        return None

    record = copy.deepcopy(value)
    record["schema_version"] = _SCHEMA_VERSION
    record.setdefault("version", "1")
    record.setdefault("run_number", 1)
    record.setdefault("previous_workflow_id", "")
    record.setdefault("history", [])
    record.setdefault("revision", 0)
    record.setdefault("status", "active")
    record.setdefault("inputs", {})
    record.setdefault("summary", "")
    record.setdefault("current_step", "")
    record.setdefault("steps", {})
    record.setdefault("last_success", None)
    record.setdefault("last_error", None)
    if not isinstance(record["history"], list):
        record["history"] = []
    record["history"] = [
        _sanitize(item) for item in record["history"][-_MAX_HISTORY:] if isinstance(item, dict)
    ]
    if not isinstance(record["inputs"], dict):
        record["inputs"] = {}
    record["inputs"] = _sanitize(record["inputs"])
    if not isinstance(record["steps"], dict):
        record["steps"] = {}
    for item in record["steps"].values():
        if not isinstance(item, dict):
            continue
        item["input"] = _sanitize(item.get("input", {}))
        item["result"] = _sanitize(item.get("result", {}))
    if isinstance(record.get("last_success"), dict):
        record["last_success"] = copy.deepcopy(record["last_success"])
        record["last_success"]["result"] = _sanitize(record["last_success"].get("result", {}))
    return record


def _model_workflow(workflow: dict[str, Any]) -> dict[str, Any]:
    """Expose only bounded workflow state and the tokens needed to continue it."""
    steps = {
        str(step): _model_step(item)
        for step, item in workflow.get("steps", {}).items()
        if isinstance(item, dict)
    }
    history = [
        _model_payload(item, _MAX_MODEL_PAYLOAD_CHARS)
        for item in workflow.get("history", [])[-_MAX_HISTORY:]
        if isinstance(item, dict)
    ]
    return {
        "schema_version": workflow.get("schema_version", _SCHEMA_VERSION),
        "workflow_id": workflow.get("workflow_id", ""),
        "key": workflow.get("key", ""),
        "version": workflow.get("version", ""),
        "run_number": workflow.get("run_number", 1),
        "previous_workflow_id": workflow.get("previous_workflow_id", ""),
        "history": history,
        "revision": workflow.get("revision", 0),
        "status": workflow.get("status", ""),
        "inputs": _model_payload(workflow.get("inputs", {}), _MAX_STATE_CHARS),
        "summary": _truncate(workflow.get("summary", ""), _MAX_SUMMARY_CHARS),
        "current_step": workflow.get("current_step", ""),
        "steps": steps,
        "last_success": _compact_success(workflow.get("last_success")),
        "last_error": _compact_error(workflow.get("last_error")),
        "created_at": workflow.get("created_at", 0),
        "updated_at": workflow.get("updated_at", 0),
    }


def _model_step(step: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": step.get("status", ""),
        "tool_name": step.get("tool_name", ""),
        "idempotency_key": step.get("idempotency_key", ""),
        "claim_token": step.get("claim_token", ""),
        "input": _model_payload(step.get("input", {}), _MAX_MODEL_PAYLOAD_CHARS),
        "result": _model_payload(step.get("result", {}), _MAX_MODEL_PAYLOAD_CHARS),
        "summary": _truncate(step.get("summary", ""), _MAX_SUMMARY_CHARS),
        "error": _truncate(step.get("error", ""), _MAX_ERROR_CHARS),
        "claim_count": step.get("claim_count", 0),
        "lease_until": step.get("lease_until", 0),
        "revision": step.get("revision", 0),
        "updated_at": step.get("updated_at", 0),
    }


def _model_payload(value: Any, max_chars: int) -> Any:
    sanitized = _sanitize(value)
    rendered = _json_text(sanitized)
    if len(rendered) <= max_chars:
        return sanitized
    return {"truncated": True, "preview": rendered[: max_chars - 32]}


def _begin(
    data_store: Any,
    storage_key: str,
    scope: str,
    state_key: str,
    version: str,
    state_json: str,
    summary: str,
    record: dict[str, Any] | None,
) -> dict[str, Any]:
    if record is not None:
        if record.get("version") != version:
            return _conflict(
                scope,
                state_key,
                "流程版本不匹配，拒绝复用已有状态。",
                record,
                expected_version=version,
            )
        return {
            "success": True,
            "scope": scope,
            "key": state_key,
            "created": False,
            "reused": True,
            "registered": True,
            "workflow": record,
        }

    state, state_error = _parse_state(state_json)
    if state_error:
        return {"success": False, "scope": scope, "key": state_key, "error": state_error}
    now = _now()
    record = _new_record(
        state_key,
        version,
        state,
        summary,
        run_number=1,
        history=[],
        timestamp=now,
    )
    data_store.set(storage_key, record)
    return {
        "success": True,
        "scope": scope,
        "key": state_key,
        "created": True,
        "reused": False,
        "registered": True,
        "workflow": record,
    }


def _restart(
    data_store: Any,
    storage_key: str,
    scope: str,
    state_key: str,
    version: str,
    state_json: str,
    summary: str,
    record: dict[str, Any] | None,
    expected_revision: int,
) -> dict[str, Any]:
    if record is None:
        return {
            "success": False,
            "scope": scope,
            "key": state_key,
            "error": "流程不存在；请先使用 begin 创建流程。",
        }
    if record.get("version") != version:
        return _conflict(
            scope,
            state_key,
            "流程版本不匹配，拒绝重启已有状态。",
            record,
            expected_version=version,
        )
    if int(expected_revision) < 0:
        return {
            "success": False,
            "scope": scope,
            "key": state_key,
            "error": "restart 必须提供最近一次 resume 返回的 expected_revision。",
            "workflow": record,
        }
    revision_error = _check_revision(record, expected_revision)
    if revision_error:
        return _conflict(
            scope,
            state_key,
            revision_error,
            record,
            expected_revision=expected_revision,
        )
    if record.get("status") not in {"completed", "failed"}:
        return {
            "success": False,
            "scope": scope,
            "key": state_key,
            "error": "只能 restart 已完成或已失败的流程；活动流程请使用 resume。",
            "workflow": record,
        }
    state, state_error = _parse_state(state_json)
    if state_error:
        return {"success": False, "scope": scope, "key": state_key, "error": state_error}
    now = _now()
    history = list(record.get("history", []))
    history.append(_compact_run(record))
    history = history[-_MAX_HISTORY:]
    next_record = _new_record(
        state_key,
        version,
        state or record.get("inputs", {}),
        summary or record.get("summary", ""),
        run_number=int(record.get("run_number", 1)) + 1,
        previous_workflow_id=str(record.get("workflow_id") or ""),
        history=history,
        timestamp=now,
    )
    data_store.set(storage_key, next_record)
    return {
        "success": True,
        "scope": scope,
        "key": state_key,
        "restarted": True,
        "workflow": next_record,
    }


def _new_record(
    state_key: str,
    version: str,
    state: dict[str, Any],
    summary: str,
    *,
    run_number: int,
    previous_workflow_id: str = "",
    history: list[dict[str, Any]],
    timestamp: float,
) -> dict[str, Any]:
    record = {
        "schema_version": _SCHEMA_VERSION,
        "workflow_id": f"wf_{uuid.uuid4().hex[:20]}",
        "key": state_key,
        "version": version,
        "run_number": run_number,
        "previous_workflow_id": previous_workflow_id,
        "history": history[-_MAX_HISTORY:],
        "revision": 0,
        "status": "active",
        "inputs": state,
        "summary": _truncate(summary, _MAX_SUMMARY_CHARS),
        "current_step": "",
        "steps": {},
        "last_success": None,
        "last_error": None,
        "created_at": timestamp,
        "updated_at": timestamp,
    }
    return record


def _compact_run(record: dict[str, Any]) -> dict[str, Any]:
    """Keep restart history useful without copying every result payload."""
    step_summaries = {}
    for step, item in record.get("steps", {}).items():
        if not isinstance(item, dict):
            continue
        step_summaries[step] = {
            "status": item.get("status", ""),
            "tool_name": item.get("tool_name", ""),
            "idempotency_key": item.get("idempotency_key", ""),
            "summary": _truncate(item.get("summary", ""), _MAX_SUMMARY_CHARS),
            "error": _truncate(item.get("error", ""), _MAX_ERROR_CHARS),
            "revision": item.get("revision", 0),
        }
    return {
        "workflow_id": record.get("workflow_id", ""),
        "run_number": record.get("run_number", 1),
        "version": record.get("version", ""),
        "status": record.get("status", ""),
        "revision": record.get("revision", 0),
        "inputs": record.get("inputs", {}),
        "summary": _truncate(record.get("summary", ""), _MAX_SUMMARY_CHARS),
        "last_success": _compact_success(record.get("last_success")),
        "last_error": _compact_error(record.get("last_error")),
        "steps": step_summaries,
    }


def _claim(
    data_store: Any,
    storage_key: str,
    scope: str,
    state_key: str,
    record: dict[str, Any],
    step: str,
    tool_name: str,
    idempotency_key: str,
    state: dict[str, Any],
    lease_seconds: int,
) -> dict[str, Any]:
    now = _now()
    steps = record.setdefault("steps", {})
    existing = steps.get(step)
    if isinstance(existing, dict):
        existing_idem = str(existing.get("idempotency_key") or "")
        existing_status = existing.get("status")
        if existing_idem and existing_idem != idempotency_key:
            return _conflict(
                scope,
                state_key,
                f"步骤 {step} 已被其他 idempotency_key 占用，拒绝重复执行。",
                record,
            )
        if state and existing.get("input", {}) != state:
            return _conflict(
                scope,
                state_key,
                f"步骤 {step} 的输入事实与已有 claim 不一致，拒绝重复执行。",
                record,
            )
        if existing_idem == idempotency_key and existing_status == "succeeded":
            return {
                "success": True,
                "scope": scope,
                "key": state_key,
                "already_done": True,
                "replayed": True,
                "step": step,
                "workflow": record,
                "result": existing.get("result", {}),
            }
        if existing_idem == idempotency_key and existing_status == "claimed":
            lease_until = float(existing.get("lease_until") or 0)
            if lease_until > now:
                return {
                    "success": True,
                    "scope": scope,
                    "key": state_key,
                    "in_progress": True,
                    "claimed": False,
                    "step": step,
                    "lease_until": lease_until,
                    "claim_token": existing.get("claim_token", ""),
                    "workflow": record,
                }

    if len(steps) >= _MAX_STEPS and step not in steps:
        return {
            "success": False,
            "scope": scope,
            "key": state_key,
            "error": f"流程步骤不能超过 {_MAX_STEPS} 个。",
            "workflow": record,
        }

    revision = int(record.get("revision", 0)) + 1
    claim = {
        "status": "claimed",
        "tool_name": tool_name,
        "idempotency_key": idempotency_key,
        "claim_token": f"ct_{uuid.uuid4().hex[:24]}",
        "input": state,
        "result": existing.get("result", {}) if isinstance(existing, dict) else {},
        "summary": existing.get("summary", "") if isinstance(existing, dict) else "",
        "claim_count": int(existing.get("claim_count", 0)) + 1 if isinstance(existing, dict) else 1,
        "claimed_at": now,
        "lease_until": now + lease_seconds,
        "updated_at": now,
        "revision": revision,
    }
    steps[step] = claim
    record["status"] = "active"
    record["current_step"] = step
    record["revision"] = revision
    record["updated_at"] = now
    data_store.set(storage_key, record)
    return {
        "success": True,
        "scope": scope,
        "key": state_key,
        "claimed": True,
        "already_done": False,
        "in_progress": False,
        "step": step,
        "lease_until": claim["lease_until"],
        "claim_token": claim["claim_token"],
        "workflow": record,
    }


def _checkpoint(
    data_store: Any,
    storage_key: str,
    scope: str,
    state_key: str,
    record: dict[str, Any],
    step: str,
    tool_name: str,
    idempotency_key: str,
    next_step: str,
    result: dict[str, Any],
    claim_token: str,
    summary: str,
) -> dict[str, Any]:
    now = _now()
    steps = record.setdefault("steps", {})
    existing = steps.get(step)
    if existing is None:
        return {
            "success": False,
            "scope": scope,
            "key": state_key,
            "error": f"步骤 {step} 尚未 claim，不能 checkpoint。",
            "workflow": record,
        }
    if isinstance(existing, dict):
        existing_idem = str(existing.get("idempotency_key") or "")
        if existing_idem != idempotency_key:
            return _conflict(
                scope,
                state_key,
                f"步骤 {step} 的幂等键不匹配，拒绝覆盖结果。",
                record,
            )
        if existing.get("status") == "succeeded":
            if existing_idem != idempotency_key or existing.get("claim_token") != claim_token:
                return _conflict(
                    scope,
                    state_key,
                    f"步骤 {step} 已完成，幂等键或 claim_token 不匹配。",
                    record,
                )
            return {
                "success": True,
                "scope": scope,
                "key": state_key,
                "checkpointed": True,
                "replayed": True,
                "step": step,
                "workflow": record,
                "result": existing.get("result", {}),
            }
        if existing.get("claim_token") != claim_token:
            return _conflict(
                scope,
                state_key,
                f"步骤 {step} 的 claim_token 已失效，拒绝迟到结果。",
                record,
            )
        if existing.get("status") != "claimed":
            return {
                "success": False,
                "scope": scope,
                "key": state_key,
                "error": f"步骤 {step} 当前不是可 checkpoint 的 claim 状态。",
                "workflow": record,
            }
    elif len(steps) >= _MAX_STEPS:
        return {
            "success": False,
            "scope": scope,
            "key": state_key,
            "error": f"流程步骤不能超过 {_MAX_STEPS} 个。",
            "workflow": record,
        }

    revision = int(record.get("revision", 0)) + 1
    step_record = {
        "status": "succeeded",
        "tool_name": tool_name or (existing or {}).get("tool_name", ""),
        "idempotency_key": idempotency_key or (existing or {}).get("idempotency_key", ""),
        "claim_token": (existing or {}).get("claim_token", ""),
        "input": (existing or {}).get("input", {}),
        "result": result,
        "summary": _truncate(summary, _MAX_SUMMARY_CHARS),
        "claimed_at": (existing or {}).get("claimed_at"),
        "completed_at": now,
        "lease_until": 0,
        "updated_at": now,
        "revision": revision,
    }
    steps[step] = step_record
    record["status"] = "completed" if not next_step else "active"
    record["current_step"] = next_step
    record["revision"] = revision
    record["updated_at"] = now
    record["last_success"] = {
        "step": step,
        "tool_name": step_record["tool_name"],
        "idempotency_key": step_record["idempotency_key"],
        "claim_token": step_record["claim_token"],
        "result": result,
        "summary": step_record["summary"],
        "revision": revision,
        "updated_at": now,
    }
    record["last_error"] = None
    data_store.set(storage_key, record)
    return {
        "success": True,
        "scope": scope,
        "key": state_key,
        "checkpointed": True,
        "replayed": False,
        "completed": not next_step,
        "step": step,
        "workflow": record,
    }


def _fail(
    data_store: Any,
    storage_key: str,
    scope: str,
    state_key: str,
    record: dict[str, Any],
    step: str,
    idempotency_key: str,
    claim_token: str,
    error: str,
    summary: str,
) -> dict[str, Any]:
    now = _now()
    steps = record.setdefault("steps", {})
    existing = steps.get(step)
    if existing is None:
        return {
            "success": False,
            "scope": scope,
            "key": state_key,
            "error": f"步骤 {step} 尚未 claim，不能 fail。",
            "workflow": record,
        }
    if isinstance(existing, dict):
        existing_idem = str(existing.get("idempotency_key") or "")
        if existing_idem != idempotency_key:
            return _conflict(
                scope,
                state_key,
                f"步骤 {step} 的幂等键不匹配，拒绝覆盖状态。",
                record,
            )
        if existing.get("status") == "succeeded":
            return _conflict(
                scope,
                state_key,
                f"步骤 {step} 已经成功 checkpoint，不能再标记 fail。",
                record,
            )
        if existing.get("claim_token") != claim_token:
            return _conflict(
                scope,
                state_key,
                f"步骤 {step} 的 claim_token 已失效，拒绝迟到错误。",
                record,
            )
        if existing.get("status") == "failed":
            return {
                "success": True,
                "scope": scope,
                "key": state_key,
                "failed": True,
                "replayed": True,
                "workflow": record,
            }
    revision = int(record.get("revision", 0)) + 1
    error_text = _truncate(error or summary or "未提供错误信息", _MAX_ERROR_CHARS)
    step_record = dict(existing) if isinstance(existing, dict) else {}
    step_record.update(
        {
            "status": "failed",
            "idempotency_key": idempotency_key or step_record.get("idempotency_key", ""),
            "claim_token": step_record.get("claim_token", ""),
            "error": error_text,
            "summary": _truncate(summary, _MAX_SUMMARY_CHARS),
            "lease_until": 0,
            "updated_at": now,
            "revision": revision,
        }
    )
    steps[step] = step_record
    record["status"] = "failed"
    record["current_step"] = step
    record["revision"] = revision
    record["updated_at"] = now
    record["last_error"] = {
        "step": step,
        "error": error_text,
        "summary": step_record["summary"],
        "revision": revision,
        "updated_at": now,
    }
    data_store.set(storage_key, record)
    return {
        "success": True,
        "scope": scope,
        "key": state_key,
        "failed": True,
        "workflow": record,
    }


def _list_result(
    data_store: Any,
    scope: str,
    requested_key: str,
) -> dict[str, Any]:
    """List registered workflow summaries for the current chat scope."""
    prefix = f"workflow:{scope}:"
    items: list[dict[str, Any]] = []
    for storage_key in data_store.keys():
        if not isinstance(storage_key, str) or not storage_key.startswith(prefix):
            continue
        workflow_key = storage_key[len(prefix) :]
        if requested_key and workflow_key != requested_key:
            continue
        record = _load_record(data_store.get(storage_key))
        if record is None:
            continue
        items.append(_list_item(record))
    items.sort(key=lambda item: float(item.get("updated_at") or 0), reverse=True)
    items = items[:_MAX_LIST_ITEMS]
    return {
        "success": True,
        "scope": scope,
        "key": requested_key,
        "registered": bool(items),
        "count": len(items),
        "workflows": items,
        "hint": "使用返回的 key 再调用 resume 获取权威状态；不要仅凭列表摘要执行外部操作。",
    }


def _list_item(record: dict[str, Any]) -> dict[str, Any]:
    """Expose enough directory metadata to choose a workflow without full payloads."""
    return {
        "key": record.get("key", ""),
        "workflow_id": record.get("workflow_id", ""),
        "version": record.get("version", ""),
        "run_number": record.get("run_number", 1),
        "status": record.get("status", ""),
        "revision": record.get("revision", 0),
        "summary": _truncate(record.get("summary", ""), _MAX_SUMMARY_CHARS),
        "next_step": record.get("current_step", ""),
        "last_success": _compact_success(record.get("last_success")),
        "last_error": _compact_error(record.get("last_error")),
        "updated_at": record.get("updated_at", 0),
    }


def _compact_success(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {
        "step": value.get("step", ""),
        "tool_name": value.get("tool_name", ""),
        "summary": _truncate(value.get("summary", ""), _MAX_SUMMARY_CHARS),
        "revision": value.get("revision", 0),
    }


def _compact_error(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {
        "step": value.get("step", ""),
        "error": _truncate(value.get("error", ""), _MAX_ERROR_CHARS),
        "revision": value.get("revision", 0),
    }


def _resume_result(scope: str, state_key: str, record: dict[str, Any]) -> dict[str, Any]:
    in_progress = [
        {
            "step": step,
            "tool_name": item.get("tool_name", ""),
            "idempotency_key": item.get("idempotency_key", ""),
            "claim_token": item.get("claim_token", ""),
            "lease_until": item.get("lease_until", 0),
        }
        for step, item in record.get("steps", {}).items()
        if isinstance(item, dict) and item.get("status") == "claimed"
    ]
    resumable = record.get("status") in {"active", "failed"}
    return {
        "success": True,
        "scope": scope,
        "key": state_key,
        "found": True,
        "resumable": resumable,
        "next_step": record.get("current_step", "") if resumable else "",
        "in_progress": in_progress,
        "workflow": record,
    }


def _check_revision(record: dict[str, Any], expected_revision: int) -> str | None:
    try:
        expected = int(expected_revision)
    except (TypeError, ValueError):
        return "expected_revision 必须是整数。"
    if expected < 0:
        return None
    actual = int(record.get("revision", 0))
    if actual != expected:
        return f"流程 revision 冲突：期望 {expected}，实际 {actual}；请先 resume 再继续。"
    return None


def _conflict(
    scope: str,
    state_key: str,
    error: str,
    record: dict[str, Any],
    *,
    expected_revision: int | None = None,
    expected_version: str | None = None,
) -> dict[str, Any]:
    return {
        "success": False,
        "scope": scope,
        "key": state_key,
        "conflict": True,
        "error": error,
        "expected_revision": expected_revision,
        "expected_version": expected_version,
        "workflow": record,
    }


def _parse_state(raw_state: str) -> tuple[dict[str, Any] | None, str | None]:
    raw_text = str(raw_state or "{}")
    if len(raw_text) > _MAX_STATE_CHARS:
        return None, f"state_json 不能超过 {_MAX_STATE_CHARS} 个字符"
    try:
        value = json.loads(raw_text)
    except (TypeError, json.JSONDecodeError) as exc:
        return None, f"state_json 不是有效 JSON: {exc}"
    if not isinstance(value, dict):
        return None, "state_json 必须是 JSON 对象"
    sanitized = _sanitize(value)
    if len(_json_text(sanitized)) > _MAX_STATE_CHARS:
        return None, f"state_json 清理后不能超过 {_MAX_STATE_CHARS} 个字符"
    return sanitized, None


def _sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _sanitize(item)
            for key, item in value.items()
            if not _is_sensitive_name(str(key))
        }
    if isinstance(value, list):
        return [_sanitize(item) for item in value[:64]]
    if isinstance(value, (str, int, float, bool)) or value is None:
        if isinstance(value, str):
            return value[:_MAX_SUMMARY_CHARS]
        return value
    return str(value)[:_MAX_SUMMARY_CHARS]


def _is_sensitive_name(name: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", name.lower())
    return normalized in _SENSITIVE_PARTS


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _validate_name(value: str, field_name: str, max_length: int) -> str | None:
    if not value or len(value) > max_length or not _KEY_RE.fullmatch(value):
        return f"{field_name} 必须是 1-{max_length} 个字符，且不能包含路径分隔符"
    return None


def _validate_lease(lease_seconds: int) -> str | None:
    try:
        value = int(lease_seconds)
    except (TypeError, ValueError):
        return "lease_seconds 必须是整数。"
    if not _MIN_LEASE_SECONDS <= value <= _MAX_LEASE_SECONDS:
        return f"lease_seconds 必须在 {_MIN_LEASE_SECONDS}-{_MAX_LEASE_SECONDS} 之间。"
    return None


def _truncate(value: Any, max_length: int) -> str:
    return str(value or "")[:max_length]


def _storage_key(scope: str, workflow_key: str) -> str:
    return f"workflow:{scope}:{workflow_key}"


def _now() -> float:
    return time.time()


def _scope_key(
    chat_context: dict[str, Any] | None,
    invocation_context: ToolInvocationContext | None,
) -> str:
    context = chat_context or {}
    chat_type = str(context.get("chat_type") or "").strip() or "unknown"
    chat_id = str(context.get("chat_id") or context.get("group_id") or "").strip()
    if not chat_id and invocation_context is not None:
        chat_id = invocation_context.caller_user_id
    return f"{chat_type}:{chat_id or 'global'}"
