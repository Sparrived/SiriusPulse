"""Persistent key-value data store for tools.

Each tool gets an isolated JSON-backed store under {work_path}/tool_data/{tool_name}.json.
This allows tools to persist data across invocations.
"""

from __future__ import annotations

import json
import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


class ToolDataStore:
    """JSON-backed persistent key-value store for a single tool.

    Thread-safety: protected by an internal re-entrant lock so concurrent
    access from multiple async tasks or threads does not corrupt the file.
    """

    def __init__(self, store_path: Path) -> None:
        self._path = store_path
        self._data: dict[str, Any] = {}
        self._dirty = False
        self._lock = threading.RLock()
        self._loaded_mtime_ns: int | None = None
        self._load()

    def _stat_mtime_ns(self) -> int | None:
        try:
            return self._path.stat().st_mtime_ns
        except OSError:
            return None

    def _load(self) -> None:
        with self._lock:
            self._loaded_mtime_ns = self._stat_mtime_ns()
            if self._path.exists():
                try:
                    raw = self._path.read_text(encoding="utf-8")
                    loaded = json.loads(raw)
                    if isinstance(loaded, dict):
                        self._data = loaded
                except (json.JSONDecodeError, OSError) as exc:
                    logger.warning("TOOL数据存储加载失败 (%s): %s", self._path, exc)

    def reload(self) -> None:
        """从磁盘重新加载数据，覆盖内存中的当前数据。

        用于 WebUI 修改 tool 配置后无需重启即可被 TOOL 感知。
        """
        with self._lock:
            self._dirty = False
            self._load()

    def reload_if_changed(self) -> None:
        """文件在别处（例如 WebUI）被改过才重新加载；没改只多一次 stat。

        有未落盘修改时不覆盖，免得丢掉工具自己刚写的数据。
        """
        with self._lock:
            if self._dirty or self._stat_mtime_ns() == self._loaded_mtime_ns:
                return
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
            self._loaded_mtime_ns = self._stat_mtime_ns()

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

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Hold the store lock across a read-modify-write operation.

        Individual ``get`` and ``set`` calls are thread-safe, but a workflow
        mutation also needs its revision check and write to be atomic.
        """
        with self._lock:
            yield

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
