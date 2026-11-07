"""How her life shows up in a conversation.

Two outputs, both cheap and rule-based:

- :func:`render_presence` -- the 【此刻的你】 block appended to a chat turn's
  reference context: time of day, energy, mood, what she was just doing, what is
  left on today's plan, how she has been lately, and -- in the morning, in the
  chat it came from -- last night's dream.  This is what makes everything
  she does on her own *visible*; without it the rest of the life loop would be
  invisible to the people she talks to.
- :func:`reply_factor` -- a multiplier on the participation score, so a sleeping
  persona only answers someone who really calls for her and a tired one chimes in
  a little less.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sirius_pulse.life.agenda import AgendaStore
from sirius_pulse.life.clock import PHASE_LABELS, DaySchedule, to_local
from sirius_pulse.life.dreams import Dream, DreamStore, last_nights_dream
from sirius_pulse.life.projects import ProjectStore
from sirius_pulse.life.reflection import SelfNoteStore
from sirius_pulse.life.state import LifeStateStore, energy_at, energy_word

# Asleep: only a strongly addressed message clears the participation threshold.
ASLEEP_REPLY_FACTOR = 0.35
TIRED_REPLY_FACTOR = 0.85
_TIRED_BELOW = 0.3

_WOKEN_WITHIN_SECONDS = 12 * 60 * 60


@dataclass(frozen=True, slots=True)
class LifeSnapshot:
    """Everything the chat side needs, read once per turn."""

    phase: str
    asleep: bool
    energy: float
    activity: str
    remaining: list[str]
    recent: str
    woken_last_night: bool
    work_done: int
    dream: Dream | None = None


def snapshot(
    life_dir: Path | str,
    schedule: DaySchedule,
    now: datetime,
    *,
    replies_today: int = 0,
    chat_id: str = "",
) -> LifeSnapshot:
    """Read her life once.  *chat_id* is the chat she is talking in; last night's
    dream only shows up there if that is where the dream came from."""
    life_dir = Path(life_dir)
    state = LifeStateStore(life_dir).load()
    agenda = AgendaStore(life_dir).load(schedule.life_date(now))
    work_done = agenda.work_done() if agenda else 0
    woken = state.woken_count(now, within_seconds=_WOKEN_WITHIN_SECONDS)
    energy = energy_at(
        schedule,
        now,
        work_done_today=work_done,
        replies_today=replies_today,
        woken_last_night=woken,
    )
    remaining = (
        [f"{block.start} {block.activity}" for block in agenda.remaining(schedule, now)[:2]]
        if agenda
        else []
    )
    dream = last_nights_dream(DreamStore(life_dir), schedule, now)
    return LifeSnapshot(
        phase=schedule.phase(now),
        asleep=schedule.is_asleep(now),
        energy=energy,
        activity=state.current_activity(now),
        remaining=remaining,
        recent=SelfNoteStore(life_dir).load().recent,
        woken_last_night=woken > 0,
        work_done=work_done,
        dream=dream if dream is not None and dream.can_tell_in(chat_id) else None,
    )


def reply_factor(snap: LifeSnapshot) -> float:
    if snap.asleep:
        return ASLEEP_REPLY_FACTOR
    if snap.energy < _TIRED_BELOW:
        return TIRED_REPLY_FACTOR
    return 1.0


def render_presence(
    snap: LifeSnapshot,
    schedule: DaySchedule,
    now: datetime,
    *,
    mood: str = "",
    projects: ProjectStore | None = None,
) -> str:
    local = to_local(now)
    lines = ["【此刻的你】"]
    if snap.asleep:
        lines.append(
            f"- 现在 {local:%H:%M}，你平时 {schedule.sleep} 睡、{schedule.wake} 起，"
            "本来已经睡下了，是被消息吵醒的，还迷迷糊糊的。回得短一点、带点困意都很自然。"
        )
    else:
        lines.append(
            f"- 现在是{PHASE_LABELS.get(snap.phase, '白天')} {local:%H:%M}"
            f"（你平时 {schedule.sleep} 睡、{schedule.wake} 起）。"
        )
        if snap.woken_last_night:
            lines.append("- 昨晚被吵醒过，今天有点没睡够。")
    lines.append(f"- 精力：{energy_word(snap.energy)}。")
    if mood:
        lines.append(f"- 心情：{mood}。")
    if snap.activity and not snap.asleep:
        lines.append(f"- 刚才在做：{snap.activity}。")
    if snap.remaining and not snap.asleep:
        lines.append("- 今天还打算：" + "；".join(snap.remaining) + "。")
    if projects is not None:
        active = projects.active()
        if active:
            lines.append("- 最近手上的事：" + "、".join(f"{p.kind_label}「{p.title}」" for p in active) + "。")
    if snap.recent:
        lines.append(f"- 近况：{snap.recent}")
    if snap.dream is not None and not snap.asleep:
        feeling = f"醒来时{snap.dream.feeling}" if snap.dream.feeling else ""
        lines.append(
            f"- 昨晚做了个梦，还记得一点：{snap.dream.text}"
            + (f"（{feeling}）" if feeling else "")
            + "。梦是假的，里面的事没有真的发生过；想起来可以随口提一句，别当真事讲。"
        )
    lines.append("这些是你自己的生活，合适的时候可以自然带出来，不必每次都提，也别像汇报待办一样念出来。")
    return "\n".join(lines)
