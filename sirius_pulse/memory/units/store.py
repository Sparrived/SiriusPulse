"""File storage for checkpoint memory units."""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path

from sirius_pulse.memory.units.models import MemoryUnit
from sirius_pulse.utils.json_io import atomic_write_json
from sirius_pulse.utils.layout import WorkspaceLayout

logger = logging.getLogger(__name__)


class MemoryUnitFileStore:
    """File-based storage for memory units.

    Layout:
        {work_path}/memory_units/{group_id}.json
    """

    def __init__(self, work_path: Path | WorkspaceLayout) -> None:
        layout = work_path if isinstance(work_path, WorkspaceLayout) else WorkspaceLayout(work_path)
        self._base_dir = layout.work_path / "memory_units"
        self._base_dir.mkdir(parents=True, exist_ok=True)

    def save(self, group_id: str, units: list[MemoryUnit]) -> None:
        path = self._path(group_id)
        data = {"group_id": group_id, "units": [u.to_dict() for u in units]}
        atomic_write_json(path, data)

    def load(self, group_id: str) -> list[MemoryUnit]:
        path = self._path(group_id)
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return [
                MemoryUnit.from_dict(item)
                for item in data.get("units", [])
                if isinstance(item, dict)
            ]
        except (OSError, json.JSONDecodeError, TypeError) as exc:
            logger.warning("Failed to load memory units for group %s: %s", group_id, exc)
            return []

    def _path(self, group_id: str) -> Path:
        return self._base_dir / f"{self._safe_name(group_id)}.json"

    @staticmethod
    def _safe_name(name: str) -> str:
        base = re.sub(r"[^a-zA-Z0-9_\-\u4e00-\u9fff]+", "_", name.strip())
        base = re.sub(r"_+", "_", base).strip("_")
        return base or "default"
