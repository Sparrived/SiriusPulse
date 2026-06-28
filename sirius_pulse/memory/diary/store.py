"""Diary memory file store."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from sirius_pulse.memory.diary.models import DiaryEntry
from sirius_pulse.utils.json_io import atomic_write_json
from sirius_pulse.utils.layout import WorkspaceLayout

logger = logging.getLogger(__name__)


class DiaryFileStore:
    """File-based storage for diary entries.

    Layout:
        {work_path}/diary/{group_id}.json
    """

    def __init__(self, work_path: Path | WorkspaceLayout) -> None:
        layout = work_path if isinstance(work_path, WorkspaceLayout) else WorkspaceLayout(work_path)
        self._base_dir = layout.work_path / "diary"
        self._base_dir.mkdir(parents=True, exist_ok=True)

    def save(self, group_id: str, entries: list[DiaryEntry]) -> None:
        path = self._path(group_id)
        data = {"group_id": group_id, "entries": [e.to_dict() for e in entries]}
        atomic_write_json(path, data)

    def load(self, group_id: str) -> list[DiaryEntry]:
        path = self._path(group_id)
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return [
                DiaryEntry.from_dict(item)
                for item in data.get("entries", [])
                if isinstance(item, dict)
            ]
        except (OSError, json.JSONDecodeError):
            return []

    def _path(self, group_id: str) -> Path:
        safe = self._safe_name(group_id)
        return self._base_dir / f"{safe}.json"

    @staticmethod
    def _safe_name(name: str) -> str:
        import re

        base = re.sub(r"[^a-zA-Z0-9_\-\u4e00-\u9fff]+", "_", name.strip())
        base = re.sub(r"_+", "_", base).strip("_")
        return base or "default"
