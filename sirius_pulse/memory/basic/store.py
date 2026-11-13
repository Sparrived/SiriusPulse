"""Basic memory file store: append-only JSON Lines archival."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path

from sirius_pulse.memory.basic.file_lock import archive_file_lock
from sirius_pulse.memory.basic.models import BasicMemoryEntry
from sirius_pulse.utils.layout import WorkspaceLayout

logger = logging.getLogger(__name__)

# Windows文件替换重试配置
_REPLACE_MAX_RETRIES = 3
_REPLACE_RETRY_DELAY = 0.1  # 100ms
#: 原地补写时在文件末尾搜索的窗口。补写的都是刚记录的消息，4 MB 足够覆盖。
_TAIL_WINDOW_BYTES = 4 * 1024 * 1024


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

    def update_entry(self, entry: BasicMemoryEntry) -> bool:
        """Rewrite one archived entry, normally touching only the file's tail.

        调用方补写的都是刚记录的消息（图片描述等），它几乎总在文件末尾。归档可达
        数 GB，逐行解析整个文件再整份重写会让每张图片都付出 O(归档大小) 的 I/O；
        这里先在末尾窗口里找，找到就只重写那一行及其后的少量行，找不到才整份重写。
        """
        if not entry.entry_id:
            return False

        path = self._path(entry.group_id)
        replacement = json.dumps(entry.to_dict(), ensure_ascii=False) + "\n"
        with self._lock_for(path), archive_file_lock(path):
            if not path.exists():
                return False
            try:
                if self._update_in_tail(path, entry.entry_id, replacement.encode("utf-8")):
                    return True
            except OSError:
                return False
            return self._rewrite_entry(path, entry.entry_id, replacement)

    @staticmethod
    def _update_in_tail(path: Path, entry_id: str, replacement: bytes) -> bool:
        """在文件末尾窗口里原地替换某条记录；窗口里没有它时返回 False。"""
        needle = entry_id.encode("utf-8")
        with path.open("r+b") as f:
            size = f.seek(0, os.SEEK_END)
            start = max(0, size - _TAIL_WINDOW_BYTES)
            f.seek(start)
            data = f.read()
            pos = 0
            if start > 0:
                # 窗口从行中间开始：跳到下一行行首。
                first_newline = data.find(b"\n")
                if first_newline < 0:
                    return False
                pos = first_newline + 1
            target: tuple[int, int] | None = None
            while pos < len(data):
                newline = data.find(b"\n", pos)
                line_end = len(data) if newline < 0 else newline + 1
                if needle in data[pos:line_end]:
                    try:
                        parsed = json.loads(data[pos:line_end])
                    except ValueError:
                        parsed = None
                    if isinstance(parsed, dict) and parsed.get("entry_id") == entry_id:
                        target = (pos, line_end)
                pos = line_end
            if target is None:
                return False
            line_start, line_end = target
            f.seek(start + line_start)
            f.write(replacement + data[line_end:])
            f.truncate()
            f.flush()
            os.fsync(f.fileno())
        return True

    def _rewrite_entry(self, path: Path, entry_id: str, replacement: str) -> bool:
        """整份重写归档以替换一条记录（目标不在末尾窗口时的兜底）。调用方持锁。"""
        updated = False
        tmp = self._tmp_path(path)
        try:
            with path.open("r", encoding="utf-8") as src, tmp.open("w", encoding="utf-8") as dst:
                for raw_line in src:
                    if entry_id not in raw_line:
                        dst.write(raw_line)
                        continue
                    try:
                        data = json.loads(raw_line)
                    except json.JSONDecodeError:
                        dst.write(raw_line)
                        continue
                    if isinstance(data, dict) and data.get("entry_id") == entry_id:
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

    @staticmethod
    def _safe_name(name: str) -> str:
        import re

        base = re.sub(r"[^a-zA-Z0-9_\-\u4e00-\u9fff]+", "_", name.strip())
        base = re.sub(r"_+", "_", base).strip("_")
        return base or "default"
