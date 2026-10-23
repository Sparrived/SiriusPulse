"""Unified NapCat / OneBot API tool.

群成员查询、群管理与点赞曾经是三份各自实现参数的专用 Tool。现在它们收敛成
同一个入口：模型只描述要调用的 OneBot action 与其参数，工具负责别名归一化、
群号注入、整数归一化、管理动作的权限拦截与结果格式化。

接口清单与高级用法由 Skill ``napcat-cookbook`` 承载，按需通过 ``read_skill`` 读取，
避免把 OneBot 的全部 action 塞进工具描述里。
"""

from __future__ import annotations

import json
import time
from typing import Any

from sirius_pulse.config.config_builder import ConfigBuilder
from sirius_pulse.tools.builtin._internal._qq_ops import (
    bridge_error,
    current_group_id,
    failure_from_exception,
    format_member,
    get_adapter,
    group_error,
    member_display,
    success_result,
)
from sirius_pulse.tools.models import ToolInvocationContext

_MAX_LIKE_TIMES = 10
_MAX_MEMBER_DISPLAY = 80
_MAX_TEXT_CHARS = 2_000

#: 模型常用的口语化名字映射到真实的 OneBot action。
_ACTION_ALIASES = {
    "kick": "set_group_kick",
    "mute": "set_group_ban",
    "mute_member": "set_group_ban",
    "mute_all": "set_group_whole_ban",
    "whole_ban": "set_group_whole_ban",
    "like": "send_like",
    "list_members": "get_group_member_list",
    "get_member": "get_group_member_info",
}

#: 只有开发者（管理员）可以执行的管理动作。
_ADMIN_ACTIONS = frozenset(
    {
        "set_group_ban",
        "set_group_whole_ban",
        "set_group_kick",
        "set_group_card",
        "set_group_name",
        "set_group_admin",
        "set_group_special_title",
        "delete_msg",
    }
)

_GROUP_ACTION_PREFIXES = ("get_group_", "set_group_")

_INT_FIELDS = ("user_id", "group_id", "duration", "times", "message_id")
_ID_FIELDS = ("user_id", "group_id")

_SYSTEM_PARAM_KEYS = frozenset(
    {
        "bridge",
        "chat_context",
        "invocation_context",
        "engine_context",
        "data_store",
        "chain_context",
        "tool_chain_context",
        "kwargs",
    }
)

#: user_id 里表示“沿用上一次目标”的占位写法。
_PLACEHOLDER_TARGETS = frozenset({"", "reuse_last", "reuse", "last", "previous", "上次", "同上"})

_config = ConfigBuilder()
_config.group("NapCat API").add(
    "action",
    type="str",
    description=(
        "要调用的 NapCat / OneBot action，例如 send_like、get_group_member_list、"
        "get_group_member_info、set_group_ban、set_group_whole_ban、set_group_kick、"
        "set_group_card。也接受别名 kick / mute_member / mute_all / like / "
        "list_members / get_member。"
    ),
    required=True,
)
_config.group("NapCat API").add(
    "params",
    type="dict",
    description=(
        "该 action 的参数字典，例如 {\"user_id\": 123456, \"times\": 3}；"
        "也可以直接给 JSON 字符串。也可以把参数平铺在顶层传参，两者会合并。"
    ),
    default={},
)


TOOL_META = {
    "name": "napcat_call",
    "description": (
        "调用 NapCat / OneBot API 的统一操作工具。常用 action：send_like(点赞, user_id, times), "
        "get_group_member_list(群成员列表), get_group_member_info(群成员信息, user_id), "
        "set_group_ban(禁言, user_id, duration), set_group_whole_ban(全员禁言, enable), "
        "set_group_kick(移出群, user_id), set_group_card(改群名片, user_id, card)。"
        "在工作模式下可使用 read_skill('napcat-cookbook') 查看完整接口列表与高级功能示例。"
    ),
    "version": "1.0.0",
    "side_effect": "external_write",
    "tags": ["napcat", "qq", "api", "group_admin", "like"],
    "adapter_types": ["napcat"],
    "allow_extra_parameters": True,
    "parameters": _config.build(),
}


async def run(
    action: str = "",
    params: dict[str, Any] | str | None = None,
    bridge: Any = None,
    chat_context: dict[str, Any] | None = None,
    invocation_context: ToolInvocationContext | None = None,
    data_store: Any = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """把一次 OneBot action 请求转发给当前 NapCat 适配器并格式化结果。"""
    normalized_action = _normalize_action(action)
    if not normalized_action:
        return {
            "success": False,
            "error": "缺少必填参数: action",
            "summary": "执行 NapCat 操作失败：缺少 action",
        }

    merged_params, params_error = _merge_params(params, kwargs)
    if params_error:
        return {
            "success": False,
            "error": params_error,
            "summary": f"执行 {normalized_action} 失败：参数无法解析",
        }

    if _is_group_action(normalized_action):
        _inject_group_id(merged_params, chat_context)
    _coerce_int_fields(merged_params)

    if normalized_action in _ADMIN_ACTIONS and not _caller_is_developer(invocation_context):
        return {
            "success": False,
            "error": f"权限不足：只有管理员或开发者允许执行管理动作「{normalized_action}」。",
            "summary": f"执行 {normalized_action} 失败：权限不足",
        }

    adapter = get_adapter(bridge)
    if adapter is None:
        return bridge_error("执行 NapCat 操作")

    if _is_group_action(normalized_action) and not merged_params.get("group_id"):
        return group_error(f"执行 {normalized_action}")

    if normalized_action == "send_like":
        return await _send_like(
            adapter, merged_params, chat_context, invocation_context, data_store
        )

    try:
        raw = await _call_adapter(adapter, normalized_action, merged_params)
    except Exception as exc:  # noqa: BLE001 — 适配器异常一律反馈给模型
        return failure_from_exception(f"执行 {normalized_action}", exc)
    return _format_result(normalized_action, merged_params, raw)


# ─── 参数归一化 ─────────────────────────────────────────────


def _normalize_action(action: Any) -> str:
    raw = str(action or "").strip().lower().replace("-", "_")
    if not raw:
        return ""
    return _ACTION_ALIASES.get(raw, raw)


def _merge_params(
    params: dict[str, Any] | str | None, kwargs: dict[str, Any]
) -> tuple[dict[str, Any], str]:
    """合并 ``params`` 与顶层平铺参数，返回 (参数, 错误信息)。"""
    merged: dict[str, Any] = {}
    if isinstance(params, str):
        text = params.strip()
        if text:
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as exc:
                return {}, f"params 不是合法 JSON：{exc}"
            if not isinstance(parsed, dict):
                return {}, "params 必须是 JSON 对象"
            merged.update(parsed)
    elif isinstance(params, dict):
        merged.update(params)
    elif params is not None:
        return {}, "params 必须是对象或 JSON 对象字符串"

    for key, value in kwargs.items():
        if key in _SYSTEM_PARAM_KEYS:
            continue
        merged.setdefault(key, value)
    # 模型若把系统键塞进了 params 里，也不要透给平台接口。
    for key in list(merged):
        if key in _SYSTEM_PARAM_KEYS:
            merged.pop(key, None)
    return merged, ""


def _is_group_action(action: str) -> bool:
    return action.startswith(_GROUP_ACTION_PREFIXES)


def _inject_group_id(params: dict[str, Any], chat_context: dict[str, Any] | None) -> None:
    """群相关 action 缺少 group_id 时，从当前群聊上下文补齐。"""
    if params.get("group_id") not in (None, ""):
        return
    group_id = current_group_id(chat_context)
    if group_id:
        params["group_id"] = group_id


def _coerce_int_fields(params: dict[str, Any]) -> None:
    """把纯数字字符串形式的 ID / 时长 / 次数归一化为 int。"""
    for field in _INT_FIELDS:
        if field not in params:
            continue
        value = params[field]
        if isinstance(value, bool) or isinstance(value, int):
            continue
        if isinstance(value, float) and value.is_integer():
            params[field] = int(value)
            continue
        if not isinstance(value, str):
            continue
        text = value.strip()
        if field in _ID_FIELDS and text.lower().startswith("qq_"):
            text = text[3:]
        if not text:
            continue
        try:
            params[field] = int(text)
        except ValueError:
            continue


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on", "enable", "enabled", "开", "开启"}:
        return True
    if text in {"0", "false", "no", "n", "off", "disable", "disabled", "关", "关闭"}:
        return False
    return default


def _caller_is_developer(invocation_context: ToolInvocationContext | None) -> bool:
    if invocation_context is None:
        return False
    caller = getattr(invocation_context, "caller", None)
    if caller is None:
        return bool(getattr(invocation_context, "caller_is_developer", False))
    flag = getattr(caller, "is_developer", False)
    if isinstance(flag, bool):
        return flag
    metadata = getattr(caller, "metadata", None)
    if isinstance(metadata, dict):
        return _as_bool(metadata.get("is_developer"), default=False)
    return False


# ─── 适配器调用 ─────────────────────────────────────────────


async def _call_adapter(
    adapter: Any, action: str, params: dict[str, Any]
) -> dict[str, Any] | list[Any] | None:
    """优先走 ``call_api``，老适配器回退到同名方法。"""
    call_api = getattr(adapter, "call_api", None)
    if callable(call_api):
        return await call_api(action, dict(params))
    method = getattr(adapter, action, None)
    if not callable(method):
        raise NotImplementedError(f"当前 NapCat 适配器不支持 action「{action}」")
    return await method(**params)


def _payload_of(raw: Any) -> Any:
    """从 OneBot 响应里取出 data 字段；已是裸数据时原样返回。"""
    if isinstance(raw, dict) and "data" in raw:
        return raw["data"]
    return raw


# ─── 结果格式化 ─────────────────────────────────────────────


def _format_result(action: str, params: dict[str, Any], raw: Any) -> dict[str, Any]:
    payload = _payload_of(raw)
    if action == "get_group_member_list":
        return _format_member_list(params, payload, raw)
    if action == "get_group_member_info":
        return _format_member_info(params, payload, raw)
    if action == "set_group_ban":
        return _format_group_ban(params, raw)
    if action == "set_group_whole_ban":
        return _format_whole_ban(params, raw)
    if action == "set_group_kick":
        return _format_group_kick(params, raw)
    if action == "set_group_card":
        return _format_group_card(params, raw)
    return _generic_result(action, params, payload)


def _format_member_list(
    params: dict[str, Any], payload: Any, raw: Any
) -> dict[str, Any]:
    if not isinstance(payload, list):
        return _generic_result("get_group_member_list", params, payload)
    group_id = str(params.get("group_id", "")).strip()
    shown = [member for member in payload[:_MAX_MEMBER_DISPLAY] if isinstance(member, dict)]
    lines = [f"群 {group_id} 成员共 {len(payload)} 人，以下显示前 {len(shown)} 人："]
    lines.extend(format_member(member) for member in shown)
    remaining = len(payload) - len(shown)
    if remaining > 0:
        lines.append(f"...还有 {remaining} 人未显示")
    return {
        "success": True,
        "summary": f"已获取群 {group_id} 成员 {len(payload)} 人",
        "text_blocks": ["\n".join(lines)],
        "internal_metadata": {
            "action": "get_group_member_list",
            "group_id": group_id,
            "member_count": len(payload),
            "members": payload,
        },
    }


def _format_member_info(
    params: dict[str, Any], payload: Any, raw: Any
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return _generic_result("get_group_member_info", params, payload)
    group_id = str(params.get("group_id", "")).strip()
    user_id = str(params.get("user_id", payload.get("user_id", ""))).strip()
    lines = [f"QQ {user_id}: {member_display(payload)}"]
    for key, label in (
        ("card", "群名片"),
        ("role", "角色"),
        ("title", "头衔"),
        ("level", "等级"),
        ("sex", "性别"),
        ("age", "年龄"),
        ("join_time", "入群时间"),
        ("last_sent_time", "最后发言"),
    ):
        value = payload.get(key)
        if value in (None, ""):
            continue
        lines.append(f"- {label}: {value}")
    return {
        "success": True,
        "summary": f"已获取成员 {user_id} 信息",
        "text_blocks": ["\n".join(lines)],
        "internal_metadata": {
            "action": "get_group_member_info",
            "group_id": group_id,
            "user_id": user_id,
            "member": payload,
        },
    }


def _format_group_ban(params: dict[str, Any], raw: Any) -> dict[str, Any]:
    group_id = params.get("group_id")
    user_id = params.get("user_id")
    seconds = int(params.get("duration", 0) or 0)
    if seconds > 0:
        summary = f"已禁言群成员 {user_id} {seconds} 秒"
    else:
        summary = f"已解除群成员 {user_id} 的禁言"
    return success_result(
        summary,
        text=summary,
        action="set_group_ban",
        group_id=group_id,
        user_id=user_id,
        duration=seconds,
    )


def _format_whole_ban(params: dict[str, Any], raw: Any) -> dict[str, Any]:
    enable = _as_bool(params.get("enable"), default=True)
    group_id = params.get("group_id")
    summary = "已开启全员禁言" if enable else "已关闭全员禁言"
    return success_result(
        summary,
        text=f"{summary}（群 {group_id}）",
        action="set_group_whole_ban",
        group_id=group_id,
        enable=enable,
    )


def _format_group_kick(params: dict[str, Any], raw: Any) -> dict[str, Any]:
    group_id = params.get("group_id")
    user_id = params.get("user_id")
    summary = f"已将成员 {user_id} 移出群 {group_id}"
    return success_result(
        summary,
        text=summary,
        action="set_group_kick",
        group_id=group_id,
        user_id=user_id,
    )


def _format_group_card(params: dict[str, Any], raw: Any) -> dict[str, Any]:
    group_id = params.get("group_id")
    user_id = params.get("user_id")
    card = str(params.get("card", "") or "").strip()
    summary = f"已将成员 {user_id} 的群名片改为「{card}」"
    return success_result(
        summary,
        text=summary,
        action="set_group_card",
        group_id=group_id,
        user_id=user_id,
        card=card,
    )


def _generic_result(action: str, params: dict[str, Any], payload: Any) -> dict[str, Any]:
    if payload in (None, "", [], {}):
        text = f"已执行 {action}（平台未返回数据）"
    else:
        text = json.dumps(payload, ensure_ascii=False)
        if len(text) > _MAX_TEXT_CHARS:
            text = f"{text[:_MAX_TEXT_CHARS]}…（已截断）"
    result = success_result(
        f"已执行 {action}",
        text=text,
        action=action,
        params=dict(params),
    )
    result["data"] = payload
    return result


# ─── 点赞 ────────────────────────────────────────────────


async def _send_like(
    adapter: Any,
    params: dict[str, Any],
    chat_context: dict[str, Any] | None,
    invocation_context: ToolInvocationContext | None,
    data_store: Any,
) -> dict[str, Any]:
    try:
        times = int(params.get("times", 1) or 1)
    except (TypeError, ValueError):
        return {
            "success": False,
            "error": "times 必须是整数",
            "summary": "执行 send_like 失败：times 不是整数",
        }
    times = max(1, min(times, _MAX_LIKE_TIMES))

    scope = _scope_key(chat_context, invocation_context)
    reuse_requested = _as_bool(params.get("reuse_last"), default=False)
    target = params.get("user_id")
    reused = False

    if _is_placeholder_target(target):
        if reuse_requested and data_store is not None:
            previous = data_store.get(f"last_success:{scope}")
            if isinstance(previous, dict) and previous.get("user_id"):
                target = previous["user_id"]
                reused = True
        if _is_placeholder_target(target):
            target = _caller_user_id(chat_context, invocation_context)

    if _is_placeholder_target(target):
        return {
            "success": False,
            "error": "缺少有效的目标 QQ 号 user_id",
            "summary": "执行 send_like 失败：缺少目标 QQ 号",
        }

    target_id = _to_positive_int(target)
    if target_id is None:
        return {
            "success": False,
            "error": f"user_id 必须是有效 QQ 号，收到：{target}",
            "summary": "执行 send_like 失败：user_id 无效",
        }

    try:
        await _call_adapter(adapter, "send_like", {"user_id": target_id, "times": times})
    except Exception as exc:  # noqa: BLE001 — 失败结果原样反馈给模型
        return failure_from_exception("QQ点赞", exc)

    if data_store is not None:
        data_store.set(
            f"last_success:{scope}",
            {
                "user_id": target_id,
                "times": times,
                "action": "send_like",
                "scope": scope,
                "saved_at": time.time(),
            },
        )

    summary = f"已给 QQ {target_id} 点赞 {times} 次"
    return success_result(
        summary,
        text=summary,
        action="send_like",
        user_id=target_id,
        times=times,
        reused_last=reused,
        scope=scope,
    )


def _is_placeholder_target(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str) and value.strip().lower() in _PLACEHOLDER_TARGETS:
        return True
    return False


def _to_positive_int(value: Any) -> int | None:
    text = str(value).strip()
    if text.lower().startswith("qq_"):
        text = text[3:]
    try:
        result = int(text)
    except (TypeError, ValueError):
        return None
    return result if result > 0 else None


def _caller_user_id(
    chat_context: dict[str, Any] | None,
    invocation_context: ToolInvocationContext | None,
) -> str:
    if invocation_context is not None and invocation_context.caller_user_id:
        return invocation_context.caller_user_id
    return str((chat_context or {}).get("user_id") or "").strip()


def _scope_key(
    chat_context: dict[str, Any] | None,
    invocation_context: ToolInvocationContext | None,
) -> str:
    context = chat_context or {}
    chat_type = str(context.get("chat_type") or "unknown").strip()
    chat_id = str(context.get("chat_id") or context.get("group_id") or "").strip()
    if not chat_id and invocation_context is not None:
        chat_id = invocation_context.caller_user_id
    return f"{chat_type}:{chat_id or 'global'}"
