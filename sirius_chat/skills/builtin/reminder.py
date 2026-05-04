"""Built-in skill for creating timed reminders."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

SKILL_META = {
    "name": "reminder",
    "description": (
        "设置定时提醒，支持一次性、间隔重复、每日、每周重复提醒。"
        "到达指定时间后会通知对应的用户。"
        "可以用 list 查看所有提醒，用 cancel 取消指定提醒。"
    ),
    "version": "1.2.0",
    "tags": ["utility", "time"],
    "developer_only": False,
    "dependencies": [],
    "parameters": {
        "action": {
            "type": "str",
            "description": "操作类型: create(创建) / list(查看所有提醒) / cancel(取消指定提醒)",
            "required": True,
        },
        "content": {
            "type": "str",
            "description": "提醒内容，格式为'我要提醒 <提醒人>（自己或用户名） <提醒内容>'",
            "required": False,
        },
        "mode": {
            "type": "str",
            "description": "触发模式: once(一次性) / interval(每隔N分钟重复) / daily(每日重复) / weekly(每周重复)",
            "required": False,
            "default": "once",
        },
        "minutes_after": {
            "type": "int",
            "description": "几分钟后触发（once/interval 模式必填；once 为一次性，interval 为重复间隔）",
            "required": False,
        },
        "trigger_at": {
            "type": "str",
            "description": "绝对触发时间 ISO 格式（仅 once 模式，与 minutes_after 二选一）",
            "required": False,
        },
        "time": {
            "type": "str",
            "description": "触发时间 HH:MM，例如 08:00、21:30（daily/weekly 模式必填）",
            "required": False,
        },
        "weekdays": {
            "type": "list[int]",
            "description": "星期列表 [0,1,2,3,4,5,6]，0=周一, 6=周日（仅 weekly 模式必填，支持多选如 [0,2,4] 表示周一三五）",
            "required": False,
        },
        "reminder_id": {
            "type": "str",
            "description": "提醒任务ID（cancel 时使用，可通过 list 查看）",
            "required": False,
        },
        "target": {
            "type": "str",
            "description": "提醒对象: user(提醒用户去做，默认) / self(提醒你自己去做这件事并告知用户)",
            "required": False,
            "default": "user",
        },
        "skill_chain": {
            "type": "list",
            "description": (
                "触发提醒时预先执行的 SKILL 调用链，每项为 {\"skill\":\"name\",\"params\":{...}}。"
                "执行结果会作为上下文输入给模型，供生成提醒消息时参考。"
            ),
            "required": False,
        },
        "adapter_type": {
            "type": "str",
            "description": "指定提醒消息通过哪个 adapter 发送，例如 'napcat'。留空则自动使用创建时的 adapter。",
            "required": False,
            "default": "",
        },
    },
}


def run(
    action: str = "create",
    content: str = "",
    mode: str = "once",
    minutes_after: int = 0,
    trigger_at: str = "",
    time: str = "",
    weekdays: list[int] | None = None,
    reminder_id: str = "",
    target: str = "user",
    skill_chain: list[dict[str, Any]] | None = None,
    adapter_type: str = "",
    data_store: Any = None,
    invocation_context: Any = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """Create, list, or cancel reminders."""
    action = action.strip().lower()

    caller = invocation_context.caller if invocation_context else None
    user_id = caller.user_id if caller else ""
    user_name = caller.name if caller else ""

    if action == "create":
        return _do_create(
            content=content,
            mode=mode,
            minutes_after=minutes_after,
            trigger_at=trigger_at,
            time=time,
            weekdays=weekdays,
            user_id=user_id,
            user_name=user_name,
            target=target,
            skill_chain=skill_chain,
            adapter_type=adapter_type,
            data_store=data_store,
        )
    if action == "list":
        return _do_list(data_store=data_store)
    if action == "cancel":
        return _do_cancel(
            reminder_id=reminder_id,
            data_store=data_store,
            requester_id=user_id,
        )

    return {
        "success": False,
        "error": f"未知操作: {action}，支持 create/list/cancel",
        "summary": "提醒操作失败：未知操作类型",
    }


def _do_create(
    content: str,
    mode: str,
    minutes_after: int,
    trigger_at: str,
    time: str,
    weekdays: list[int] | None,
    user_id: str,
    user_name: str,
    target: str,
    skill_chain: list[dict[str, Any]] | None,
    adapter_type: str,
    data_store: Any | None,
) -> dict[str, Any]:
    if not content or not content.strip():
        return {
            "success": False,
            "error": "提醒内容不能为空",
            "summary": "创建提醒失败：内容为空",
        }

    mode = mode.strip().lower()
    if mode not in {"once", "interval", "daily", "weekly"}:
        return {
            "success": False,
            "error": f"不支持的触发模式: {mode}，支持 once/interval/daily/weekly",
            "summary": "创建提醒失败：模式不支持",
        }

    target = target.strip().lower() if target else "user"
    if target not in {"user", "self"}:
        target = "user"

    now = datetime.now(timezone.utc)
    reminder: dict[str, Any] = {
        "id": f"rem_{uuid.uuid4().hex[:12]}",
        "content": content.strip(),
        "mode": mode,
        "target": target,
        "user_id": user_id,
        "user_name": user_name,
        "created_at": now.isoformat(),
        "last_fired_at": None,
        "fire_count": 0,
    }
    if skill_chain:
        reminder["skill_chain"] = skill_chain
    if adapter_type.strip():
        reminder["adapter_type"] = adapter_type.strip().lower()

    if mode in ("once", "interval"):
        if minutes_after and minutes_after > 0:
            fire_at = now + timedelta(minutes=minutes_after)
            reminder["fire_at"] = fire_at.isoformat()
            reminder["minutes_after"] = minutes_after
        elif mode == "once" and trigger_at:
            try:
                dt = datetime.fromisoformat(trigger_at.replace("Z", "+00:00"))
                reminder["fire_at"] = dt.isoformat()
            except ValueError:
                return {
                    "success": False,
                    "error": f"触发时间格式错误: {trigger_at}",
                    "summary": "创建提醒失败：时间格式错误",
                }
        else:
            mode_name = "一次性" if mode == "once" else "间隔"
            return {
                "success": False,
                "error": f"{mode_name}提醒需要指定 minutes_after（最小 1 分钟）",
                "summary": "创建提醒失败：未指定触发时间",
            }
    elif mode == "daily":
        if not time or not _is_valid_hhmm(time):
            return {
                "success": False,
                "error": "每日提醒需要指定有效的时间 HH:MM",
                "summary": "创建提醒失败：时间格式错误",
            }
        reminder["time"] = time
    elif mode == "weekly":
        if not time or not _is_valid_hhmm(time):
            return {
                "success": False,
                "error": "每周提醒需要指定有效的时间 HH:MM",
                "summary": "创建提醒失败：时间格式错误",
            }
        parsed_weekdays = _parse_weekdays(weekdays)
        if not parsed_weekdays:
            return {
                "success": False,
                "error": "每周提醒需要指定 weekdays 列表，如 [0,2,4] 表示周一三五",
                "summary": "创建提醒失败：星期参数错误",
            }
        reminder["time"] = time
        reminder["weekdays"] = parsed_weekdays

    _save_reminder(reminder, data_store)

    mode_desc = {"once": "一次性", "interval": "间隔", "daily": "每日", "weekly": "每周"}.get(mode, mode)
    fire_desc = ""
    if mode == "once" and reminder.get("fire_at"):
        fire_desc = f"，将在 {reminder['fire_at']} 触发"
    elif mode == "interval":
        fire_desc = f"，每隔 {minutes_after} 分钟提醒一次"
    elif mode == "daily":
        fire_desc = f"，将在每天 {time} 触发"
    elif mode == "weekly":
        wd_names = [_weekday_name(d) for d in parsed_weekdays]
        fire_desc = f"，将在每周{','.join(wd_names)} {time} 触发"

    who = f"给 {user_name}" if user_name else ""
    target_desc = "提醒用户" if target == "user" else "提醒自己"
    return {
        "success": True,
        "summary": f"已创建{mode_desc}提醒{who}{fire_desc}",
        "text_blocks": [
            f"✅ 已设置提醒（ID: {reminder['id']}）\n"
            f"对象: {user_name or '未指定'}\n"
            f"目标: {target_desc}\n"
            f"内容: {reminder['content']}\n"
            f"模式: {mode_desc}{fire_desc}"
        ],
        "internal_metadata": {"reminder_id": reminder["id"]},
    }


def _do_list(data_store: Any | None) -> dict[str, Any]:
    reminders = _load_reminders(data_store)
    if not reminders:
        return {
            "success": True,
            "summary": "当前没有设置任何提醒",
            "text_blocks": ["当前没有待触发的提醒任务。"],
        }

    lines = [f"共 {len(reminders)} 个提醒任务："]
    for r in reminders:
        mode_desc = {"once": "一次性", "daily": "每日", "weekly": "每周"}.get(r["mode"], r["mode"])
        who = f"[{r.get('user_name') or r.get('user_id', '?')}] "
        detail = f"[{r['id']}] {who}{mode_desc} | {r['content']}"
        if r.get("fire_at"):
            detail += f" | 触发: {r['fire_at']}"
        if r.get("time"):
            detail += f" | 时间: {r['time']}"
        if r.get("weekdays"):
            wd_names = [_weekday_name(d) for d in r["weekdays"]]
            detail += f" ({','.join(wd_names)})"
        elif r.get("weekday") is not None:
            detail += f" ({_weekday_name(r['weekday'])}"
        lines.append(detail)

    return {
        "success": True,
        "summary": f"列出 {len(reminders)} 个提醒任务",
        "text_blocks": ["\n".join(lines)],
    }


def _do_cancel(
    reminder_id: str, data_store: Any | None, requester_id: str = ""
) -> dict[str, Any]:
    if not reminder_id:
        return {
            "success": False,
            "error": "取消提醒需要提供 reminder_id",
            "summary": "取消提醒失败：未提供ID",
        }

    reminders = _load_reminders(data_store)
    target = next((r for r in reminders if r.get("id") == reminder_id), None)
    if target is None:
        return {
            "success": False,
            "error": f"未找到提醒任务: {reminder_id}",
            "summary": "取消提醒失败：任务不存在",
        }

    owner_id = target.get("user_id", "")
    if owner_id and requester_id and owner_id != requester_id:
        owner_name = target.get("user_name") or owner_id
        return {
            "success": False,
            "error": f"该提醒由 {owner_name} 创建，只有创建者本人可以取消",
            "summary": "取消提醒失败：权限不足",
        }

    reminders = [r for r in reminders if r.get("id") != reminder_id]
    _store_reminders(reminders, data_store)
    return {
        "success": True,
        "summary": f"已取消提醒任务 {reminder_id}",
        "text_blocks": [f"✅ 已取消提醒任务 {reminder_id}"],
    }


def _is_valid_hhmm(value: str) -> bool:
    try:
        h, m = value.split(":")
        hi, mi = int(h), int(m)
        return 0 <= hi <= 23 and 0 <= mi <= 59
    except Exception:
        return False


def _weekday_name(d: int) -> str:
    names = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
    return names[d] if 0 <= d <= 6 else str(d)


def _parse_weekdays(value: list[int] | None) -> list[int]:
    """Parse and validate weekdays list, returning sorted unique valid values."""
    if value is None:
        return []
    if isinstance(value, int):
        if 0 <= value <= 6:
            return [value]
        return []
    if isinstance(value, str):
        try:
            parsed = __import__("json").loads(value)
            if isinstance(parsed, list):
                value = parsed
            elif isinstance(parsed, int):
                value = [parsed]
            else:
                return []
        except Exception:
            return []
    if not isinstance(value, list):
        return []
    result: set[int] = set()
    for item in value:
        try:
            d = int(item)
            if 0 <= d <= 6:
                result.add(d)
        except (ValueError, TypeError):
            continue
    return sorted(result)


def _load_reminders(data_store: Any | None) -> list[dict[str, Any]]:
    if data_store is None:
        return []
    return list(data_store.get("reminders", []))


def _store_reminders(reminders: list[dict[str, Any]], data_store: Any | None) -> None:
    if data_store is not None:
        data_store.set("reminders", reminders)


def _save_reminder(reminder: dict[str, Any], data_store: Any | None) -> None:
    reminders = _load_reminders(data_store)
    reminders.append(reminder)
    _store_reminders(reminders, data_store)
