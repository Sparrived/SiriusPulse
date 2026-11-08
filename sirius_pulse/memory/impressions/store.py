"""按人保存印象：``{work_path}/memory/impressions/{user_id}.json``。"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from pathlib import Path

from sirius_pulse.memory.impressions.models import PersonImpression
from sirius_pulse.utils.json_io import atomic_write_json

logger = logging.getLogger(__name__)

#: 聊天每轮都要读印象；目录里几十个小文件，缓存几十秒足够，写入时就地更新缓存。
_CACHE_SECONDS = 60.0


class ImpressionStore:
    """一个人格的全部人物印象。读多写少：聊天每轮读，后台偶尔写。"""

    def __init__(self, work_path: Path | str) -> None:
        self._dir = Path(work_path) / "memory" / "impressions"
        self._lock = threading.Lock()
        self._cache: dict[str, PersonImpression] | None = None
        self._loaded_at = 0.0

    @property
    def directory(self) -> Path:
        return self._dir

    def _path(self, user_id: str) -> Path:
        safe = re.sub(r"[^0-9A-Za-z_.-]+", "_", user_id.strip()) or "unknown"
        return self._dir / f"{safe}.json"

    def all(self) -> dict[str, PersonImpression]:
        """全部印象，按 user_id 索引（返回的是缓存的副本字典，对象本身别改）。"""
        with self._lock:
            if self._cache is None or time.monotonic() - self._loaded_at > _CACHE_SECONDS:
                self._cache = self._read_all()
                self._loaded_at = time.monotonic()
            return dict(self._cache)

    def get(self, user_id: str) -> PersonImpression | None:
        return self.all().get(user_id)

    def save(self, impression: PersonImpression) -> None:
        if not impression.user_id:
            raise ValueError("impression needs a user_id")
        self._dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self._path(impression.user_id), impression.to_dict())
        with self._lock:
            if self._cache is not None:
                self._cache[impression.user_id] = impression

    def _read_all(self) -> dict[str, PersonImpression]:
        result: dict[str, PersonImpression] = {}
        if not self._dir.exists():
            return result
        for path in self._dir.glob("*.json"):
            try:
                impression = PersonImpression.from_dict(json.loads(path.read_text("utf-8")))
            except (OSError, ValueError, TypeError) as exc:
                logger.warning("跳过读不了的人物印象 %s: %s", path.name, exc)
                continue
            if impression.user_id:
                result[impression.user_id] = impression
        return result
