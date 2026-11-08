"""她心里对一个人的印象。

不是档案，也不是事实库：事实仍在记忆单元里，靠检索按需取用。这里是记忆单元
「合起来」之后她对这个人的整体感觉——对方是什么样的人、她们一起经历过什么、
还有哪些事她惦记着想问问。它按人保存、跨群共用，由后台定期根据新记忆更新。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

#: 有日期的惦记过了期限这么久仍没结果，就不再提；没有日期的惦记放这么久也放下。
_THREAD_GRACE_DAYS = 7
_UNDATED_THREAD_DAYS = 21


def _parse(value: str) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


@dataclass(slots=True)
class OpenThread:
    """对方提过、之后值得关心或追问的一件事。"""

    text: str
    due: str = ""
    """大概的日期（YYYY-MM-DD），不知道时为空。"""
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "due": self.due, "created_at": self.created_at}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "OpenThread":
        return cls(
            text=str(data.get("text", "") or "").strip(),
            due=str(data.get("due", "") or "").strip()[:10],
            created_at=str(data.get("created_at", "") or ""),
        )

    def is_live(self, now: datetime) -> bool:
        due = _parse(self.due)
        if due is not None:
            return now <= due + timedelta(days=_THREAD_GRACE_DAYS)
        created = _parse(self.created_at)
        return created is None or now <= created + timedelta(days=_UNDATED_THREAD_DAYS)

    def is_due_soon(self, now: datetime) -> bool:
        """日期就在这几天：到了可以自然问问的时候。"""
        due = _parse(self.due)
        return due is not None and due - timedelta(days=1) <= now <= due + timedelta(days=3)


@dataclass(slots=True)
class PersonImpression:
    """她对一个人的整体印象与关系温度。"""

    user_id: str
    name: str = ""
    impression: str = ""
    shared: list[str] = field(default_factory=list)
    """一起经历过、值得记住的具体事，新的在前。"""
    threads: list[OpenThread] = field(default_factory=list)
    warmth: float = 0.0
    """她对这个人的亲近程度，-1..1。会写进 ``user_bias``，让她面对不同的人心情不同。"""
    updated_at: str = ""
    watermark: str = ""
    """已经并进印象的最新记忆单元的 created_at；之后的单元才算「新的」。"""
    update_count: int = 0

    def live_threads(self, now: datetime | None = None) -> list[OpenThread]:
        now = now or datetime.now(timezone.utc)
        return [thread for thread in self.threads if thread.text and thread.is_live(now)]

    def to_dict(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "name": self.name,
            "impression": self.impression,
            "shared": list(self.shared),
            "threads": [thread.to_dict() for thread in self.threads],
            "warmth": self.warmth,
            "updated_at": self.updated_at,
            "watermark": self.watermark,
            "update_count": self.update_count,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PersonImpression":
        threads = [
            OpenThread.from_dict(item) for item in data.get("threads", []) if isinstance(item, dict)
        ]
        try:
            warmth = max(-1.0, min(1.0, float(data.get("warmth", 0.0) or 0.0)))
        except (TypeError, ValueError):
            warmth = 0.0
        return cls(
            user_id=str(data.get("user_id", "") or ""),
            name=str(data.get("name", "") or ""),
            impression=str(data.get("impression", "") or ""),
            shared=[str(item) for item in data.get("shared", []) if str(item).strip()],
            threads=[thread for thread in threads if thread.text],
            warmth=warmth,
            updated_at=str(data.get("updated_at", "") or ""),
            watermark=str(data.get("watermark", "") or ""),
            update_count=int(data.get("update_count", 0) or 0),
        )
