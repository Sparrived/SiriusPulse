"""Basic memory file store: append-only JSON Lines archival."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

from sirius_pulse.memory.basic.file_lock import archive_file_lock
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

    """

    def __init__(
        self,
        work_path: Path | WorkspaceLayout,
    ) -> None:
        layout = work_path if isinstance(work_path, WorkspaceLayout) else WorkspaceLayout(work_path)
        self._base_dir = layout.work_path / "archive"
        self._base_dir.mkdir(parents=True, exist_ok=True)
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
        """Synchronize a completed temporary archive then replace the target."""
        with tmp.open("a", encoding="utf-8") as f:
            f.flush()
            os.fsync(f.fileno())
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
        with self._lock_for(path), archive_file_lock(path):
            with path.open("a", encoding="utf-8") as f:
                f.write(line)

    def append_batch(self, group_id: str, entries: list[BasicMemoryEntry]) -> None:
        """Atomically append multiple entries."""
        if not entries:
            return
        path = self._path(group_id)
        lines = "\n".join(json.dumps(e.to_dict(), ensure_ascii=False) for e in entries) + "\n"
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock_for(path), archive_file_lock(path):
            with path.open("a", encoding="utf-8") as f:
                f.write(lines)

    def update_entry(self, entry: BasicMemoryEntry) -> bool:
        """Rewrite one archived entry without loading the whole JSONL file."""
        if not entry.entry_id:
            return False

        path = self._path(entry.group_id)
        replacement = json.dumps(entry.to_dict(), ensure_ascii=False) + "\n"
        with self._lock_for(path), archive_file_lock(path):
            if not path.exists():
                return False

            updated = False
            tmp = self._tmp_path(path)
            try:
                with path.open("r", encoding="utf-8") as src, tmp.open(
                    "w", encoding="utf-8"
                ) as dst:
                    for raw_line in src:
                        try:
                            data = json.loads(raw_line)
                        except json.JSONDecodeError:
                            dst.write(raw_line)
                            continue
                        if isinstance(data, dict) and data.get("entry_id") == entry.entry_id:
                            dst.write(replacement)
                            updated = True
                        else:
                            dst.write(raw_line)
                if updated:
                    self._atomic_replace(tmp, path)
                return updated
            except OSError:
                return False
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
        with self._lock_for(path), archive_file_lock(path):
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
