"""Basic memory file store: append-only JSON Lines archival."""

from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from sirius_pulse.memory.basic.models import BasicMemoryEntry
from sirius_pulse.utils.layout import WorkspaceLayout

logger = logging.getLogger(__name__)

# Windows文件替换重试配置
_REPLACE_MAX_RETRIES = 3
_REPLACE_RETRY_DELAY = 0.1  # 100ms


class BasicMemoryFileStore:
    """Append-only archival store for basic memory entries.

    Layout:
        {work_path}/archive/{group_id}.jsonl

    支持可选的 remote_bridge：助手模式下，新消息同时推送到管家端。
    """

    def __init__(
        self,
        work_path: Path | WorkspaceLayout,
        remote_bridge: Any = None,
    ) -> None:
        layout = (
            work_path
            if isinstance(work_path, WorkspaceLayout)
            else WorkspaceLayout(work_path)
        )
        self._base_dir = layout.work_path / "archive"
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._remote_bridge = remote_bridge
        self._locks_guard = threading.Lock()
        self._locks: dict[Path, threading.RLock] = {}

    def _lock_for(self, path: Path) -> threading.RLock:
        key = path.resolve()
        with self._locks_guard:
            lock = self._locks.get(key)
            if lock is None:
                lock = threading.RLock()
                self._locks[key] = lock
            return lock

    @staticmethod
    def _tmp_path(target: Path) -> Path:
        return target.with_name(f"{target.name}.{uuid.uuid4().hex}.tmp")

    def _atomic_replace(self, tmp: Path, target: Path) -> None:
        """原子替换文件，Windows下添加重试机制。"""
        for attempt in range(_REPLACE_MAX_RETRIES):
            try:
                tmp.replace(target)
                return
            except PermissionError:
                if attempt < _REPLACE_MAX_RETRIES - 1:
                    logger.warning(
                        "文件替换失败，重试中 (%d/%d): %s",
                        attempt + 1,
                        _REPLACE_MAX_RETRIES,
                        target,
                    )
                    time.sleep(_REPLACE_RETRY_DELAY)
                else:
                    raise

    def append(self, entry: BasicMemoryEntry) -> None:
        """Atomically append a single entry to the group's archive file."""
        path = self._path(entry.group_id)
        line = json.dumps(entry.to_dict(), ensure_ascii=False) + "\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock_for(path):
            with path.open("a", encoding="utf-8") as f:
                f.write(line)
        # 助手模式：实时推送到管家端
        if self._remote_bridge is not None:
            self._remote_bridge.push_message(entry.group_id, entry.to_dict())

    def append_batch(self, group_id: str, entries: list[BasicMemoryEntry]) -> None:
        """Atomically append multiple entries."""
        if not entries:
            return
        path = self._path(group_id)
        lines = (
            "\n".join(json.dumps(e.to_dict(), ensure_ascii=False) for e in entries)
            + "\n"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock_for(path):
            with path.open("a", encoding="utf-8") as f:
                f.write(lines)

    def update_entry(self, entry: BasicMemoryEntry) -> bool:
        """Rewrite an archived entry in place by entry_id."""
        if not entry.entry_id:
            return False

        path = self._path(entry.group_id)
        with self._lock_for(path):
            if not path.exists():
                return False

            replacement = json.dumps(entry.to_dict(), ensure_ascii=False) + "\n"
            updated = False
            lines: list[str] = []

            try:
                with path.open("r", encoding="utf-8") as f:
                    for raw_line in f:
                        stripped = raw_line.strip()
                        if not stripped:
                            lines.append(raw_line)
                            continue
                        try:
                            data = json.loads(stripped)
                        except json.JSONDecodeError:
                            lines.append(raw_line)
                            continue

                        if data.get("entry_id") == entry.entry_id:
                            lines.append(replacement)
                            updated = True
                        else:
                            lines.append(raw_line)
            except OSError:
                return False

            if not updated:
                return False

            tmp = self._tmp_path(path)
            try:
                tmp.write_text("".join(lines), encoding="utf-8")
                self._atomic_replace(tmp, path)
                return True
            finally:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass

    def read_all(self, group_id: str) -> list[BasicMemoryEntry]:
        """Read all archived entries for a group."""
        path = self._path(group_id)
        if not path.exists():
            return []
        entries: list[BasicMemoryEntry] = []
        try:
            with path.open("r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        entries.append(BasicMemoryEntry.from_dict(data))
                    except (json.JSONDecodeError, TypeError):
                        continue
        except OSError:
            return []
        return entries

    def _path(self, group_id: str) -> Path:
        safe = self._safe_name(group_id)
        return self._base_dir / f"{safe}.jsonl"

    def restore_archive(self, group_id: str, entries: list[dict[str, Any]]) -> None:
        """从远程快照恢复归档消息（覆盖写入）。"""
        if not entries:
            return
        path = self._path(group_id)
        lines = "\n".join(json.dumps(e, ensure_ascii=False) for e in entries) + "\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock_for(path):
            tmp = self._tmp_path(path)
            try:
                tmp.write_text(lines, encoding="utf-8")
                self._atomic_replace(tmp, path)
            finally:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass

    @staticmethod
    def _safe_name(name: str) -> str:
        import re

        base = re.sub(r"[^a-zA-Z0-9_\-\u4e00-\u9fff]+", "_", name.strip())
        base = re.sub(r"_+", "_", base).strip("_")
        return base or "default"
