"""Where her life meets the chat engine.

The life loop itself runs in the ``autonomy`` tool's heartbeat.  This module is
the chat side of it, kept to three small hooks so the pipeline stays readable:

- :func:`reply_factor` scales participation: asleep, she only answers someone who
  really calls for her; worn out, she chimes in a little less.
- :func:`presence_context` is the 【此刻的你】 block for a chat turn.
- :func:`record_reply` counts how much she has talked today (it tires her) and
  notices when a reply woke her up at night.

Everything here is best-effort: a missing or broken life directory must never
break a chat reply, so failures fall back to "no effect".
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sirius_pulse.life import DaySchedule, life_dir, schedule_from_config
from sirius_pulse.life.presence import LifeSnapshot, render_presence
from sirius_pulse.life.presence import reply_factor as _snapshot_reply_factor
from sirius_pulse.life.presence import snapshot
from sirius_pulse.life.projects import ProjectStore
from sirius_pulse.life.state import LifeStateStore
from sirius_pulse.models.emotion import describe_mood

logger = logging.getLogger(__name__)

_AUTONOMY_TOOL = "autonomy"


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
