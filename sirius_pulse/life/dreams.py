"""Her dreams: one short dream a night, remembered the next morning.

A dream is fiction, and that decides every rule here:

- It is **never** written into her memory units.  Memory is what she believes
  happened; a dream retrieved later as a fact ("你上次说你开了火锅店") would be a
  lie she cannot tell apart from the truth.  Dreams live only in
  ``memory/life/dreams.json``, a short rolling list.
- Its material is her own day plus a few memory fragments from **one** chat at
  most, and the dream remembers which chat that was.  She only brings it up in
  that chat (or anywhere, if no chat went into it), so a dream cannot carry what
  was said in one group into another.
- She only remembers it in the morning.  By midday a dream is gone, like a
  person's.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sirius_pulse.life.clock import PHASE_MORNING, DaySchedule
from sirius_pulse.life.reflection import extract_json_object
from sirius_pulse.utils.json_io import atomic_write_json, read_json

_DREAMS_FILE = "dreams.json"
_MAX_KEPT = 30

MAX_DREAM_CHARS = 240
MAX_FEELING_CHARS = 40
_MIN_DREAM_CHARS = 8

#: She dreams a while after falling asleep, not the moment she lies down.
DREAM_AFTER_MINUTES = 90


def _clip(value: Any, limit: int) -> str:
    return " ".join(str(value or "").split())[:limit]


@dataclass(slots=True)
class Dream:
    """The dream of the night that ends the life date ``date``."""

    date: str
    text: str
    feeling: str = ""
    chat_id: str = ""
    dreamt_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.date,
            "text": self.text,
            "feeling": self.feeling,
            "chat_id": self.chat_id,
            "dreamt_at": self.dreamt_at,
        }

    @classmethod
    def from_dict(cls, data: Any) -> "Dream | None":
        if not isinstance(data, dict):
            return None
        date = str(data.get("date", "") or "")
        text = _clip(data.get("text"), MAX_DREAM_CHARS)
        if not date or not text:
            return None
        return cls(
            date=date,
            text=text,
            feeling=_clip(data.get("feeling"), MAX_FEELING_CHARS),
            chat_id=str(data.get("chat_id", "") or ""),
            dreamt_at=str(data.get("dreamt_at", "") or ""),
        )

    def can_tell_in(self, chat_id: str) -> bool:
        """Whether she may bring this dream up in *chat_id*.

        A dream built from one chat's fragments stays in that chat; a dream made
        only of her own day can be told anywhere.
        """
        return not self.chat_id or self.chat_id == str(chat_id or "")


class DreamStore:
    """``memory/life/dreams.json`` -- the last few dreams, newest last."""

    def __init__(self, life_dir: Path | str) -> None:
        self.path = Path(life_dir) / _DREAMS_FILE

    def load(self) -> list[Dream]:
        raw = read_json(self.path, default=None)
        items = raw.get("dreams") if isinstance(raw, dict) else None
        if not isinstance(items, list):
            return []
        return [dream for item in items if (dream := Dream.from_dict(item)) is not None]

    def get(self, life_date: str) -> Dream | None:
        for dream in reversed(self.load()):
            if dream.date == life_date:
                return dream
        return None

    def save(self, dream: Dream) -> None:
        kept = [item for item in self.load() if item.date != dream.date]
        kept.append(dream)
        atomic_write_json(self.path, {"dreams": [item.to_dict() for item in kept[-_MAX_KEPT:]]})


def is_dream_time(schedule: DaySchedule, now: datetime) -> bool:
    """Asleep, and long enough that she is past falling asleep.

    For a short night the wait shrinks to half of it, so she still dreams.
    """
    if not schedule.is_asleep(now):
        return False
    asleep_for = schedule.minutes_since_wake(now) - schedule.awake_span
    night = 24 * 60 - schedule.awake_span
    return asleep_for >= min(DREAM_AFTER_MINUTES, night // 2)


def last_nights_dream(store: DreamStore, schedule: DaySchedule, now: datetime) -> Dream | None:
    """The dream she woke up from this morning, while she still remembers it."""
    if schedule.phase(now) != PHASE_MORNING:
        return None
    yesterday = schedule.life_date(now - timedelta(days=1))
    return store.get(yesterday)


def parse_dream(text: str, *, date: str, chat_id: str = "") -> Dream | None:
    """Validate a dream reply.  Anything unusable means no dream tonight."""
    data = extract_json_object(text)
    if data is None:
        return None
    body = _clip(data.get("dream"), MAX_DREAM_CHARS)
    if len(body) < _MIN_DREAM_CHARS:
        return None
    return Dream(
        date=date,
        text=body,
        feeling=_clip(data.get("feeling"), MAX_FEELING_CHARS),
        chat_id=chat_id,
    )
