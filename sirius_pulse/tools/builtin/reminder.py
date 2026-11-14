"""提醒：别人托她到某个时间提醒一件事，或她答应晚点再提起。

以前唯一的办法是进工作模式写五字段 crontab：语法门槛高，``crontab -`` 还是覆盖语义
（加一条会冲掉别的），也表达不了「只提醒一次」。这里把提醒做成普通聊天里就能用的
小工具：

- 时间用本地时间（UTC+8）或「几分钟后」，可选每天 / 工作日 / 每周重复；
- 提醒正文在设提醒时就由她用自己的口气写好，到点原样发出，并写进聊天历史；
- 到点时即使她在睡觉也照常发——这是别人托她的事，和她自己主动说话不一样；
- 进程停过的话，晚了不超过几个小时的提醒照发，再晚的记为错过，不再补发。

提醒存在本工具的数据存储里（``tool_data/reminder.json``），由后台心跳每隔十几秒检查一次。
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sirius_pulse.extension_runtime import BackgroundTaskSpec
from sirius_pulse.life.clock import LOCAL_TIMEZONE, to_local
from sirius_pulse.tools.engine_context import DeliveryAccess, ToolEngineContext
from sirius_pulse.tools.models import ToolInvocationContext

logger = logging.getLogger(__name__)

_STORE_KEY = "reminders"
_TICK_SECONDS = 15.0
_DEFAULT_MAX_PENDING = 20
_MAX_MESSAGE_CHARS = 500
_MIN_LEAD = timedelta(seconds=30)
_MAX_LEAD = timedelta(days=366)
#: 进程停过、到点没发出去的提醒，晚了这么久以内照发，再晚就记为错过。
_MISSED_AFTER = timedelta(hours=6)
_MAX_RETAINED_FINISHED = 100

_REPEATS = ("none", "daily", "weekdays", "weekly")
_REPEAT_LABELS = {"none": "", "daily": "，每天", "weekdays": "，每个工作日", "weekly": "，每周"}

TOOL_META = {
    "name": "reminder",
    "description": (
        "有人托你到点提醒他（或提醒群里）一件事，或你答应了晚点再提起某事时用。"
        "到点会把 message 原样发到这个聊天，你在睡觉也照发。只有设提醒的人或开发者能取消。"
    ),
    "version": "1.0.0",
    "side_effect": "external_write",
    "actions": {"list": {"side_effect": "read_only", "retry_safe": True}},
    "tags": ["reminder", "schedule", "time"],
    "parameters": {
        "action": {
            "type": "string",
            "choices": ["add", "list", "cancel"],
            "description": "add 设一个提醒；list 查看本聊天待发的提醒；cancel 取消一个提醒。",
            "required": True,
        },
        "at": {
            "type": "string",
            "description": (
                "add：提醒时间，本地时间（UTC+8），如 2026-11-15 20:00、11-15 20:00 或 20:00"
                "（只写时刻表示接下来的那一次）。与 in_minutes 二选一。"
            ),
            "required": False,
            "default": "",
        },
        "in_minutes": {
            "type": "integer",
            "description": "add：多少分钟后提醒（1 到 525600）。与 at 二选一。",
            "required": False,
            "default": 0,
        },
        "message": {
            "type": "string",
            "description": (
                "add：到点时要发出去的原话（500 字以内），用你自己的口气写完整，自带上下文，" "让人一看就知道是提醒什么；群里要 @ 人就在开头写 [AT:QQ号]。"
            ),
            "required": False,
            "default": "",
        },
        "repeat": {
            "type": "string",
            "choices": list(_REPEATS),
            "description": ("add：none 只提醒一次（默认）；daily 每天；weekdays 每个工作日（周一到周五）；weekly 每周。"),
            "required": False,
            "default": "none",
        },
        "reminder_id": {
            "type": "string",
            "description": "cancel：要取消的提醒 ID（形如 r-xxxxxxxx，list 里能看到）。",
            "required": False,
            "default": "",
        },
    },
    "config": {
        "max_pending_per_chat": {
            "type": "int",
            "description": "每个聊天最多同时挂着多少个待发提醒，范围 1 到 100。",
            "default": _DEFAULT_MAX_PENDING,
            "group": "限制",
        },
    },
}


async def run(
    action: str,
    at: str = "",
    in_minutes: int = 0,
    message: str = "",
    repeat: str = "none",
    reminder_id: str = "",
    *,
    data_store: Any = None,
    chat_context: dict[str, Any] | None = None,
    invocation_context: ToolInvocationContext | None = None,
    now: datetime | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    if data_store is None:
        return {"success": False, "error": "reminder 需要可持久化的数据存储"}
    _reload(data_store)
    if data_store.get("_enabled", True) is False:
        return {"success": False, "error": "reminder Tool 已被当前人格禁用"}

    context = dict(chat_context or {})
    if not context and invocation_context is not None:
        context = dict(invocation_context.chat_context)
    group_id = (invocation_context.group_id if invocation_context else "") or str(
        context.get("group_id") or ""
    )
    if not group_id:
        return {"success": False, "error": "提醒要在一个具体的聊天里设置"}
    current = now or datetime.now(timezone.utc)
    action = str(action or "").strip().lower()

    if action == "add":
        return _add(
            data_store,
            group_id=group_id,
            adapter_type=str(context.get("adapter_type") or ""),
            invocation_context=invocation_context,
            at=at,
            in_minutes=in_minutes,
            message=message,
            repeat=repeat,
            now=current,
        )
    if action == "list":
        pending = [r for r in _load(data_store) if _pending_in(r, group_id)]
        if not pending:
            return _reply("这个聊天里没有待发的提醒。", {"reminders": []})
        pending.sort(key=lambda r: str(r.get("due_at")))
        lines = ["这个聊天里待发的提醒："] + [_describe(r) for r in pending]
        return _reply("\n".join(lines), {"reminders": [_public(r) for r in pending]})
    if action == "cancel":
        return _cancel(data_store, group_id, reminder_id, invocation_context)
    return {"success": False, "error": f"不支持的 action: {action}（可用 add/list/cancel）"}


def _add(
    data_store: Any,
    *,
    group_id: str,
    adapter_type: str,
    invocation_context: ToolInvocationContext | None,
    at: str,
    in_minutes: Any,
    message: str,
    repeat: str,
    now: datetime,
) -> dict[str, Any]:
    text = str(message or "").strip()
    if not text:
        return {"success": False, "error": "message 不能为空：写下到点时要发出去的原话"}
    if len(text) > _MAX_MESSAGE_CHARS:
        return {"success": False, "error": f"message 太长了，最多 {_MAX_MESSAGE_CHARS} 字"}
    repeat = str(repeat or "none").strip().lower()
    if repeat not in _REPEATS:
        return {"success": False, "error": f"repeat 只能是 {' / '.join(_REPEATS)}"}

    try:
        minutes = int(in_minutes or 0)
    except (TypeError, ValueError):
        return {"success": False, "error": "in_minutes 必须是整数"}
    try:
        if minutes > 0:
            due = now + timedelta(minutes=min(minutes, 525_600))
        elif str(at or "").strip():
            due = parse_local_time(str(at), now)
        else:
            return {"success": False, "error": "要给出提醒时间：at 或 in_minutes 二选一"}
    except ValueError as exc:
        return {"success": False, "error": str(exc)}
    if repeat == "weekdays":
        due = _skip_weekend(due)
    if due < now + _MIN_LEAD:
        return {"success": False, "error": "提醒时间已经过去了（或太近），请给一个之后的时间"}
    if due > now + _MAX_LEAD:
        return {"success": False, "error": "提醒最远只能设到一年以内"}

    reminders = _load(data_store)
    limit = _max_pending(data_store)
    if sum(1 for r in reminders if _pending_in(r, group_id)) >= limit:
        return {
            "success": False,
            "error": f"这个聊天里待发的提醒已经有 {limit} 个了，先取消一些再设新的",
        }
    reminder = {
        "id": f"r-{uuid.uuid4().hex[:8]}",
        "status": "pending",
        "group_id": group_id,
        "adapter_type": adapter_type,
        "creator_id": invocation_context.caller_user_id if invocation_context else "",
        "creator_name": invocation_context.caller_name if invocation_context else "",
        "message": text,
        "repeat": repeat,
        "due_at": due.isoformat(),
        "created_at": now.isoformat(),
        "sent_count": 0,
    }
    reminders.append(reminder)
    _save(data_store, reminders)
    logger.info("已设提醒: %s @ %s (%s)", reminder["id"], reminder["due_at"], repeat)
    return _reply(
        f"已设好提醒 {reminder['id']}：{_local_label(due)}{_REPEAT_LABELS[repeat]}，" f"到时会发出：{text}",
        {"reminder": _public(reminder)},
    )


def _cancel(
    data_store: Any,
    group_id: str,
    reminder_id: str,
    invocation_context: ToolInvocationContext | None,
) -> dict[str, Any]:
    target = str(reminder_id or "").strip()
    if not target:
        return {"success": False, "error": "cancel 需要 reminder_id（list 里能看到）"}
    reminder = next(
        (r for r in _load(data_store) if r.get("id") == target and r.get("group_id") == group_id),
        None,
    )
    if reminder is None or reminder.get("status") != "pending":
        return {"success": False, "error": f"这个聊天里没有待发的提醒 {target}"}
    caller = invocation_context.caller_user_id if invocation_context else ""
    developer = bool(invocation_context and invocation_context.caller_is_developer)
    if not developer and caller != reminder.get("creator_id"):
        return {"success": False, "error": "只有设这个提醒的人或开发者能取消它"}
    updated = _update(data_store, target, status="cancelled")
    return _reply(f"已取消提醒 {target}。", {"reminder": _public(updated or reminder)})


# ---------------------------------------------------------------------------
# 后台：到点发送
# ---------------------------------------------------------------------------


def create_background_tasks(ctx: ToolEngineContext) -> list[BackgroundTaskSpec]:
    async def _tick() -> None:
        await deliver_due(ctx, ctx.get_data_store("reminder"))

    return [
        BackgroundTaskSpec(name="reminder_due", interval_seconds=_TICK_SECONDS, task_func=_tick)
    ]


async def deliver_due(ctx: DeliveryAccess, data_store: Any, now: datetime | None = None) -> int:
    """把已经到点的提醒发出去，返回这次成功发出的条数。"""
    current = now or datetime.now(timezone.utc)
    _reload(data_store)
    sent = 0
    for reminder in _load(data_store):
        if reminder.get("status") != "pending":
            continue
        due = _parse_stored(reminder.get("due_at"))
        if due is None or due > current:
            continue
        reminder_id = str(reminder.get("id"))
        if current - due > _MISSED_AFTER:
            _after_occurrence(data_store, reminder, due, current, delivered=False)
            logger.warning("提醒错过了发送时间，不再补发: %s", reminder_id)
            continue
        try:
            delivered = await ctx.dispatch_proactive_message(
                group_id=str(reminder.get("group_id")),
                text=str(reminder.get("message")),
                adapter_type=str(reminder.get("adapter_type") or ""),
                event_id=f"reminder:{reminder_id}:{reminder.get('due_at')}",
                record_delivery=True,
            )
        except Exception:
            logger.warning("提醒发送失败，稍后重试: %s", reminder_id, exc_info=True)
            delivered = False
        if delivered:
            sent += 1
            _after_occurrence(data_store, reminder, due, current, delivered=True)
    return sent


def _after_occurrence(
    data_store: Any, reminder: dict[str, Any], due: datetime, now: datetime, *, delivered: bool
) -> None:
    """一次提醒发完（或错过）之后：重复的排到下一次，一次性的结束。"""
    fields: dict[str, Any] = {}
    if delivered:
        fields["sent_count"] = int(reminder.get("sent_count") or 0) + 1
        fields["last_sent_at"] = now.isoformat()
    repeat = str(reminder.get("repeat") or "none")
    if repeat == "none":
        fields["status"] = "sent" if delivered else "missed"
        fields["finished_at"] = now.isoformat()
    else:
        fields["due_at"] = next_occurrence(due, repeat, after=now).isoformat()
        if not delivered:
            fields["missed_count"] = int(reminder.get("missed_count") or 0) + 1
    _update(data_store, str(reminder.get("id")), **fields)


# ---------------------------------------------------------------------------
# 时间
# ---------------------------------------------------------------------------


def parse_local_time(text: str, now: datetime) -> datetime:
    """把模型写的本地时间解析成 UTC 时刻；只写时刻 / 月日时取接下来的那一次。"""
    raw = str(text or "").strip()
    if not raw:
        raise ValueError("at 不能为空")
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        parsed = None
    if parsed is not None:
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=LOCAL_TIMEZONE)
        return parsed.astimezone(timezone.utc)

    normalized = raw.replace("/", "-").replace("：", ":").replace("T", " ")
    local_now = to_local(now)
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S"):
        try:
            value = datetime.strptime(normalized, fmt).replace(tzinfo=LOCAL_TIMEZONE)
            return value.astimezone(timezone.utc)
        except ValueError:
            pass
    try:
        value = datetime.strptime(normalized, "%m-%d %H:%M")
        candidate = value.replace(year=local_now.year, tzinfo=LOCAL_TIMEZONE)
        if candidate <= local_now:
            candidate = candidate.replace(year=local_now.year + 1)
        return candidate.astimezone(timezone.utc)
    except ValueError:
        pass
    try:
        clock = datetime.strptime(normalized, "%H:%M")
        candidate = local_now.replace(hour=clock.hour, minute=clock.minute, second=0, microsecond=0)
        if candidate <= local_now:
            candidate += timedelta(days=1)
        return candidate.astimezone(timezone.utc)
    except ValueError:
        pass
    raise ValueError(f"看不懂这个时间：{raw}。请写成 2026-11-15 20:00、11-15 20:00 或 20:00 这样的本地时间")


def next_occurrence(due: datetime, repeat: str, *, after: datetime) -> datetime:
    """重复提醒的下一次：按本地日历推进，跳过已经过去的时刻。"""
    step = timedelta(days=7) if repeat == "weekly" else timedelta(days=1)
    local = to_local(due)
    while True:
        local += step
        if repeat == "weekdays":
            while local.weekday() >= 5:
                local += timedelta(days=1)
        if local.astimezone(timezone.utc) > after:
            return local.astimezone(timezone.utc)


def _skip_weekend(due: datetime) -> datetime:
    local = to_local(due)
    while local.weekday() >= 5:
        local += timedelta(days=1)
    return local.astimezone(timezone.utc)


def _local_label(moment: datetime) -> str:
    local = to_local(moment)
    weekday = "一二三四五六日"[local.weekday()]
    return f"{local.month}月{local.day}日（周{weekday}）{local:%H:%M}"


def _parse_stored(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 存储
# ---------------------------------------------------------------------------


def _pending_in(reminder: dict[str, Any], group_id: str) -> bool:
    return reminder.get("status") == "pending" and reminder.get("group_id") == group_id


def _describe(reminder: dict[str, Any]) -> str:
    due = _parse_stored(reminder.get("due_at"))
    when = _local_label(due) if due else "时间未知"
    label = _REPEAT_LABELS.get(str(reminder.get("repeat") or "none"), "")
    who = str(reminder.get("creator_name") or "").strip()
    by = f"（{who} 设的）" if who else ""
    return f"- {reminder.get('id')}｜{when}{label}{by}：{str(reminder.get('message'))[:80]}"


def _public(reminder: dict[str, Any]) -> dict[str, Any]:
    return {
        key: reminder.get(key)
        for key in ("id", "status", "due_at", "repeat", "message", "creator_name")
    }


def _reply(text: str, data: dict[str, Any]) -> dict[str, Any]:
    return {"success": True, "text_blocks": [text], "data": data}


def _max_pending(data_store: Any) -> int:
    try:
        value = int(data_store.get("max_pending_per_chat", _DEFAULT_MAX_PENDING))
    except (TypeError, ValueError):
        value = _DEFAULT_MAX_PENDING
    return max(1, min(100, value))


def _reload(data_store: Any) -> None:
    reload_store = getattr(data_store, "reload", None)
    if callable(reload_store):
        reload_store()


def _load(data_store: Any) -> list[dict[str, Any]]:
    raw = data_store.get(_STORE_KEY, [])
    return [item for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def _save(data_store: Any, reminders: list[dict[str, Any]]) -> None:
    finished = [r for r in reminders if r.get("status") != "pending"]
    overflow = len(finished) - _MAX_RETAINED_FINISHED
    if overflow > 0:
        drop = {id(r) for r in finished[:overflow]}
        reminders = [r for r in reminders if id(r) not in drop]
    data_store.set(_STORE_KEY, reminders)
    data_store.save()


def _update(data_store: Any, reminder_id: str, **fields: Any) -> dict[str, Any] | None:
    """只改一条：先读回磁盘上的最新列表再写，避免盖掉发送期间别处做的改动。"""
    _reload(data_store)
    reminders = _load(data_store)
    for reminder in reminders:
        if reminder.get("id") == reminder_id:
            reminder.update(fields)
            _save(data_store, reminders)
            return reminder
    return None
