"""Where her life meets the chat engine.

The life loop itself runs in the ``autonomy`` tool's heartbeat.  This module is
the chat side of it, kept to a few small hooks so the pipeline stays readable:

- :func:`reply_factor` scales participation: asleep, she only answers someone who
  really calls for her; worn out, she chimes in a little less.
- :func:`presence_context` is the 【此刻的你】 block for a chat turn.
- :func:`record_reply` counts how much she has talked today (it tires her) and
  notices when a reply woke her up at night.
- :func:`home_overview` is the 【你的家目录 ~】 glance handed to her when she starts
  working, so she does not open every turn with ``ls -la ~``.

Everything here is best-effort: a missing or broken life directory must never
break a chat reply, so failures fall back to "no effect".
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sirius_pulse.life import DaySchedule, life_dir, schedule_from_config
from sirius_pulse.life.home import summarize_home
from sirius_pulse.life.presence import LifeSnapshot, render_presence
from sirius_pulse.life.presence import reply_factor as _snapshot_reply_factor
from sirius_pulse.life.presence import snapshot
from sirius_pulse.life.projects import ProjectStore
from sirius_pulse.life.state import LifeStateStore
from sirius_pulse.models.emotion import describe_mood

logger = logging.getLogger(__name__)

_AUTONOMY_TOOL = "autonomy"


def day_schedule(engine: Any) -> DaySchedule:
    """她的作息；没配置或读不到时用默认作息。"""
    return _schedule(engine)


def _schedule(engine: Any) -> DaySchedule:
    executor = getattr(engine, "_tool_executor", None)
    if executor is None:
        return DaySchedule()
    try:
        return schedule_from_config(executor.get_data_store(_AUTONOMY_TOOL))
    except Exception:
        return DaySchedule()


def _work_path(engine: Any) -> str:
    return str(getattr(engine, "work_path", "") or "")


def _replies_today(engine: Any, schedule: DaySchedule, now: datetime) -> int:
    counter = getattr(engine, "_life_replies", None)
    if not isinstance(counter, dict) or counter.get("date") != schedule.life_date(now):
        return 0
    return int(counter.get("count", 0) or 0)


def _snapshot(
    engine: Any, now: datetime, chat_id: str = ""
) -> tuple[LifeSnapshot, DaySchedule] | None:
    work_path = _work_path(engine)
    if not work_path:
        return None
    schedule = _schedule(engine)
    snap = snapshot(
        life_dir(work_path),
        schedule,
        now,
        replies_today=_replies_today(engine, schedule, now),
        chat_id=chat_id,
    )
    return snap, schedule


def reply_factor(engine: Any, now: datetime | None = None) -> float:
    """Multiplier on the participation score (1.0 when there is nothing to say)."""
    try:
        result = _snapshot(engine, now or datetime.now(timezone.utc))
    except Exception:
        logger.debug("读取生活状态失败，参与度不受影响", exc_info=True)
        return 1.0
    return _snapshot_reply_factor(result[0]) if result else 1.0


def _mood_label(engine: Any) -> str:
    emotion = getattr(engine, "assistant_emotion", None)
    if emotion is None:
        return ""
    return describe_mood(emotion.valence, emotion.arousal)


def presence_context(engine: Any, now: datetime | None = None, *, chat_id: str = "") -> str:
    """The 【此刻的你】 block for a turn in *chat_id*, or "" when unavailable."""
    now = now or datetime.now(timezone.utc)
    try:
        result = _snapshot(engine, now, chat_id)
        if result is None:
            return ""
        snap, schedule = result
        return render_presence(
            snap,
            schedule,
            now,
            mood=_mood_label(engine),
            projects=ProjectStore(life_dir(_work_path(engine))),
        )
    except Exception:
        logger.debug("生成此刻状态失败，本轮不注入", exc_info=True)
        return ""


def record_reply(engine: Any, now: datetime | None = None) -> None:
    """Count one reply she actually sent; remember it if it woke her up."""
    now = now or datetime.now(timezone.utc)
    try:
        schedule = _schedule(engine)
        today = schedule.life_date(now)
        counter = getattr(engine, "_life_replies", None)
        if not isinstance(counter, dict) or counter.get("date") != today:
            counter = {"date": today, "count": 0}
        counter["count"] = int(counter.get("count", 0)) + 1
        engine._life_replies = counter

        work_path = _work_path(engine)
        if work_path and schedule.is_asleep(now):
            store = LifeStateStore(life_dir(work_path))
            # One wake-up per stretch: a burst of replies is one interrupted night.
            if not store.load().woken_recently(now):
                store.update(lambda state: state.record_woken(now))
    except Exception:
        logger.debug("记录回复对生活状态的影响失败", exc_info=True)


#: 家目录概览缓存多久：一次工作里几轮回合、几次回调接连开工时不必每次都重新遍历。
HOME_OVERVIEW_TTL_SECONDS = 300.0


async def home_overview(engine: Any, now: datetime | None = None) -> str:
    """开工前给她的家目录概览；读不到就返回空串，不影响回合本身。

    遍历家目录是文件系统 I/O（线上几千个文件），所有人格共用一个事件循环，所以放到
    线程里做，并按人格缓存几分钟。
    """
    work_path = _work_path(engine)
    if not work_path:
        return ""
    cache = getattr(engine, "_home_overview_cache", None)
    clock = time.monotonic()
    if isinstance(cache, tuple) and clock - cache[0] < HOME_OVERVIEW_TTL_SECONDS:
        return cache[1]
    current = now or datetime.now(timezone.utc)
    try:
        text = await asyncio.to_thread(summarize_home, Path(work_path) / "home", now=current)
    except Exception:
        logger.debug("生成家目录概览失败", exc_info=True)
        return ""
    try:
        engine._home_overview_cache = (clock, text)
    except AttributeError:
        pass
    return text
