"""File storage for checkpoint memory units."""

from __future__ import annotations

import json
import logging
import re
import shutil
import uuid
from pathlib import Path
from typing import Any

from sirius_pulse.memory.units.models import MemoryUnit
from sirius_pulse.utils.json_io import atomic_write_json, replace_with_retry
from sirius_pulse.utils.layout import WorkspaceLayout

logger = logging.getLogger(__name__)

#: 取 group_id 时只读文件头这么多字节。``atomic_write_json`` 的 payload 固定以
#: ``{"group_id": ...}`` 开头，ID 必然落在开头几行内，因此 4 KiB 足够。
_GROUP_ID_PROBE_BYTES = 4096
_GROUP_ID_RE = re.compile(r'"group_id"\s*:\s*"((?:[^"\\]|\\.)*)"')
_EMPTY_UNITS_RE = re.compile(r'"units"\s*:\s*\[\s*\]')


class MemoryUnitFileStore:
    """File-based storage for memory units.

    Layout:
        {work_path}/memory_units/{group_id}.json
    """

    def __init__(self, work_path: Path | WorkspaceLayout) -> None:
        layout = work_path if isinstance(work_path, WorkspaceLayout) else WorkspaceLayout(work_path)
        self._base_dir = layout.work_path / "memory_units"
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._group_ids: set[str] | None = None

    def save(self, group_id: str, units: list[MemoryUnit]) -> None:
        path = self._path(group_id)
        data = {"group_id": group_id, "units": [u.to_dict() for u in units]}
        atomic_write_json(path, data)
        self._note_group(group_id, bool(units))

    def _note_group(self, group_id: str, has_units: bool) -> None:
        """就地更新 group_id 缓存，避免每次写盘后都重扫整个目录。

        ``list_group_ids()`` 的调用方是检索路径（cross-group 开启时每条消息都会
        走到），而每次 checkpoint 落盘都会写 ``memory_units/*.json``。若写盘一律
        作废缓存，紧接着的那次检索就要重扫一遍全部文件——包括 258 MB 的那个。
        写盘方自己知道改了哪个分组，直接增量维护即可。
        """
        if self._group_ids is None:
            return
        if has_units:
            self._group_ids.add(group_id)
        else:
            self._group_ids.discard(group_id)

    @property
    def base_dir(self) -> Path:
        return self._base_dir

    def list_group_ids(self) -> list[str]:
        """Return every group that has units, reading each file at most once.

        调用方是检索路径（cross-group 记忆默认开启），每条消息都会走到这里。这里
        **只读每个文件的开头**而不是全量解析：``atomic_write_json`` 的 payload
        固定以 ``{"group_id": …}`` 开头，而单个文件可达数百 MB（96.9% 是向量），
        为一个 group_id 去解析 241 MB 会在每条消息上付出秒级开销。结果做进程内
        缓存，并由写盘方增量维护（见 ``_note_group``）。
        """
        if self._group_ids is None:
            group_ids: set[str] = set()
            for path in self._base_dir.glob("*.json"):
                group_id = self._probe_group_id(path)
                if group_id is not None:
                    group_ids.add(group_id)
            self._group_ids = group_ids
        return sorted(self._group_ids)

    def _probe_group_id(self, path: Path) -> str | None:
        """从文件开头提取 group_id；空单元的分组返回 None。

        只认开头 ``_GROUP_ID_PROBE_BYTES`` 字节内的 ``group_id``。若该窗口内看不到
        （理论上不会发生，因为它是 JSON 的第一个键），退回到全量解析以保证不漏分组。
        """
        try:
            with path.open("r", encoding="utf-8") as handle:
                head = handle.read(_GROUP_ID_PROBE_BYTES)
        except (OSError, UnicodeDecodeError):
            return None
        match = _GROUP_ID_RE.search(head)
        if match is None:
            data = self._read_payload(path)
            if data is None or not data.get("units"):
                return None
            return str(data.get("group_id") or path.stem)
        # 头部窗口内若能确认 units 为空数组，说明该分组没有单元，跳过。
        if _EMPTY_UNITS_RE.search(head):
            return None
        try:
            return json.loads(f'"{match.group(1)}"')
        except json.JSONDecodeError:  # pragma: no cover - 正则已保证是合法字符串体
            return match.group(1)

    @staticmethod
    def _read_payload(path: Path) -> dict[str, Any] | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            return None
        return data if isinstance(data, dict) else None

    def save_many_atomically(self, groups: dict[str, list[MemoryUnit]]) -> None:
        stage_dir = self._base_dir.parent / f".memory_units_stage_{uuid.uuid4().hex}"
        stage_dir.mkdir(parents=True)
        try:
            staged: dict[str, Path] = {}
            for group_id, units in groups.items():
                path = stage_dir / f"{self._safe_name(group_id)}.json"
                atomic_write_json(
                    path, {"group_id": group_id, "units": [u.to_dict() for u in units]}
                )
                staged[group_id] = path
            self._base_dir.mkdir(parents=True, exist_ok=True)
            for group_id, path in staged.items():
                self._replace_staged(path, self._path(group_id))
        finally:
            shutil.rmtree(stage_dir, ignore_errors=True)
        for group_id, units in groups.items():
            self._note_group(group_id, bool(units))

    @staticmethod
    def _replace_staged(staged: Path, destination: Path) -> None:
        replace_with_retry(staged, destination)

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
