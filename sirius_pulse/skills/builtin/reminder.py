"""Built-in skill for creating timed reminders.

Supports both active (model-callable) and passive (background task) modes.
- Active: AI calls [SKILL_CALL: reminder | {...}] to create/list/cancel.
- Passive: create_background_tasks() registers a periodic checker that
  scans for due reminders, generates persona-styled messages, and queues
  them for delivery.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sirius_pulse.config.config_builder import ConfigBuilder

logger = logging.getLogger(__name__)

_config = ConfigBuilder()
_config.group("基础操作").add(
    "action",
    type="str",
    description="操作类型: create(创建) / list(查看所有提醒) / cancel(取消指定提醒)",
    required=True,
)
_config.group("基础操作").add(
    "content",
    type="str",
    description="提醒内容，格式为'我要提醒 <提醒人>（自己或用户名） <提醒内容>'",
)
_config.group("触发模式").add(
    "mode",
    type="str",
    description="触发模式: once(一次性) / interval(每隔N分钟重复) / daily(每日重复) / weekly(每周重复)",
    default="once",
)
_config.group("触发模式").add(
    "minutes_after",
    type="int",
    description="几分钟后触发（once/interval 模式必填；once 为一次性，interval 为重复间隔）",
)
_config.group("触发模式").add(
    "trigger_at",
    type="str",
    description="绝对触发时间 ISO 格式（仅 once 模式，与 minutes_after 二选一）",
)
_config.group("触发模式").add(
    "time",
    type="str",
    description="触发时间 HH:MM，例如 08:00、21:30（daily/weekly 模式必填）",
)
_config.group("触发模式").add(
    "weekdays",
    type="list[int]",
    description="星期列表 [0,1,2,3,4,5,6]，0=周一, 6=周日（仅 weekly 模式必填，支持多选如 [0,2,4] 表示周一三五）",
)
_config.group("管理").add(
    "reminder_id",
    type="str",
    description="提醒任务ID（cancel 时使用，可通过 list 查看）",
)
_config.group("高级设置").add(
    "target",
    type="str",
    description="提醒对象: user(提醒用户去做，默认) / self(提醒你自己去做这件事并告知用户)",
    default="user",
)
_config.group("高级设置").add(
    "skill_chain",
    type="list",
    description=(
        "触发提醒时预先执行的 SKILL 调用链，每项为 {\"skill\":\"name\",\"params\":{...}}。"
        "执行结果会作为上下文输入给模型，供生成提醒消息时参考。"
    ),
)
_config.group("高级设置").add(
    "adapter_type",
    type="str",
    description="指定提醒消息通过哪个 adapter 发送，例如 'napcat'。留空则自动使用创建时的 adapter。",
    default="",
)

SKILL_META = {
    "name": "reminder",
    "description": (
        "设置定时提醒，支持一次性、间隔重复、每日、每周重复提醒。"
        "到达指定时间后会通知对应的用户。"
        "可以用 list 查看所有提醒，用 cancel 取消指定提醒。"
    ),
    "version": "2.0.0",
    "tags": ["utility", "time"],
    "developer_only": False,
    "dependencies": [],
    "parameters": _config.build(),
}


# ── Active: model-callable run() ──────────────────────────────────────

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


# ── Passive: background task factory ──────────────────────────────────

def create_background_tasks(ctx: Any) -> list[Any]:
    """Register a periodic reminder checker as a background task.

    Returns a BackgroundTaskSpec that polls for due reminders every
    10 seconds (configurable via 'reminder_check_interval_seconds').
    """
    from sirius_pulse.skills.models import BackgroundTaskSpec

    async def _check_due_reminders() -> None:
        await _check_and_fire_reminders(ctx)

    interval = ctx.get_config_value("reminder_check_interval_seconds", 10)

    return [BackgroundTaskSpec(
        name="reminder_check",
        interval_seconds=interval,
        task_func=_check_due_reminders,
    )]


async def _check_and_fire_reminders(ctx: Any) -> None:
    """Scan reminders and queue due ones for delivery."""
    store = ctx.get_data_store("reminder")
    reminders = list(store.get("reminders", []))
    now = datetime.now(timezone.utc)
    triggered: list[tuple[str, str, str, str, str, str, list[dict[str, Any]]]] = []
    remaining: list[dict[str, Any]] = []

    for r in reminders:
        if _is_reminder_due(r, now):
            gid = r.get("group_id")
            if gid:
                content = r.get("content", "提醒时间到啦")
                user_id = r.get("user_id", "")
                user_name = r.get("user_name", "")
                adapter_type = r.get("adapter_type", "")
                target = r.get("target", "user")
                skill_chain = r.get("skill_chain")
                skill_results: list[dict[str, Any]] = []
                if skill_chain:
                    skill_results = await _execute_skill_chain(
                        ctx, gid, user_id, user_name, r.get("id"), skill_chain
                    )
                triggered.append(
                    (gid, content, user_id, user_name, adapter_type, target, skill_results)
                )
                r["last_fired_at"] = now.isoformat()
                r["fire_count"] = r.get("fire_count", 0) + 1
                mode = r.get("mode", "once")
                if mode == "once":
                    continue
                if mode == "interval":
                    interval = r.get("minutes_after", 1)
                    next_fire = now + timedelta(minutes=interval)
                    r["fire_at"] = next_fire.isoformat()
            else:
                logger.warning("Reminder %s has no group_id, skipping", r.get("id"))
        remaining.append(r)

    if len(remaining) != len(reminders):
        store.set("reminders", remaining)
        store.save()

    for gid, content, user_id, user_name, adapter_type, target, skill_results in triggered:
        reply = await _generate_reminder_message(
            ctx, gid, content, user_id, user_name, target, skill_results
        )
        if reply:
            ctx.queue_pending_message(gid, reply, adapter_type)
            ctx.log_inner_thought(f"AI 生成提醒：{reply[:40]}")
            if gid.startswith("private_"):
                ctx.activate_private_group(gid)
            await ctx.emit_event(
                "reminder_triggered",
                {"group_id": gid, "reply": reply, "adapter_type": adapter_type},
            )


async def _execute_skill_chain(
    ctx: Any,
    group_id: str,
    user_id: str,
    user_name: str,
    reminder_id: str | None,
    skill_chain: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Execute a skill chain defined in a reminder."""
    executor = ctx.skill_executor
    if executor is None:
        return []

    from sirius_pulse.skills.models import SkillInvocationContext
    from sirius_pulse.memory.user.unified_models import UnifiedUser

    caller = UnifiedUser(user_id=user_id, name=user_name)
    inv_ctx = SkillInvocationContext(caller=caller)

    logger.info("Reminder %s skill_chain start: %d items", reminder_id, len(skill_chain))
    skill_results: list[dict[str, Any]] = []
    for item in skill_chain:
        if not isinstance(item, dict):
            continue
        skill_name = item.get("skill", "")
        params = item.get("params", {}) or {}
        if not skill_name:
            continue
        try:
            skill = ctx.skill_registry.get(skill_name)
            if skill is None:
                logger.warning("Reminder %s skill '%s' not found", reminder_id, skill_name)
                skill_results.append({"skill": skill_name, "params": params, "error": "未找到"})
                continue
            result = await executor.execute_async(
                skill, params, invocation_context=inv_ctx
            )
            skill_results.append({
                "skill": skill_name,
                "params": params,
                "result": result.to_display_text() if result.success else result.error,
            })
        except Exception as exc:
            logger.warning("Reminder %s skill_chain failed: %s -> %s", reminder_id, skill_name, exc)
            skill_results.append({"skill": skill_name, "params": params, "error": str(exc)})

    logger.info(
        "Reminder %s skill_chain finished: %d/%d succeeded",
        reminder_id,
        sum(1 for sr in skill_results if "error" not in sr),
        len(skill_results),
    )
    return skill_results


async def _generate_reminder_message(
    ctx: Any,
    group_id: str,
    content: str,
    user_id: str,
    user_name: str,
    target: str = "user",
    skill_results: list[dict[str, Any]] | None = None,
) -> str | None:
    """Generate a persona-styled reminder message via LLM.

    Delegates memory recording, timestamp persistence, and sticker
    sending to engine post-hooks (post_process=True).
    """
    from sirius_pulse.core.prompt_factory import PromptFactory

    try:
        persona = ctx.get_persona()
        identity = persona.build_system_prompt() if persona else ""
        skill_desc = ctx.get_skill_descriptions(caller_is_developer=False)
        system_prompt, messages = PromptFactory.build_reminder_sections(
            identity=identity,
            content=content,
            user_name=user_name,
            user_id=user_id,
            target=target,
            skill_results=skill_results,
            skill_desc=skill_desc,
        )

        reply = await ctx.generate_text(
            system_prompt, messages, group_id,
            task_name="proactive_generate",
            post_process=True,
        )
        reply = reply.strip()
        return reply or None
    except Exception as exc:
        logger.warning("Failed to generate reminder message: %s", exc)
        return None


# ── Reminder due-detection ────────────────────────────────────────────

def _is_reminder_due(reminder: dict[str, Any], now: datetime) -> bool:
    """Check whether a single reminder should fire at *now*."""
    mode = reminder.get("mode", "once")
    if mode == "once":
        fire_at_str = reminder.get("fire_at")
        if not fire_at_str:
            return False
        try:
            fire_at = datetime.fromisoformat(str(fire_at_str).replace("Z", "+00:00"))
        except ValueError:
            return False
        return now >= fire_at

    if mode == "interval":
        fire_at_str = reminder.get("fire_at")
        if not fire_at_str:
            return False
        try:
            fire_at = datetime.fromisoformat(str(fire_at_str).replace("Z", "+00:00"))
        except ValueError:
            return False
        if now < fire_at:
            return False
        last_fired = reminder.get("last_fired_at")
        if last_fired:
            try:
                last_dt = datetime.fromisoformat(str(last_fired).replace("Z", "+00:00"))
                if (now - last_dt).total_seconds() < 60:
                    return False
            except ValueError:
                logger.warning("解析上次提醒时间失败", exc_info=True)
                pass
        return True

    if mode in ("daily", "weekly"):
        time_str = reminder.get("time", "")
        if not time_str or ":" not in time_str:
            return False
        try:
            h, m = map(int, str(time_str).split(":"))
        except ValueError:
            return False
        now_local = now.astimezone()
        if now_local.hour != h or now_local.minute != m:
            return False
        last_fired = reminder.get("last_fired_at")
        if last_fired:
            try:
                last_dt = datetime.fromisoformat(str(last_fired).replace("Z", "+00:00"))
                last_local = last_dt.astimezone()
                if (
                    last_local.year == now_local.year
                    and last_local.month == now_local.month
                    and last_local.day == now_local.day
                    and last_local.hour == now_local.hour
                    and last_local.minute == now_local.minute
                ):
                    return False
            except ValueError:
                pass
        if mode == "weekly":
            weekdays = reminder.get("weekdays")
            if weekdays is not None:
                if now_local.weekday() not in [int(d) for d in weekdays]:
                    return False
            else:
                weekday = reminder.get("weekday")
                if weekday is not None and now_local.weekday() != int(weekday):
                    return False
        return True

    return False


# ── CRUD helpers ──────────────────────────────────────────────────────

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
        who = f"【{r.get('user_name') or r.get('user_id', '?')}】"
        detail = f"【{r['id']}】{who}{mode_desc} | {r['content']}"
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


# ── Utilities ─────────────────────────────────────────────────────────

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
    if value is None:
        return []
    if isinstance(value, int):
        if 0 <= value <= 6:
            return [value]
        return []
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
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
