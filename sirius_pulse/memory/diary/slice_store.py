"""日记切片持久化存储。

Layout:
    {work_path}/diary/slices/{group_id}.json
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from sirius_pulse.memory.diary.slice_models import DiarySlice
from sirius_pulse.utils.json_io import atomic_write_json

logger = logging.getLogger(__name__)

__all__ = ["DiarySliceStore"]


class DiarySliceStore:
    """日记切片文件存储。"""

    def __init__(self, work_path: Path) -> None:
        self._base_dir = work_path / "diary" / "slices"
        self._base_dir.mkdir(parents=True, exist_ok=True)

    def save(self, group_id: str, slices: list[DiarySlice]) -> None:
        """保存某群组的所有切片。"""
        path = self._path(group_id)
        data = {
            "group_id": group_id,
            "slices": [s.to_dict() for s in slices],
        }
        atomic_write_json(path, data)

    def append(self, group_id: str, slices: list[DiarySlice]) -> None:
        """追加切片到已有数据。"""
        existing = self.load(group_id)
        existing.extend(slices)
        self.save(group_id, existing)

    def load(self, group_id: str) -> list[DiarySlice]:
        """加载某群组的所有切片。"""
        path = self._path(group_id)
        if not path.exists():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return [
                DiarySlice.from_dict(item)
                for item in data.get("slices", [])
                if isinstance(item, dict)
            ]
        except (OSError, json.JSONDecodeError):
            return []

    def load_all(self) -> list[DiarySlice]:
        """加载所有群组的切片。"""
        all_slices: list[DiarySlice] = []
        for path in self._base_dir.glob("*.json"):
            group_id = path.stem
            all_slices.extend(self.load(group_id))
        return all_slices

    def delete(self, group_id: str) -> None:
        """删除某群组的所有切片。"""
        path = self._path(group_id)
        if path.exists():
            path.unlink()

    def count(self, group_id: str) -> int:
        """统计某群组的切片数量。"""
        return len(self.load(group_id))

    def _path(self, group_id: str) -> Path:
        safe = self._safe_name(group_id)
        return self._base_dir / f"{safe}.json"

    @staticmethod
    def _safe_name(name: str) -> str:
        import re
        base = re.sub(r"[^a-zA-Z0-9_\-\u4e00-\u9fff]+", "_", name.strip())
        return base[:100] or "default"
