"""Engine state persistence for EmotionalGroupChatEngine.

Provides save/load of runtime state with group isolation:
- working_memory snapshots per group
- assistant_emotion state
- delayed_queue pending items
- group_last_message_at timestamps

Storage layout::

    {work_path}/engine_state/
        ├── assistant_emotion.json
        ├── delayed_queue.json
        ├── group_timestamps.json
        └── groups/
            └── {group_id}.json   # working memory snapshot
"""

from __future__ import annotations

import json
import logging
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Per-file locks to prevent concurrent writes on Windows (WinError 32)
_write_locks: dict[str, threading.Lock] = {}
_write_locks_lock = threading.Lock()

def _get_file_lock(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _write_locks_lock:
        if key not in _write_locks:
            _write_locks[key] = threading.Lock()
        return _write_locks[key]


class EngineStateStore:
    """Handles serialization and deserialization of engine runtime state."""

    def __init__(self, work_path: Path | str) -> None:
        self._base = Path(work_path) / "engine_state"
        self._base.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Save
    # ------------------------------------------------------------------

    def save_working_memory(
        self,
        group_id: str,
        entries: list[dict[str, Any]],
    ) -> None:
        """Save working memory snapshot for a group."""
        path = self._base / "groups" / f"{self._safe_name(group_id)}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(path, {"group_id": group_id, "entries": entries})

    def save_assistant_emotion(self, state: dict[str, Any]) -> None:
        """Save assistant emotion state."""
        path = self._base / "assistant_emotion.json"
        _atomic_write(path, state)

    def save_delayed_queue(self, items: list[dict[str, Any]]) -> None:
        """Save delayed queue pending items."""
        path = self._base / "delayed_queue.json"
        _atomic_write(path, {"items": items})

    def save_group_timestamps(self, timestamps: dict[str, str]) -> None:
        """Save last message timestamps per group."""
        path = self._base / "group_timestamps.json"
        _atomic_write(path, timestamps)

    def save_token_usage_records(self, records: list[dict[str, Any]]) -> None:
        """Save token usage records."""
        path = self._base / "token_usage_records.json"
        _atomic_write(path, {"records": records})

    def save_event_memory(self, state: dict[str, Any]) -> None:
        """Save event memory v2 state."""
        path = self._base / "event_memory.json"
        _atomic_write(path, state)

    def save_basic_memory(self, state: dict[str, Any]) -> None:
        """Save basic memory state."""
        path = self._base / "basic_memory.json"
        _atomic_write(path, state)

    def save_diary_state(self, state: dict[str, Any]) -> None:
        """Save diary manager state."""
        path = self._base / "diary_state.json"
        _atomic_write(path, state)

    def save_all(
        self,
        *,
        working_memories: dict[str, list[dict[str, Any]]],
        assistant_emotion: dict[str, Any],
        delayed_queue: list[dict[str, Any]],
        group_timestamps: dict[str, str],
        token_usage_records: list[dict[str, Any]] | None = None,
        event_memory: dict[str, Any] | None = None,
        basic_memory: dict[str, Any] | None = None,
        diary_state: dict[str, Any] | None = None,
    ) -> None:
        """Convenience: save all state in one call."""
        for group_id, entries in working_memories.items():
            self.save_working_memory(group_id, entries)
        self.save_assistant_emotion(assistant_emotion)
        self.save_delayed_queue(delayed_queue)
        self.save_group_timestamps(group_timestamps)
        if token_usage_records is not None:
            self.save_token_usage_records(token_usage_records)
        if event_memory is not None:
            self.save_event_memory(event_memory)
        if basic_memory is not None:
            self.save_basic_memory(basic_memory)
        if diary_state is not None:
            self.save_diary_state(diary_state)
        logger.info("把现在的状态记下来啦，%d 个群的上下文都好好存着呢。", len(working_memories))

    # ------------------------------------------------------------------
    # Load
    # ------------------------------------------------------------------

    def load_working_memory(self, group_id: str) -> list[dict[str, Any]]:
        """Load working memory snapshot for a group."""
        path = self._base / "groups" / f"{self._safe_name(group_id)}.json"
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return list(data.get("entries", []))
        except (OSError, json.JSONDecodeError):
            return []

    def load_assistant_emotion(self) -> dict[str, Any] | None:
        """Load assistant emotion state."""
        path = self._base / "assistant_emotion.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def load_delayed_queue(self) -> list[dict[str, Any]]:
        """Load delayed queue pending items."""
        path = self._base / "delayed_queue.json"
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return list(data.get("items", []))
        except (OSError, json.JSONDecodeError):
            return []

    def load_group_timestamps(self) -> dict[str, str]:
        """Load last message timestamps per group."""
        path = self._base / "group_timestamps.json"
        if not path.exists():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k): str(v) for k, v in data.items()}
            return {}
        except (OSError, json.JSONDecodeError):
            return {}

    def load_event_memory(self) -> dict[str, Any] | None:
        """Load event memory v2 state."""
        path = self._base / "event_memory.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def load_basic_memory(self) -> dict[str, Any] | None:
        """Load basic memory state."""
        path = self._base / "basic_memory.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def load_diary_state(self) -> dict[str, Any] | None:
        """Load diary manager state."""
        path = self._base / "diary_state.json"
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def load_all(self) -> dict[str, Any]:
        """Convenience: load all state in one call.

        Returns dict with keys:
            working_memories: dict[str, list[dict]]
            assistant_emotion: dict | None
            delayed_queue: list[dict]
            group_timestamps: dict[str, str]
            event_memory: dict | None
            basic_memory: dict | None
            diary_state: dict | None
        """
        # Discover saved groups
        groups_dir = self._base / "groups"
        group_ids: set[str] = set()
        if groups_dir.exists():
            for p in groups_dir.glob("*.json"):
                try:
                    data = json.loads(p.read_text(encoding="utf-8"))
                    gid = data.get("group_id")
                    if gid:
                        group_ids.add(str(gid))
                except (OSError, json.JSONDecodeError):
                    continue

        working_memories = {gid: self.load_working_memory(gid) for gid in group_ids}

        return {
            "working_memories": working_memories,
            "assistant_emotion": self.load_assistant_emotion(),
            "delayed_queue": self.load_delayed_queue(),
            "group_timestamps": self.load_group_timestamps(),
            "event_memory": self.load_event_memory(),
            "basic_memory": self.load_basic_memory(),
            "diary_state": self.load_diary_state(),
        }

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_name(name: str) -> str:
        import re
        base = re.sub(r"[^a-zA-Z0-9_\-\u4e00-\u9fff]+", "_", name.strip())
        base = re.sub(r"_+", "_", base).strip("_")
        return base or "default"


# ---------------------------------------------------------------------------
# Atomic write helper
# ---------------------------------------------------------------------------

def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    """Write JSON atomically using temp file + replace.

    Uses a per-file lock to prevent concurrent writes, and retries on
    Windows PermissionError (file locked by another reader/writer).
    """
    lock = _get_file_lock(path)
    with lock:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        # On Windows replace() can fail if another handle has the file open.
        for attempt in range(5):
            try:
                tmp.replace(path)
                return
            except PermissionError:
                if attempt < 4:
                    time.sleep(0.05 * (attempt + 1))
                else:
                    raise
        # Unreachable, but satisfies type checker
        return
