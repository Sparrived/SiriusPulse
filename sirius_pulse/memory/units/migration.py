"""一次性迁移：把旧的 ``memory_units/*.json`` + 向量 sidecar 导入 ``persona.db``。

旧布局有两代：

- v1：向量内联在 JSON 的 ``embedding`` 里；
- v2：JSON 只有元数据，``vector_file`` 指向 ``memory_units/vectors/*.bin``，每条单元带
  ``vector_offset`` / ``vector_dim`` / ``vector_text``（float32 打包）。

迁移在 ``MemoryUnitStore`` 第一次访问库时自动进行，可以安全地重复执行：

- 每个导入成功的文件按 (文件名, 大小, mtime) 记进 ``memory_unit_migrations``，记录和
  导入在同一个事务里提交；再遇到同一份文件只会把它挪去备份，不会再导一遍——哪怕
  期间有人在 WebUI 里删改过这些单元，也不会被旧文件「复活」或覆盖。
- 库里已有同一 (群, 单元 ID) 的行一律保留，不拿旧文件覆盖。
- 导入后的原文件**不删除**，整体挪到 ``backups/memory_units_json/<时间戳>/``。
- 读不出来（I/O 失败，常见于杀软短暂占用）的文件原地不动，下次启动再试；内容损坏的
  文件导不进来，但同样挪进备份留档，不会丢。
- 引擎和 WebUI 两个进程同时迁移也没关系：导入在 ``BEGIN IMMEDIATE`` 里先查记录，只会
  有一方真正导入；挪文件时对方已挪走就跳过。
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

import numpy as np

from sirius_pulse.memory.units.models import MemoryUnit

logger = logging.getLogger(__name__)

LEGACY_DIR_NAME = "memory_units"
LEGACY_VECTORS_DIR_NAME = "vectors"
BACKUP_DIR = Path("backups") / "memory_units_json"

_PayloadStatus = Literal["ok", "missing", "corrupt", "unreadable"]


@dataclass(slots=True)
class MigrationReport:
    imported_groups: list[str] = field(default_factory=list)
    imported_units: int = 0
    skipped_files: list[str] = field(default_factory=list)
    corrupt_files: list[str] = field(default_factory=list)
    deferred_files: list[str] = field(default_factory=list)
    backup_dir: Path | None = None


def legacy_dir(work_path: Path) -> Path:
    return Path(work_path) / LEGACY_DIR_NAME


def legacy_files_present(work_path: Path) -> bool:
    base = legacy_dir(work_path)
    if not base.is_dir():
        return False
    return any(base.glob("*.json"))


def migrate_legacy_files(work_path: Path, conn: sqlite3.Connection) -> MigrationReport:
    """导入 ``work_path/memory_units`` 下的旧文件；``conn`` 须为 autocommit 连接且已建表。"""
    from sirius_pulse.memory.units.store import MemoryUnitTransaction

    work_path = Path(work_path)
    base = legacy_dir(work_path)
    report = MigrationReport()
    files = sorted(base.glob("*.json")) if base.is_dir() else []
    if not files:
        return report
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    backup_root = work_path / BACKUP_DIR / stamp
    report.backup_dir = backup_root

    for path in files:
        try:
            stat = path.stat()
        except FileNotFoundError:
            continue  # 另一个进程刚挪走
        except OSError:
            report.deferred_files.append(path.name)
            continue
        status, data = _load_payload(path)
        if status == "missing":
            continue
        if status == "unreadable":
            logger.warning("旧记忆单元文件 %s 暂时读不出来，下次启动再迁移", path)
            report.deferred_files.append(path.name)
            continue
        if status == "corrupt" or data is None:
            logger.error("旧记忆单元文件 %s 已损坏，无法导入，原样移入备份 %s", path, backup_root)
            report.corrupt_files.append(path.name)
            _move_to_backup(path, backup_root / path.name)
            continue

        group_id = str(data.get("group_id") or path.stem)
        units = _units_from_payload(base, data, group_id)
        conn.execute("BEGIN IMMEDIATE")
        try:
            done = conn.execute(
                "SELECT 1 FROM memory_unit_migrations "
                "WHERE source = ? AND size = ? AND mtime_ns = ?",
                (path.name, stat.st_size, stat.st_mtime_ns),
            ).fetchone()
            if done is None:
                tx = MemoryUnitTransaction(conn)
                tx.apply_changes(group_id, units, {})
                conn.execute(
                    "INSERT INTO memory_unit_migrations "
                    "(source, size, mtime_ns, group_id, units, migrated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        path.name,
                        stat.st_size,
                        stat.st_mtime_ns,
                        group_id,
                        len(units),
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        if done is None:
            report.imported_groups.append(group_id)
            report.imported_units += len(units)
        else:
            report.skipped_files.append(path.name)
        _move_to_backup(path, backup_root / path.name)
        vector_name = data.get("vector_file")
        if isinstance(vector_name, str) and vector_name:
            vector_path = base / LEGACY_VECTORS_DIR_NAME / vector_name
            _move_to_backup(vector_path, backup_root / LEGACY_VECTORS_DIR_NAME / vector_name)

    # 有文件这次没读出来时不动向量目录：不知道它引用的是哪个向量文件。
    _sweep_leftovers(base, backup_root, include_vectors=not report.deferred_files)
    if report.imported_groups or report.corrupt_files or report.skipped_files:
        logger.info(
            "记忆单元已迁入 persona.db：%d 个群、%d 条；原文件备份在 %s",
            len(report.imported_groups),
            report.imported_units,
            backup_root,
        )
    return report


def _units_from_payload(base: Path, data: dict[str, Any], group_id: str) -> list[MemoryUnit]:
    """把一份旧 JSON 解析成单元，并尽量挂上它的向量（v1 内联 / v2 sidecar）。"""
    from sirius_pulse.memory.units.store import text_fingerprint

    items = [item for item in data.get("units", []) if isinstance(item, dict)]
    blob: np.ndarray | None = None
    vector_name = data.get("vector_file")
    if isinstance(vector_name, str) and vector_name:
        vector_path = base / LEGACY_VECTORS_DIR_NAME / vector_name
        try:
            blob = np.fromfile(vector_path, dtype=np.float32)
        except OSError as exc:
            logger.warning("旧向量文件 %s 读不出来，这些单元将按文本重算向量: %s", vector_path, exc)
    units: list[MemoryUnit] = []
    item_bytes = np.dtype(np.float32).itemsize
    for item in items:
        unit_id = item.get("unit_id")
        if not isinstance(unit_id, str) or not unit_id:
            continue
        inline = item.get("embedding")
        try:
            unit = MemoryUnit.from_dict(
                {
                    **{key: value for key, value in item.items() if key != "embedding"},
                    "unit_id": unit_id,
                    "group_id": group_id,
                    "created_at": str(item.get("created_at") or ""),
                }
            )
        except TypeError:
            logger.warning("跳过无法解析的旧记忆单元 %s/%s", group_id, unit_id)
            continue
        unit.group_id = group_id
        unit.embedding = None
        if isinstance(inline, list) and inline:
            unit.embedding = [float(value) for value in inline]
        elif blob is not None:
            offset, dim, fingerprint = (
                item.get("vector_offset"),
                item.get("vector_dim"),
                item.get("vector_text"),
            )
            if (
                isinstance(offset, int)
                and isinstance(dim, int)
                and dim > 0
                and fingerprint == text_fingerprint(unit)
            ):
                start = offset // item_bytes
                if start + dim <= blob.shape[0]:
                    unit.embedding = blob[start : start + dim].tolist()
        units.append(unit)
    return units


def _load_payload(path: Path) -> tuple[_PayloadStatus, dict[str, Any] | None]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "missing", None
    except UnicodeDecodeError:
        return "corrupt", None
    except OSError:
        return "unreadable", None
    try:
        data = json.loads(text)
    except (json.JSONDecodeError, TypeError):
        return "corrupt", None
    if not isinstance(data, dict):
        return "corrupt", None
    return "ok", data


def _move_to_backup(source: Path, destination: Path) -> None:
    try:
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(source, destination)
    except FileNotFoundError:
        return  # 另一个进程已经挪走
    except OSError as exc:
        # 挪不动只是原文件还留在原处；迁移记录已提交，下次启动只会补挪、不会重导。
        logger.warning("旧记忆单元文件 %s 暂时挪不进备份: %s", source, exc)


def _sweep_leftovers(base: Path, backup_root: Path, *, include_vectors: bool) -> None:
    """旧存储留下的残余（损坏留档、临时文件、没人引用的向量文件）也一并移进备份。"""
    for path in list(base.glob("*.json.*")):
        if path.is_file():
            _move_to_backup(path, backup_root / path.name)
    vectors = base / LEGACY_VECTORS_DIR_NAME
    if include_vectors and vectors.is_dir():
        for path in list(vectors.iterdir()):
            if path.is_file():
                _move_to_backup(path, backup_root / LEGACY_VECTORS_DIR_NAME / path.name)
        try:
            vectors.rmdir()
        except OSError:
            pass
