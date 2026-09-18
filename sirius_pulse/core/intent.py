"""Intention: the persisted reason a persona does something on her own.

An *intention* is what makes autonomy motivated rather than scheduled.  It is
created when she runs into something, and it survives across ticks: the next
tick does not ask "am I bored yet", it asks "is there anything I am still
carrying".  Without this, the trigger degrades into a restlessness timer and
``kind`` collapses into "finish a task".

Two resolutions, deliberately the same shape:

``do``
    She wants to work something out — read it, try it, write it down.
``tell``
    She wants to say something to someone — share, ask, discuss.  ``audience``
    records who, because "想说给谁" is part of the intention, not an
    afterthought; a piece of news meant for one friend is not broadcast.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

# Intention life stages.  ``nascent`` is the interesting one: she noticed
# something but has not committed to acting on it yet.
STATUS_NASCENT = "nascent"
STATUS_ACTIVE = "active"
STATUS_RESOLVED = "resolved"
STATUS_DROPPED = "dropped"

RESOLUTION_DO = "do"
RESOLUTION_TELL = "tell"

# An unfulfilled intention should not nag forever; it fades out on its own.
_DEFAULT_TTL_SECONDS = 3 * 24 * 60 * 60

# Urgency is a property of the intention, not of the clock: how much she still
# wants this.  It decays rather than grows, so ticking alone never manufactures
# motivation.
_DEFAULT_URGENCY = 0.6

# How many times she may spend a model turn on the same intention before leaving
# it.  The heartbeat interval spaces these out; the cap only stops one
# never-finished intention from costing a turn on every beat forever.
_MAX_ATTEMPTS = 3


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_time(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return datetime(1970, 1, 1, tzinfo=timezone.utc)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass(slots=True)
class Intention:
    """Something she is carrying, with a reason to act on it later."""

    intention_id: str
    created_at: str
    what: str
    why: str = ""
    resolution: str = RESOLUTION_DO
    status: str = STATUS_NASCENT
    kind: str = "musing"
    audience: str = ""
    audience_label: str = ""
    urgency: float = _DEFAULT_URGENCY
    source: str = ""
    origin_group: str = ""
    resolved_at: str = ""
    shared_at: str = ""
    share_count: int = 0
    attempts: int = 0
    last_attempt_at: str = ""
    refs: list[str] = field(default_factory=list)
    outcome: str = ""

    @property
    def is_open(self) -> bool:
        return self.status in {STATUS_NASCENT, STATUS_ACTIVE} and not self.is_spent

    @property
    def is_tell(self) -> bool:
        return self.resolution == RESOLUTION_TELL

    @property
    def is_spent(self) -> bool:
        """Whether she has already tried this enough times to leave it alone.

        Without this, dropping the pacing cooldown would let one intention she
        never finishes (or keeps declining) buy a model turn on every heartbeat.
        Being able to act at any time must not mean re-deciding the same thing
        forever — that is how autonomy turns back into a loop.
        """
        return self.attempts >= _MAX_ATTEMPTS

    def effective_urgency(
        self, *, now: str = "", ttl_seconds: float = _DEFAULT_TTL_SECONDS
    ) -> float:
        """Urgency after fading with age."""
        anchor = _parse_time(self.resolved_at or self.shared_at or self.created_at)
        reference = _parse_time(now) if now else datetime.now(timezone.utc)
        age = max(0.0, (reference - anchor).total_seconds())
        if ttl_seconds <= 0:
            return max(0.0, min(1.0, self.urgency))
        faded = self.urgency * (1.0 - age / ttl_seconds)
        return max(0.0, min(1.0, faded))

    def is_expired(self, *, now: str = "", ttl_seconds: float = _DEFAULT_TTL_SECONDS) -> bool:
        return self.effective_urgency(now=now, ttl_seconds=ttl_seconds) <= 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "intention_id": self.intention_id,
            "created_at": self.created_at,
            "what": self.what,
            "why": self.why,
            "resolution": self.resolution,
            "status": self.status,
            "kind": self.kind,
            "audience": self.audience,
            "audience_label": self.audience_label,
            "urgency": round(self.urgency, 4),
            "source": self.source,
            "origin_group": self.origin_group,
            "resolved_at": self.resolved_at,
            "shared_at": self.shared_at,
            "share_count": int(self.share_count),
            "attempts": int(self.attempts),
            "last_attempt_at": self.last_attempt_at,
            "refs": list(self.refs),
            "outcome": self.outcome,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Intention":
        return cls(
            intention_id=str(data.get("intention_id", "") or uuid.uuid4().hex),
            created_at=str(data.get("created_at", "") or _now_iso()),
            what=str(data.get("what", "")),
            why=str(data.get("why", "")),
            resolution=str(data.get("resolution", RESOLUTION_DO)) or RESOLUTION_DO,
            status=str(data.get("status", STATUS_NASCENT)) or STATUS_NASCENT,
            kind=str(data.get("kind", "musing")) or "musing",
            audience=str(data.get("audience", "")),
            audience_label=str(data.get("audience_label", "")),
            urgency=_clamp(data.get("urgency", _DEFAULT_URGENCY)),
            source=str(data.get("source", "")),
            origin_group=str(data.get("origin_group", "")),
            resolved_at=str(data.get("resolved_at", "")),
            shared_at=str(data.get("shared_at", "")),
            share_count=int(data.get("share_count", 0) or 0),
            attempts=int(data.get("attempts", 0) or 0),
            last_attempt_at=str(data.get("last_attempt_at", "")),
            refs=[str(item) for item in data.get("refs", []) or []],
            outcome=str(data.get("outcome", "")),
        )

    @classmethod
    def create(
        cls,
        *,
        what: str,
        why: str = "",
        resolution: str = RESOLUTION_DO,
        kind: str = "musing",
        audience: str = "",
        audience_label: str = "",
        urgency: float = _DEFAULT_URGENCY,
        source: str = "",
        origin_group: str = "",
        refs: list[str] | None = None,
    ) -> "Intention":
        return cls(
            intention_id=uuid.uuid4().hex,
            created_at=_now_iso(),
            what=str(what).strip(),
            why=str(why).strip(),
            resolution=resolution
            if resolution in {RESOLUTION_DO, RESOLUTION_TELL}
            else RESOLUTION_DO,
            kind=str(kind or "musing"),
            audience=str(audience or ""),
            audience_label=str(audience_label or ""),
            urgency=_clamp(urgency),
            source=str(source or ""),
            origin_group=str(origin_group or ""),
            refs=list(refs or []),
        )


class IntentStore:
    """Ordered, persisted collection of intentions with a small state machine."""

    def __init__(self, intentions: list[Intention] | None = None) -> None:
        self._items: list[Intention] = list(intentions or [])

    def __len__(self) -> int:
        return len(self._items)

    def all(self) -> list[Intention]:
        return list(self._items)

    def open_items(self, *, now: str = "") -> list[Intention]:
        """Open intentions, most urgent first; expired ones are dropped here."""
        alive = [item for item in self._items if item.is_open and not item.is_expired(now=now)]
        for item in self._items:
            if item.is_open and item.status == STATUS_NASCENT:
                item.status = STATUS_ACTIVE
        alive.sort(key=lambda item: item.effective_urgency(now=now), reverse=True)
        return alive

    def record_attempt(self, intention_id: str, *, now: str = "") -> Intention | None:
        """Book one model turn spent on this intention."""
        item = self.get(intention_id)
        if item is None:
            return None
        item.attempts += 1
        item.last_attempt_at = now or _now_iso()
        return item

    def resolve(self, intention_id: str, *, outcome: str = "", now: str = "") -> Intention | None:
        item = self.get(intention_id)
        if item is None:
            return None
        item.status = STATUS_RESOLVED
        item.outcome = str(outcome or "")
        item.resolved_at = now or _now_iso()
        return item

    def mark_shared(self, intention_id: str, *, now: str = "") -> Intention | None:
        """Record that she actually said it, so she does not say it again."""
        item = self.get(intention_id)
        if item is None:
            return None
        stamp = now or _now_iso()
        item.shared_at = stamp
        item.share_count += 1
        if item.resolution == RESOLUTION_TELL:
            item.status = STATUS_RESOLVED
            item.resolved_at = stamp
        return item

    def drop(self, intention_id: str) -> Intention | None:
        item = self.get(intention_id)
        if item is None:
            return None
        item.status = STATUS_DROPPED
        return item

    def attach_audience(
        self, intention_id: str, *, audience: str, label: str = ""
    ) -> Intention | None:
        """Decide who to tell, without rewriting what she wanted to say.

        This is a separate step from forming the intention: she may want to say
        something long before she knows who to say it to.  Attaching an audience
        must not create a second copy, or the same words would be delivered twice.
        """
        item = self.get(intention_id)
        if item is None or item.shared_at:
            return None
        item.audience = str(audience or "").strip()
        item.audience_label = str(label or "")
        return item

    def get(self, intention_id: str) -> Intention | None:
        for item in self._items:
            if item.intention_id == intention_id:
                return item
        return None

    def pending_shares(self, *, now: str = "") -> list[Intention]:
        """Tell-intentions that are ready to be said out loud.

        The words and the destination were both decided when the intention
        formed, so delivering one is mechanical.  An intention without an
        ``audience`` is excluded on purpose: she never picked who to tell, and
        guessing a destination on her behalf is how a private thought ends up in
        the wrong group.
        """
        ready = [
            item
            for item in self._items
            if item.is_tell
            and not item.shared_at
            and bool(item.what.strip())
            and bool(item.audience.strip())
        ]
        ready.sort(key=lambda item: item.effective_urgency(now=now), reverse=True)
        return ready

    def needs_audience(self, *, now: str = "") -> list[Intention]:
        """Tell-intentions she wants to voice but has not aimed at anyone yet."""
        return [
            item
            for item in self._items
            if item.is_tell and not item.shared_at and not item.audience.strip()
        ]

    def add(self, intention: Intention) -> Intention:
        self._items.append(intention)
        return intention

    def prune(self, *, keep: int = 60, now: str = "") -> None:
        """Keep the collection bounded, preserving open intentions."""
        open_items = [item for item in self._items if item.is_open]
        closed = [item for item in self._items if not item.is_open]
        self._items = open_items + closed[-keep:]

    def to_list(self) -> list[dict[str, Any]]:
        return [item.to_dict() for item in self._items]

    @classmethod
    def from_list(cls, data: list[Any] | None) -> "IntentStore":
        items: list[Intention] = []
        for raw in data or []:
            if isinstance(raw, dict):
                items.append(Intention.from_dict(raw))
        return cls(items)


@dataclass(slots=True)
class Audience:
    """A place she could say something, as offered to her during a turn.

    She chooses the audience herself, so candidates must be concrete and known
    to be reachable: an intention aimed at a chat that no longer exists would
    otherwise sit unresolved forever.
    """

    chat_id: str
    label: str
    kind: str = "group"
    last_active_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "chat_id": self.chat_id,
            "label": self.label,
            "kind": self.kind,
            "last_active_at": self.last_active_at,
        }


class IntentFileStore:
    """Persona-level persistence for intentions.

    Lives under ``memory/`` so edits surface in the WebUI through the same
    file-driven refresh as the rest of her memory.
    """

    def __init__(self, work_path: Any) -> None:
        from pathlib import Path

        from sirius_pulse.utils.layout import WorkspaceLayout

        self._layout = WorkspaceLayout(Path(str(work_path)))

    @property
    def path(self):
        return self._layout.memory_dir() / "intentions.json"

    def load(self) -> IntentStore:
        from sirius_pulse.utils.json_io import read_json

        payload = read_json(self.path, default={})
        raw = payload.get("intentions") if isinstance(payload, dict) else payload
        return IntentStore.from_list(raw if isinstance(raw, list) else [])

    def save(self, store: IntentStore) -> None:
        from sirius_pulse.utils.json_io import atomic_write_json

        atomic_write_json(self.path, {"intentions": store.to_list()})


def _clamp(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, number))


__all__ = [
    "Audience",
    "IntentFileStore",
    "IntentStore",
    "Intention",
    "RESOLUTION_DO",
    "RESOLUTION_TELL",
    "STATUS_ACTIVE",
    "STATUS_DROPPED",
    "STATUS_NASCENT",
    "STATUS_RESOLVED",
]
