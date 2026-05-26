"""Basic memory file store: append-only JSON Lines archival."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from sirius_pulse.memory.basic.models import BasicMemoryEntry
from sirius_pulse.utils.layout import WorkspaceLayout

logger = logging.getLogger(__name__)


class BasicMemoryFileStore:
    """Append-only archival store for basic memory entries.

    Layout:
        {work_path}/archive/{group_id}.jsonl
    """

    def __init__(self, work_path: Path | WorkspaceLayout) -> None:
        layout = work_path if isinstance(work_path, WorkspaceLayout) else WorkspaceLayout(work_path)
        self._base_dir = layout.work_path / "archive"
        self._base_dir.mkdir(parents=True, exist_ok=True)

    def append(self, entry: BasicMemoryEntry) -> None:
        """Atomically append a single entry to the group's archive file."""
        path = self._path(entry.group_id)
        line = json.dumps(entry.to_dict(), ensure_ascii=False) + "\n"
        tmp = path.with_suffix(path.suffix + ".tmp")
        # Read existing if any, append, then atomic replace
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        tmp.write_text(existing + line, encoding="utf-8")
        tmp.replace(path)

    def append_batch(self, group_id: str, entries: list[BasicMemoryEntry]) -> None:
        """Atomically append multiple entries."""
        if not entries:
            return
        path = self._path(group_id)
        lines = "\n".join(json.dumps(e.to_dict(), ensure_ascii=False) for e in entries) + "\n"
        tmp = path.with_suffix(path.suffix + ".tmp")
        existing = path.read_text(encoding="utf-8") if path.exists() else ""
        tmp.write_text(existing + lines, encoding="utf-8")
        tmp.replace(path)

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
