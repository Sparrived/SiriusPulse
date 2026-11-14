"""自我唤醒：过一会儿叫醒自己，回来看一件事。

她等部署跑完、等日志出结果、等一个文件生成时，以前只能在 bash 里 ``sleep`` 几秒再看
（两个月 490 次，大多发生在她自己的项目回合里），等得久一点就只能放下不管；crontab 能排
脚本，却排不了「到时候叫我回来想一想」。这里让她给自己定一个时间和一句备注：

- 到点时在原来的会话里开一个工作回合（走后台结果回调那条路，忙的时候自动往后挪），
  她只拿到那句备注和会话最近的上下文，所以备注要写清楚看什么、怎么判断、下一步做什么；
- 她自己发起的回合定的唤醒不能对外说话，碰上她睡觉会等她醒了再叫；聊天里定的照点叫；
- 只叫一次；要再等就在被叫醒的那一轮里再定一次。每天次数和同时挂着的个数都有上限，
  免得她一次次把自己叫醒空转；
- 查看与取消走 ``background_task``（ID 形如 ``wake_xxxxxxxx``）。

唤醒存在本工具的数据存储里（``tool_data/wake_me.json``），后台心跳每隔十几秒检查一次。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sirius_pulse.extension_runtime import BackgroundTaskSpec
from sirius_pulse.life.clock import is_quiet_hours, to_local
from sirius_pulse.tools.builtin.reminder import parse_local_time
from sirius_pulse.tools.models import ToolInvocationContext

logger = logging.getLogger(__name__)

TASK_PREFIX = "wake_"
_STORE_KEY = "wakes"
_TICK_SECONDS = 15.0
_MIN_DELAY = timedelta(minutes=1)
_MAX_DELAY = timedelta(days=7)
#: 进程停过、或者一直在等她醒：晚了这么久还没叫成就记为错过。
_MISSED_AFTER = timedelta(hours=24)
_MAX_NOTE_CHARS = 500
_MAX_ATTEMPTS = 3
_MAX_RETAINED_FINISHED = 100
_DEFAULT_MAX_PENDING = 10
_DEFAULT_MAX_PER_DAY = 24

PENDING = "pending"
WAKING = "waking"
DONE = "done"
CANCELLED = "cancelled"
MISSED = "missed"
FAILED = "failed"
INTERRUPTED = "interrupted"

#: 没有存储锁时（测试替身）退回到这把；真实运行时用存储自己的锁。
_fallback_lock = threading.RLock()


def _guard(data_store: Any) -> Any:
    """读改写要串起来：工具在线程里执行，心跳在事件循环上，background_task 也会来改。

    工具模块会被注册表和 background_task 各加载一份，模块级的锁不共享，所以用存储的锁。
    """
    return getattr(data_store, "lock", None) or _fallback_lock


_LEAD = (
    "【到点了】你之前给自己定了这个时间回来看一件事，你当时写的备注附在最后。"
    "这不是别人的新消息。按备注去检查：已经好了就接着做或收尾；还没好又值得再等，"
    "可以再用 wake_me 定一次；不值得等了就放下，并记下为什么。"
)

TOOL_META = {
    "name": "wake_me",
    "description": (
        "【适用场景】在等一件要几分钟到几小时才有结果的事（部署、构建、跑着的脚本、别人回复），"
        "或想晚点回来接着做时，给自己定个时间回来看看，不要用 bash 的 sleep 干等。\n"
        "【使用方法】in_minutes 填几分钟后（1 到 10080），或者 at 填本地时间（UTC+8，"
        "如 2026-11-15 20:00 或 20:00）；note 写清楚到时要看什么、怎么判断、下一步做什么——"
        "被叫醒时你只记得这句话和这个会话最近的上下文。\n"
        "【注意事项】到点会在当前会话里开一个工作回合叫醒你，只叫一次；"
        "你自己做事时定的唤醒碰上你睡觉会等你醒了再叫；只等几秒用 bash 的 sleep 就行；"
        "查看或取消用 background_task（ID 形如 wake_xxxxxxxx）。"
    ),
    "version": "1.0.0",
    # 只是给自己排一个回合，不对外发送，自主回合里也能用。
    "side_effect": "unknown",
    "retry_safe": False,
    "work_mode_only": True,
    "tags": ["wake", "schedule", "wait", "background"],
    "parameters": {
        "in_minutes": {
            "type": "integer",
            "description": "多少分钟后叫醒你（1 到 10080）。与 at 二选一。",
            "required": False,
            "default": 0,
        },
        "at": {
            "type": "string",
            "description": "叫醒你的本地时间（UTC+8），如 2026-11-15 20:00 或 20:00。与 in_minutes 二选一。",
            "required": False,
            "default": "",
        },
        "note": {
            "type": "string",
            "description": "到时要看什么、怎么判断、下一步做什么（500 字以内）。",
            "required": True,
        },
    },
    "config": {
        "max_pending": {
            "type": "int",
            "description": "同时最多挂着几个还没到点的唤醒，范围 1 到 50。",
            "default": _DEFAULT_MAX_PENDING,
            "group": "限制",
        },
        "max_per_day": {
            "type": "int",
            "description": "24 小时内最多定几次唤醒，范围 1 到 200。",
            "default": _DEFAULT_MAX_PER_DAY,
            "group": "限制",
        },
    },
}


def run(
    in_minutes: int = 0,
    at: str = "",
    note: str = "",
    *,
    data_store: Any = None,
    chat_context: dict[str, Any] | None = None,
    invocation_context: ToolInvocationContext | None = None,
    now: datetime | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    if data_store is None:
        return {"success": False, "error": "wake_me 需要可持久化的数据存储"}
    context = dict(chat_context or {})
    if not context and invocation_context is not None:
        context = dict(invocation_context.chat_context)
    group_id = (invocation_context.group_id if invocation_context else "") or str(
        context.get("group_id") or ""
    )
    if not group_id:
        return {"success": False, "error": "唤醒要在一个具体的会话里设置，到点才知道在哪儿叫你"}
    text = str(note or "").strip()
    if not text:
        return {"success": False, "error": "note 不能为空：写清楚到时要看什么"}
    if len(text) > _MAX_NOTE_CHARS:
        return {"success": False, "error": f"note 太长了，{_MAX_NOTE_CHARS} 字以内"}

    current = now or datetime.now(timezone.utc)
    try:
        due = _due_time(in_minutes, at, current)
    except ValueError as exc:
        return {"success": False, "error": str(exc)}

    caller = invocation_context.caller if invocation_context is not None else None
    with _guard(data_store):
        _refresh(data_store)
        wakes = _load(data_store)
        pending = [w for w in wakes if w.get("status") == PENDING]
        limit = _config_int(data_store, "max_pending", _DEFAULT_MAX_PENDING, 1, 50)
        if len(pending) >= limit:
            return {
                "success": False,
                "error": f"已经挂着 {len(pending)} 个唤醒了（上限 {limit}），先用 background_task 看看或取消",
            }
        day_ago = current - timedelta(days=1)
        recent = [w for w in wakes if (_parse(w.get("created_at")) or day_ago) > day_ago]
        per_day = _config_int(data_store, "max_per_day", _DEFAULT_MAX_PER_DAY, 1, 200)
        if len(recent) >= per_day:
            return {
                "success": False,
                "error": f"24 小时内已经定了 {len(recent)} 次唤醒（上限 {per_day}），这件事先放一放",
            }
        wake = {
            "id": f"{TASK_PREFIX}{uuid.uuid4().hex[:8]}",
            "status": PENDING,
            "note": text,
            "due_at": due.isoformat(),
            "created_at": current.isoformat(),
            "group_id": group_id,
            "user_id": str(getattr(caller, "user_id", "") or ""),
            "user_name": str(getattr(caller, "name", "") or ""),
            "adapter_type": str(
                context.get("adapter_type")
                or (invocation_context.adapter_type if invocation_context else "")
                or ""
            ),
            "caller_is_developer": bool(
                invocation_context is not None and invocation_context.caller_is_developer
            ),
            "self_initiated": bool(
                invocation_context is not None and invocation_context.self_initiated
            ),
            "attempts": 0,
        }
        wakes.append(wake)
        _store(data_store, wakes)
    return {
        "success": True,
        "text_blocks": [
            f"好，{_label(due)} 叫醒你（{wake['id']}）。到时你会看到这句备注：{text}" "\n现在可以先放下这件事；不用再 sleep 等它。"
        ],
        "data": {"wake": describe(wake)},
    }


def _due_time(in_minutes: Any, at: Any, now: datetime) -> datetime:
    minutes = _int(in_minutes, 0)
    at_text = str(at or "").strip()
    if minutes and at_text:
        raise ValueError("in_minutes 和 at 只填一个")
    if minutes:
        due = now + timedelta(minutes=minutes)
    elif at_text:
        due = parse_local_time(at_text, now)
    else:
        raise ValueError("要填 in_minutes（几分钟后）或 at（几点）")
    if due - now < _MIN_DELAY:
        raise ValueError("至少要等 1 分钟；只等几秒用 bash 的 sleep 就行")
    if due - now > _MAX_DELAY:
        raise ValueError("最多只能定在 7 天以内")
    return due


# ---------------------------------------------------------------------------
# 后台：到点叫醒
# ---------------------------------------------------------------------------


def create_background_tasks(ctx: Any) -> list[BackgroundTaskSpec]:
    data_store = ctx.get_data_store("wake_me")
    recover_interrupted(data_store)

    async def _tick() -> None:
        await deliver_due(ctx, data_store)

    return [BackgroundTaskSpec(name="wake_me_due", interval_seconds=_TICK_SECONDS, task_func=_tick)]


def recover_interrupted(data_store: Any) -> int:
    """进程重启时还在「叫醒中」的：那一轮可能已经做了事，不再重跑，记为中断。"""
    with _guard(data_store):
        _refresh(data_store)
        wakes = _load(data_store)
        stuck = [w for w in wakes if w.get("status") == WAKING]
        for wake in stuck:
            wake["status"] = INTERRUPTED
            wake["finished_at"] = datetime.now(timezone.utc).isoformat()
        if stuck:
            _store(data_store, wakes)
    return len(stuck)


async def deliver_due(ctx: Any, data_store: Any, now: datetime | None = None) -> str | None:
    """叫醒一个到点的；一次心跳最多叫一个（每次都是一整轮模型回合）。

    返回这次叫的那个唤醒的新状态（还要再叫时是 ``pending``）；没有要叫的返回 ``None``。
    """
    current = now or datetime.now(timezone.utc)
    with _guard(data_store):
        _refresh(data_store)
        wakes = _load(data_store)
        changed = False
        target: dict[str, Any] | None = None
        asleep: bool | None = None
        for wake in sorted(wakes, key=lambda w: str(w.get("due_at"))):
            if wake.get("status") != PENDING:
                continue
            due = _parse(wake.get("due_at"))
            if due is None or due > current:
                continue
            if current - due > _MISSED_AFTER:
                wake["status"] = MISSED
                wake["finished_at"] = current.isoformat()
                changed = True
                logger.warning("唤醒错过了时间，不再补叫: %s", wake.get("id"))
                continue
            if wake.get("self_initiated"):
                if asleep is None:
                    asleep = _asleep(ctx, current)
                if asleep:
                    continue
            target = wake
            break
        if target is not None:
            target["status"] = WAKING
            changed = True
        if changed:
            data_store.set(_STORE_KEY, wakes)
    if changed:
        await asyncio.to_thread(data_store.save)
    if target is None:
        return None

    tools_ran = [False]
    try:
        status, attempts = await _wake(ctx, target, current, tools_ran)
    except asyncio.CancelledError:
        # 关停打断：已经动过工具就不再重跑，没动过的下次照叫。
        _settle(data_store, target, INTERRUPTED if tools_ran[0] else PENDING, None)
        data_store.save()
        raise
    _settle(data_store, target, status, attempts)
    await asyncio.to_thread(data_store.save)
    return status


def _settle(data_store: Any, target: dict[str, Any], status: str, attempts: int | None) -> None:
    with _guard(data_store):
        wakes = _load(data_store)
        for wake in wakes:
            if wake.get("id") != target.get("id"):
                continue
            wake["status"] = status
            if attempts is not None:
                wake["attempts"] = attempts
            if status != PENDING:
                wake["finished_at"] = datetime.now(timezone.utc).isoformat()
        _store(data_store, wakes, save=False)


async def _wake(
    ctx: Any, wake: dict[str, Any], now: datetime, tools_ran: list[bool]
) -> tuple[str, int]:
    """开一轮回合；返回（新状态, 尝试次数）。"""
    attempts = int(wake.get("attempts") or 0)

    def before_tool() -> None:
        tools_ran[0] = True

    created = _parse(wake.get("created_at"))
    note = str(wake.get("note") or "")
    try:
        payload = await ctx.handle_background_tool_result(
            callback_id=f"wake_me:{wake.get('id')}",
            tool_name="wake_me",
            outcome="success",
            request_summary=f"（唤醒 {wake.get('id')}）{note}",
            output=(
                f"你在 {_label(created) if created else '之前'} 给自己定了这次唤醒，备注是：\n{note}\n"
                f"现在是 {_label(now)}。"
            ),
            group_id=str(wake.get("group_id") or ""),
            user_id=str(wake.get("user_id") or ""),
            user_name=str(wake.get("user_name") or ""),
            adapter_type=str(wake.get("adapter_type") or ""),
            caller_is_developer=bool(wake.get("caller_is_developer")),
            self_initiated=bool(wake.get("self_initiated")),
            before_tool=before_tool,
            lead=_LEAD,
        )
    except Exception as exc:
        attempts += 1
        logger.warning("唤醒回合失败 (%s): %s", wake.get("id"), exc)
        if tools_ran[0]:
            return INTERRUPTED, attempts
        return (FAILED if attempts >= _MAX_ATTEMPTS else PENDING), attempts
    if isinstance(payload, dict) and payload.get("status") == "deferred":
        # 会话正忙（有人在聊、或者别的回合在跑）：下一次心跳再叫。
        return PENDING, attempts
    if not isinstance(payload, dict) or payload.get("status") != "completed":
        attempts += 1
        return (FAILED if attempts >= _MAX_ATTEMPTS else PENDING), attempts
    return DONE, attempts


def _asleep(ctx: Any, now: datetime) -> bool:
    get_schedule = getattr(ctx, "get_day_schedule", None)
    schedule = None
    if callable(get_schedule):
        try:
            schedule = get_schedule()
        except Exception:
            logger.debug("读取作息失败，按默认夜间时段判断", exc_info=True)
    return is_quiet_hours(now, schedule)


# ---------------------------------------------------------------------------
# 给 background_task 用：列出与取消
# ---------------------------------------------------------------------------


def list_wakes(data_store: Any) -> list[dict[str, Any]]:
    with _guard(data_store):
        _refresh(data_store)
        return [dict(w) for w in _load(data_store)]


def describe(wake: dict[str, Any]) -> dict[str, Any]:
    due = _parse(wake.get("due_at"))
    status = str(wake.get("status") or "")
    return {
        "task_id": str(wake.get("id") or ""),
        "kind": "wake_me",
        "status": "waiting" if status == PENDING else status,
        "group_id": str(wake.get("group_id") or ""),
        "due_at": str(wake.get("due_at") or ""),
        "prompt": f"{_label(due) if due else '时间未知'} 叫醒：{str(wake.get('note') or '')[:100]}",
    }


def cancel(data_store: Any, wake_id: str) -> dict[str, Any] | None:
    """取消一个还没叫的唤醒；返回取消后的描述，找不到返回 None。"""
    with _guard(data_store):
        _refresh(data_store)
        wakes = _load(data_store)
        for wake in wakes:
            if wake.get("id") != wake_id:
                continue
            if wake.get("status") == PENDING:
                wake["status"] = CANCELLED
                wake["finished_at"] = datetime.now(timezone.utc).isoformat()
                _store(data_store, wakes)
                return {**describe(wake), "stopped": True}
            return {**describe(wake), "stopped": False}
    return None


# ---------------------------------------------------------------------------
# 存储
# ---------------------------------------------------------------------------


def _refresh(data_store: Any) -> None:
    reload_if_changed = getattr(data_store, "reload_if_changed", None)
    if callable(reload_if_changed):
        reload_if_changed()


def _load(data_store: Any) -> list[dict[str, Any]]:
    raw = data_store.get(_STORE_KEY, [])
    return [dict(item) for item in raw if isinstance(item, dict)] if isinstance(raw, list) else []


def _store(data_store: Any, wakes: list[dict[str, Any]], *, save: bool = True) -> None:
    finished = [w for w in wakes if w.get("status") not in {PENDING, WAKING}]
    overflow = len(finished) - _MAX_RETAINED_FINISHED
    if overflow > 0:
        drop = {id(w) for w in finished[:overflow]}
        wakes = [w for w in wakes if id(w) not in drop]
    data_store.set(_STORE_KEY, wakes)
    if save:
        data_store.save()


def _config_int(data_store: Any, key: str, default: int, low: int, high: int) -> int:
    return max(low, min(high, _int(data_store.get(key, default), default)))


def _parse(value: Any) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _label(moment: datetime) -> str:
    local = to_local(moment)
    weekday = "一二三四五六日"[local.weekday()]
    return f"{local.month}月{local.day}日（周{weekday}）{local:%H:%M}"


def _int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
