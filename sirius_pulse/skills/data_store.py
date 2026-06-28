"""Persistent key-value data store for skills.

Each skill gets an isolated JSON-backed store under {work_path}/skill_data/{skill_name}.json.
This allows skills to persist data across invocations.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class SkillDataStore:
    """JSON-backed persistent key-value store for a single skill.

    Thread-safety: protected by an internal re-entrant lock so concurrent
    access from multiple async tasks or threads does not corrupt the file.
    """

    def __init__(self, store_path: Path) -> None:
        self._path = store_path
        self._data: dict[str, Any] = {}
        self._dirty = False
        self._lock = threading.RLock()
        self._load()

    def _load(self) -> None:
        with self._lock:
            if self._path.exists():
                try:
                    raw = self._path.read_text(encoding="utf-8")
                    loaded = json.loads(raw)
                    if isinstance(loaded, dict):
                        self._data = loaded
                except (json.JSONDecodeError, OSError) as exc:
                    logger.warning("SKILL数据存储加载失败 (%s): %s", self._path, exc)

    def reload(self) -> None:
        """从磁盘重新加载数据，覆盖内存中的当前数据。

        用于 WebUI 修改 skill 配置后无需重启即可被 SKILL 感知。
        """
        with self._lock:
            self._dirty = False
            self._load()

    def save(self) -> None:
        """持久化当前数据到磁盘（仅在修改后写入）。

        使用原子 JSON 保存，避免多个线程并发写入时损坏数据。
        """
        with self._lock:
            if not self._dirty:
                return
            from sirius_pulse.config.file_io import atomic_json_save

            atomic_json_save(self._path, self._data)
            self._dirty = False

    def get(self, key: str, default: Any = None) -> Any:
        """Get a value by key."""
        with self._lock:
            return self._data.get(key, default)

    def set(self, key: str, value: Any) -> None:
        """Set a value by key. Call save() to persist."""
        with self._lock:
            self._data[key] = value
            self._dirty = True

    def delete(self, key: str) -> bool:
        """Delete a key. Returns True if key existed."""
        with self._lock:
            if key in self._data:
                del self._data[key]
                self._dirty = True
                return True
            return False

    def keys(self) -> list[str]:
        """Return all stored keys."""
        with self._lock:
            return list(self._data.keys())

    def all(self) -> dict[str, Any]:
        """Return a shallow copy of all stored data."""
        with self._lock:
            return dict(self._data)

    @property
    def is_dirty(self) -> bool:
        with self._lock:
            return self._dirty

    @property
    def store_path(self) -> Path:
        return self._path

    @property
    def artifact_dir(self) -> Path:
        return self._path.parent / "artifacts" / self._path.stem
