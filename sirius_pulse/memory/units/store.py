"""File storage for checkpoint memory units."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import uuid
from array import array
from pathlib import Path
from typing import Any

from sirius_pulse.memory.units.models import MemoryUnit, embedding_text
from sirius_pulse.utils.json_io import atomic_write_json, replace_with_retry
from sirius_pulse.utils.layout import WorkspaceLayout

logger = logging.getLogger(__name__)

#: 取 group_id 时只读文件头这么多字节。``atomic_write_json`` 的 payload 固定以
#: ``{"group_id": ...}`` 开头，ID 必然落在开头几行内，因此 4 KiB 足够。
_GROUP_ID_PROBE_BYTES = 4096
_GROUP_ID_RE = re.compile(r'"group_id"\s*:\s*"((?:[^"\\]|\\.)*)"')
_EMPTY_UNITS_RE = re.compile(r'"units"\s*:\s*\[\s*\]')
#: 写盘格式版本。2 = 向量外置到 sidecar；缺失该键的文件即旧的内联格式。
#: 它紧跟 group_id 落盘，所以读文件头就能判出来，不必解析几百 MB 的向量。
#: 公开是因为 WebUI 会直接改写单元文件，必须由它写回同一个标记与 ``vector_file``。
UNITS_FORMAT_KEY = "units_format"
UNITS_FORMAT_VERSION = 2
VECTOR_FILE_KEY = "vector_file"

#: 向量 sidecar 的目录名（相对 ``memory_units/``）。
VECTORS_DIR_NAME = "vectors"
#: 向量按 float32 打包存放：1024 维一条 = 4 KiB，内联成 JSON 文本要 30 KiB 以上。
_VECTOR_TYPECODE = "f"
_VECTOR_ITEM_BYTES = array(_VECTOR_TYPECODE).itemsize


class MemoryUnitFileStore:
    """File-based storage for memory units.

    Layout:
        {work_path}/memory_units/{group_id}.json          — 单元元数据（无向量）
        {work_path}/memory_units/vectors/{group_id}.bin   — 打包的 float32 向量

    向量与元数据分开存放，原因见 ``load`` / ``hydrate_embeddings`` 的文档：向量占
    单条单元的 99% 体积，而其中绝大多数属于已退休、永远不会被注入的单元。放在同一
    个 JSON 里意味着「取一条摘要」必须解析几亿字符的向量文本。
    """

    def __init__(self, work_path: Path | WorkspaceLayout) -> None:
        layout = work_path if isinstance(work_path, WorkspaceLayout) else WorkspaceLayout(work_path)
        self._base_dir = layout.work_path / "memory_units"
        self._base_dir.mkdir(parents=True, exist_ok=True)
        self._vectors_dir = self._base_dir / VECTORS_DIR_NAME
        self._group_ids: set[str] | None = None

    def save(self, group_id: str, units: list[MemoryUnit]) -> None:
        self._write_payload(group_id, units)
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

    @property
    def vectors_dir(self) -> Path:
        return self._vectors_dir

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

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------

    def load(self, group_id: str, *, with_embeddings: bool = True) -> list[MemoryUnit]:
        """读取某分组的单元；``with_embeddings=False`` 时完全不碰向量文件。

        默认带上向量，是为了让「需要向量」的调用方（离线重建、去重扫描、WebUI
        导出）行为与改动前一致。常驻检索路径应当传 ``False``，再只给真正参与注入
        的单元调 ``hydrate_embeddings``——退休单元的向量没有任何检索价值，让它们
        常驻内存正是内存占用失控的主因。
        """
        path = self._path(group_id)
        if not path.exists():
            return []
        data = self._read_payload(path)
        if data is None:
            logger.warning("Failed to load memory units for group %s: unreadable payload", group_id)
            return []
        items = [item for item in data.get("units", []) if isinstance(item, dict)]
        units = [MemoryUnit.from_dict(item) for item in items]
        if not with_embeddings:
            for unit in units:
                unit.embedding = None
            return units
        self._attach_embeddings(data, items, units)
        return units

    def hydrate_embeddings(
        self,
        group_id: str,
        units: list[MemoryUnit],
        *,
        unit_ids: set[str] | None = None,
    ) -> int:
        """从向量文件给 ``units`` 补上 ``embedding``，返回补上的条数。

        只读 ``unit_ids``（缺省为全部尚未带向量的）对应的一段，不解析整个分组。
        """
        pending = [
            unit
            for unit in units
            if not unit.embedding and (unit_ids is None or unit.unit_id in unit_ids)
        ]
        if not pending:
            return 0
        _name, bin_path, index, inline = self._read_vector_index(group_id)
        if bin_path is None:
            # 尚未迁移的旧文件：向量还内联在 JSON 里。
            restored = 0
            for unit in pending:
                legacy = inline.get(unit.unit_id)
                if legacy:
                    unit.embedding = legacy
                    restored += 1
            return restored
        try:
            with bin_path.open("rb") as handle:
                return sum(
                    1 for unit in pending if _read_vector(handle, index.get(unit.unit_id), unit)
                )
        except OSError as exc:
            logger.warning("Failed to hydrate memory unit vectors for group %s: %s", group_id, exc)
            return 0

    def _attach_embeddings(
        self,
        data: dict[str, Any],
        items: list[dict[str, Any]],
        units: list[MemoryUnit],
    ) -> None:
        """补齐 ``units`` 的向量：老格式的内联向量优先，其余从 sidecar 取。"""
        vector_name = data.get(VECTOR_FILE_KEY)
        if not isinstance(vector_name, str) or not vector_name:
            return
        index = _vector_index(items)
        bin_path = self._vectors_dir / vector_name
        try:
            with bin_path.open("rb") as handle:
                for unit in units:
                    if unit.embedding:
                        continue
                    _read_vector(handle, index.get(unit.unit_id), unit)
        except OSError as exc:
            logger.warning("Failed to read memory unit vectors %s: %s", bin_path, exc)

    # ------------------------------------------------------------------
    # 写
    # ------------------------------------------------------------------

    def _write_payload(self, group_id: str, units: list[MemoryUnit]) -> None:
        """写元数据与向量。JSON 是唯一提交点，向量文件是它的从属文件。

        顺序是先写新向量文件、再原子替换 JSON、最后删掉旧向量文件：任何一步之间
        中断，磁盘上留下的都是「旧 JSON + 旧向量」或「新 JSON + 新向量」这样自洽的
        组合，不会出现摘要与向量错配。
        """
        previous_name, previous_bin, previous_index, inline = self._read_vector_index(group_id)
        items, blob = self._serialize(units, previous_bin, previous_index, inline)
        new_name = self._write_vector_blob(group_id, blob)
        payload: dict[str, Any] = {
            "group_id": group_id,
            UNITS_FORMAT_KEY: UNITS_FORMAT_VERSION,
            "units": items,
        }
        if new_name is not None:
            payload[VECTOR_FILE_KEY] = new_name
        atomic_write_json(self._path(group_id), payload)
        self._discard_vector_file(previous_name, keep=new_name)

    def is_legacy_layout(self, group_id: str) -> bool:
        """判断某分组的文件是否还是「向量内联」的旧格式。

        只读文件头：格式版本号紧跟在 group_id 之后，所以不需要为一次判断解析
        几百 MB 的向量。用途见 ``MemoryUnitManager.ensure_group_loaded``——旧格式
        的向量留在 JSON 里，只有真的落一次盘才会搬进 sidecar。
        """
        path = self._path(group_id)
        if not path.exists():
            return False
        try:
            with path.open("r", encoding="utf-8") as handle:
                head = handle.read(_GROUP_ID_PROBE_BYTES)
        except (OSError, UnicodeDecodeError):
            return False
        return UNITS_FORMAT_KEY not in head

    def _serialize(
        self,
        units: list[MemoryUnit],
        previous_bin: Path | None,
        previous_index: dict[str, tuple[int, int]],
        inline: dict[str, list[float]] | None = None,
    ) -> tuple[list[dict[str, Any]], bytes]:
        """拆出元数据与向量。内存里没有向量的单元沿用磁盘上的那一份。

        沿用是关键：常驻路径为了省内存是「不带向量读入」的，若写盘时把 ``None``
        当成「没有向量」，一次 checkpoint 就会把整组向量清空。WebUI 的增删改同理
        ——它只改元数据，不该动向量。``inline`` 是旧格式（向量内联）的沿用来源。
        """
        items: list[dict[str, Any]] = []
        blob = bytearray()
        handle = previous_bin.open("rb") if previous_bin is not None else None
        try:
            for unit in units:
                item = unit.to_dict()
                vector = item.pop("embedding", None)
                fingerprint = _text_fingerprint(unit)
                if vector:
                    packed = _pack_vector(vector)
                    dim = len(vector)
                else:
                    # 只有在「磁盘上的向量就是这段文本的向量」时才沿用；否则留空，
                    # 由索引器按当前文本重算。少了这道校验，编辑过的单元会一直带
                    # 着旧向量参与语义检索。
                    packed, dim, previous_print = _read_vector_bytes(
                        handle, previous_index.get(unit.unit_id)
                    )
                    if packed and previous_print != fingerprint:
                        packed, dim = b"", 0
                if not packed and inline:
                    legacy = inline.get(unit.unit_id)
                    if legacy:
                        packed = _pack_vector(legacy)
                        dim = len(legacy)
                if packed:
                    item["vector_offset"] = len(blob)
                    item["vector_dim"] = dim
                    item["vector_text"] = fingerprint
                    blob.extend(packed)
                items.append(item)
        finally:
            if handle is not None:
                handle.close()
        return items, bytes(blob)

    def _write_vector_blob(self, group_id: str, blob: bytes) -> str | None:
        if not blob:
            return None
        self._vectors_dir.mkdir(parents=True, exist_ok=True)
        name = f"{self._safe_name(group_id)}.{uuid.uuid4().hex}.bin"
        path = self._vectors_dir / name
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(blob)
        replace_with_retry(tmp, path)
        return name

    def _discard_vector_file(self, name: str | None, *, keep: str | None) -> None:
        if not name or name == keep:
            return
        try:
            (self._vectors_dir / name).unlink(missing_ok=True)
        except OSError as exc:  # pragma: no cover - 删不掉只是残留，不影响正确性
            logger.debug("Failed to remove stale vector file %s: %s", name, exc)

    def _read_vector_index(
        self, group_id: str
    ) -> tuple[str | None, Path | None, dict[str, tuple[int, int]], dict[str, list[float]]]:
        """读出当前 JSON 引用的向量文件、offset 映射，以及老格式的内联向量。

        第四个返回值是迁移存量数据的关键：改动前向量内联在 JSON 里，没有 sidecar。
        此时 ``units`` 是「不带向量读入」的，若把 ``None`` 当成「没有向量」，第一次
        落盘就会清空整组向量。内联向量在这里被读出来当作沿用来源。
        """
        data = self._read_payload(self._path(group_id))
        if data is None:
            return None, None, {}, {}
        items = [item for item in data.get("units", []) if isinstance(item, dict)]
        name = data.get(VECTOR_FILE_KEY)
        inline = {
            str(item["unit_id"]): item["embedding"]
            for item in items
            if isinstance(item.get("unit_id"), str) and isinstance(item.get("embedding"), list)
        }
        if not isinstance(name, str) or not name:
            return None, None, {}, inline
        return name, self._vectors_dir / name, _vector_index(items), inline

    def save_many_atomically(self, groups: dict[str, list[MemoryUnit]]) -> None:
        stage_dir = self._base_dir.parent / f".memory_units_stage_{uuid.uuid4().hex}"
        stage_dir.mkdir(parents=True)
        previous: dict[str, str | None] = {}
        new_names: dict[str, str | None] = {}
        try:
            staged: dict[str, Path] = {}
            staged_vectors: dict[str, Path] = {}
            for group_id, units in groups.items():
                old_name, old_bin, old_index, inline = self._read_vector_index(group_id)
                items, blob = self._serialize(units, old_bin, old_index, inline)
                payload: dict[str, Any] = {
                    "group_id": group_id,
                    UNITS_FORMAT_KEY: UNITS_FORMAT_VERSION,
                    "units": items,
                }
                new_name: str | None = None
                if blob:
                    new_name = f"{self._safe_name(group_id)}.{uuid.uuid4().hex}.bin"
                    vector_path = stage_dir / new_name
                    vector_path.write_bytes(blob)
                    staged_vectors[group_id] = vector_path
                    payload[VECTOR_FILE_KEY] = new_name
                path = stage_dir / f"{self._safe_name(group_id)}.json"
                path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                staged[group_id] = path
                previous[group_id] = old_name
                new_names[group_id] = new_name
            self._vectors_dir.mkdir(parents=True, exist_ok=True)
            self._base_dir.mkdir(parents=True, exist_ok=True)
            for vector_path in staged_vectors.values():
                replace_with_retry(vector_path, self._vectors_dir / vector_path.name)
            for group_id, path in staged.items():
                self._replace_staged(path, self._path(group_id))
            for group_id, old_name in previous.items():
                self._discard_vector_file(old_name, keep=new_names.get(group_id))
        finally:
            shutil.rmtree(stage_dir, ignore_errors=True)
        for group_id, units in groups.items():
            self._note_group(group_id, bool(units))

    @staticmethod
    def _replace_staged(staged: Path, destination: Path) -> None:
        replace_with_retry(staged, destination)

    def _path(self, group_id: str) -> Path:
        return self._base_dir / f"{self._safe_name(group_id)}.json"

    @staticmethod
    def _safe_name(name: str) -> str:
        base = re.sub(r"[^a-zA-Z0-9_\-\u4e00-\u9fff]+", "_", name.strip())
        base = re.sub(r"_+", "_", base).strip("_")
        return base or "default"


def _vector_index(items: list[dict[str, Any]]) -> dict[str, tuple[int, int, str]]:
    """从单元元数据里抽出 unit_id → (offset, dim, text_fingerprint)。"""
    index: dict[str, tuple[int, int, str]] = {}
    for item in items:
        unit_id = item.get("unit_id")
        offset = item.get("vector_offset")
        dim = item.get("vector_dim")
        fingerprint = item.get("vector_text")
        if (
            isinstance(unit_id, str)
            and isinstance(offset, int)
            and isinstance(dim, int)
            and dim > 0
            and isinstance(fingerprint, str)
        ):
            index[unit_id] = (offset, dim, fingerprint)
    return index


def _text_fingerprint(unit: MemoryUnit) -> str:
    """向量对应文本的指纹。

    它让「元数据变了但向量没重算」不可能悄悄发生：沿用旧向量前必须指纹相等，否则
    宁可不带向量（下次加载时按当前文本重算），也不能把过期向量当成当前向量。
    """
    return hashlib.sha256(embedding_text(unit).encode("utf-8")).hexdigest()[:16]


def _pack_vector(vector: list[float]) -> bytes:
    return array(_VECTOR_TYPECODE, [float(value) for value in vector]).tobytes()


def _read_vector_bytes(handle: Any, entry: tuple[int, int, str] | None) -> tuple[bytes, int, str]:
    """按索引读出一段向量字节；越界或缺失一律当作没有向量。"""
    if entry is None or handle is None:
        return b"", 0, ""
    offset, dim, fingerprint = entry
    handle.seek(offset)
    data = handle.read(dim * _VECTOR_ITEM_BYTES)
    if len(data) != dim * _VECTOR_ITEM_BYTES:
        logger.warning("Memory unit vector truncated at offset %d (dim %d)", offset, dim)
        return b"", 0, ""
    return data, dim, fingerprint


def _read_vector(handle: Any, entry: tuple[int, int, str] | None, unit: MemoryUnit) -> bool:
    """把一段向量写回 ``unit.embedding``；指纹不符或读取失败返回 False。"""
    data, _dim, fingerprint = _read_vector_bytes(handle, entry)
    if not data or fingerprint != _text_fingerprint(unit):
        return False
    vector = array(_VECTOR_TYPECODE)
    vector.frombytes(data)
    unit.embedding = list(vector)
    return True
