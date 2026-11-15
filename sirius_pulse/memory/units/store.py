"""SQLite storage for checkpoint memory units.

单元存放在人格库 ``persona.db`` 里，两张表：

- ``memory_units``：一行一条单元。元数据是普通列，列表字段存 JSON 文本；
  ``revision`` 供逐行乐观并发，``content_hash`` 让「内容没变」的写入直接跳过。
- ``memory_unit_vectors``：float32 向量 BLOB 与它所编码文本的指纹。和元数据分表，
  是因为 1024 维向量 4 KiB，是其余字段的十倍；放在同一行会让「只读摘要」的扫描
  被溢出页拖慢，而线上九成以上的向量属于永远不会被注入的退休单元。

为什么不用 ``PersonaDatabase`` 的共享连接：那条连接被好几个存储层共用、没有任何锁，
一个线程里的 ``BEGIN … COMMIT`` 会把另一个存储层写了一半的东西一起提交。这里每次
操作开一条自己的短连接：WAL 下读写互不阻塞，写事务用 ``BEGIN IMMEDIATE`` 一开始就
拿写锁，跨进程（WebUI）的并发由 SQLite 自己排队。
"""

from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
import threading
from collections import Counter
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from sirius_pulse.memory.units.models import MemoryUnit, embedding_text
from sirius_pulse.utils.layout import WorkspaceLayout

logger = logging.getLogger(__name__)

#: 写锁最多等这么久。引擎和 WebUI 的写事务都很短，等不到通常意味着别人卡死了。
_BUSY_TIMEOUT_SECONDS = 30.0
#: ``IN (...)`` 一次带的参数个数，远低于 SQLite 的变量上限。
_IN_CHUNK = 500
#: 冷层从不收录的退休原因，与 ``indexer._NEVER_COLD_REASONS`` 一致；这里只做预筛。
_NEVER_COLD_REASONS = ("model", "manual", "expired")

_LIST_FIELDS = (
    "participants",
    "topics",
    "keywords",
    "retrieval_terms",
    "identity_aliases",
    "source_ids",
)
_TEXT_FIELDS = (
    "created_at",
    "unit_type",
    "scope",
    "scope_id",
    "summary",
    "event_time",
    "valid_until",
    "status",
    "lifespan",
)
#: 元数据列，顺序即写入顺序。
_META_COLUMNS = (
    *_TEXT_FIELDS,
    *_LIST_FIELDS,
    "salience",
    "confidence",
    "should_prompt",
    "metadata",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_units (
    group_id         TEXT    NOT NULL,
    unit_id          TEXT    NOT NULL,
    position         INTEGER NOT NULL DEFAULT 0,
    revision         INTEGER NOT NULL DEFAULT 1,
    content_hash     TEXT    NOT NULL DEFAULT '',
    updated_at       TEXT    NOT NULL DEFAULT '',
    created_at       TEXT    NOT NULL DEFAULT '',
    unit_type        TEXT    NOT NULL DEFAULT 'event',
    scope            TEXT    NOT NULL DEFAULT 'group',
    scope_id         TEXT    NOT NULL DEFAULT '',
    summary          TEXT    NOT NULL DEFAULT '',
    event_time       TEXT    NOT NULL DEFAULT '',
    valid_until      TEXT    NOT NULL DEFAULT '',
    status           TEXT    NOT NULL DEFAULT '',
    lifespan         TEXT    NOT NULL DEFAULT 'medium',
    participants     TEXT    NOT NULL DEFAULT '[]',
    topics           TEXT    NOT NULL DEFAULT '[]',
    keywords         TEXT    NOT NULL DEFAULT '[]',
    retrieval_terms  TEXT    NOT NULL DEFAULT '[]',
    identity_aliases TEXT    NOT NULL DEFAULT '[]',
    source_ids       TEXT    NOT NULL DEFAULT '[]',
    salience         REAL    NOT NULL DEFAULT 0.5,
    confidence       REAL    NOT NULL DEFAULT 0.7,
    should_prompt    INTEGER NOT NULL DEFAULT 1,
    metadata         TEXT    NOT NULL DEFAULT '{}',
    PRIMARY KEY (group_id, unit_id)
);
CREATE INDEX IF NOT EXISTS idx_memory_units_prompt
    ON memory_units (group_id, should_prompt);
CREATE INDEX IF NOT EXISTS idx_memory_units_unit_id ON memory_units (unit_id);

CREATE TABLE IF NOT EXISTS memory_unit_vectors (
    group_id TEXT    NOT NULL,
    unit_id  TEXT    NOT NULL,
    dim      INTEGER NOT NULL,
    text_fp  TEXT    NOT NULL,
    vector   BLOB    NOT NULL,
    PRIMARY KEY (group_id, unit_id),
    FOREIGN KEY (group_id, unit_id)
        REFERENCES memory_units (group_id, unit_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS memory_unit_migrations (
    source      TEXT    NOT NULL,
    size        INTEGER NOT NULL,
    mtime_ns    INTEGER NOT NULL,
    group_id    TEXT    NOT NULL,
    units       INTEGER NOT NULL,
    migrated_at TEXT    NOT NULL,
    PRIMARY KEY (source, size, mtime_ns)
);
"""


class MemoryUnitStoreError(RuntimeError):
    """数据库此刻读写不了（被锁太久、文件损坏、磁盘出错）。

    读失败绝不能被当成「这个群没有记忆」：调用方要么降级为本轮不召回，要么放弃这次
    写入稍后重试，不能拿一个假的空列表去覆盖。
    """


@dataclass(frozen=True, slots=True)
class ApplyOutcome:
    """``apply_changes`` 的结果。

    ``conflicts``：因为别人（通常是 WebUI 里的真人）先改过、删过同一条而放弃的改动数。
    ``foreign``：库里还有本次读取之后别人新加的单元。两者任一非零，调用方手里的列表
    就不再等于库里的现状，需要重读。
    """

    conflicts: int = 0
    foreign: bool = False

    @property
    def in_sync(self) -> bool:
        return self.conflicts == 0 and not self.foreign


class MemoryUnitStore:
    """SQLite-backed storage for memory units (``persona.db``).

    读接口不建库：库文件不存在、也没有待迁移的旧文件时，读到的就是空。写接口按需
    建表。第一次访问时若 ``memory_units/*.json`` 还在，会先把它们导入（见
    ``sirius_pulse.memory.units.migration``）。
    """

    def __init__(
        self, work_path: Path | WorkspaceLayout, *, db_path: Path | str | None = None
    ) -> None:
        layout = work_path if isinstance(work_path, WorkspaceLayout) else WorkspaceLayout(work_path)
        self._work_path = layout.work_path
        self._db_path = Path(db_path) if db_path is not None else self._work_path / "persona.db"
        self._ready = False
        self._ready_lock = threading.Lock()
        #: 有单元的分组；检索路径每条消息都会问，由写入方增量维护。
        self._group_ids: set[str] | None = None
        self._group_lock = threading.Lock()

    @property
    def work_path(self) -> Path:
        return self._work_path

    @property
    def db_path(self) -> Path:
        return self._db_path

    # ------------------------------------------------------------------
    # 连接与建表
    # ------------------------------------------------------------------

    def _open(self) -> sqlite3.Connection:
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(
            str(self._db_path), timeout=_BUSY_TIMEOUT_SECONDS, isolation_level=None
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def _ensure_ready(self, *, create: bool) -> bool:
        """建表并导入旧文件；库不存在且不需要创建时返回 False（读路径据此返回空）."""
        if self._ready:
            return True
        from sirius_pulse.memory.units.migration import legacy_files_present, migrate_legacy_files

        legacy = legacy_files_present(self._work_path)
        if not create and not legacy and not self._db_path.exists():
            return False
        with self._ready_lock:
            if self._ready:
                return True
            try:
                conn = self._open()
            except sqlite3.Error as exc:
                raise MemoryUnitStoreError(f"cannot open {self._db_path}: {exc}") from exc
            try:
                try:
                    conn.execute("PRAGMA journal_mode=WAL")
                except sqlite3.OperationalError:
                    # 别的连接正占着库时切不了模式；库的主人（PersonaDatabase）会设。
                    pass
                conn.executescript(_SCHEMA)
                if legacy:
                    migrate_legacy_files(self._work_path, conn)
            except sqlite3.Error as exc:
                raise MemoryUnitStoreError(f"cannot prepare {self._db_path}: {exc}") from exc
            finally:
                conn.close()
            self._ready = True
        return True

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection | None]:
        """只读连接；库还不存在时给 None。"""
        if not self._ensure_ready(create=False):
            yield None
            return
        try:
            conn = self._open()
        except sqlite3.Error as exc:
            raise MemoryUnitStoreError(f"cannot open {self._db_path}: {exc}") from exc
        try:
            yield conn
        except sqlite3.Error as exc:
            raise MemoryUnitStoreError(f"memory unit read failed: {exc}") from exc
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator["MemoryUnitTransaction"]:
        """一个写事务（``BEGIN IMMEDIATE``）。正常退出提交，抛错回滚。

        引擎与 WebUI 的所有改动都经过这里：同一时刻只有一个写事务，读到的就是提交
        时的现状，不再需要文件锁和「写回前比对版本」。
        """
        self._ensure_ready(create=True)
        try:
            conn = self._open()
        except sqlite3.Error as exc:
            raise MemoryUnitStoreError(f"cannot open {self._db_path}: {exc}") from exc
        tx = MemoryUnitTransaction(conn)
        try:
            try:
                conn.execute("BEGIN IMMEDIATE")
            except sqlite3.Error as exc:
                raise MemoryUnitStoreError(f"cannot start memory unit write: {exc}") from exc
            try:
                yield tx
                touched = tx.group_presence()
                conn.execute("COMMIT")
            except BaseException as exc:
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                if isinstance(exc, sqlite3.Error):
                    raise MemoryUnitStoreError(f"memory unit write failed: {exc}") from exc
                raise
        finally:
            conn.close()
        for group_id, has_units in touched.items():
            self._note_group(group_id, has_units)

    # ------------------------------------------------------------------
    # 分组
    # ------------------------------------------------------------------

    def _note_group(self, group_id: str, has_units: bool) -> None:
        with self._group_lock:
            if self._group_ids is None:
                return
            if has_units:
                self._group_ids.add(group_id)
            else:
                self._group_ids.discard(group_id)

    def forget_group_cache(self) -> None:
        """丢弃分组缓存，下次重新查库（别的进程改过库之后调用）。"""
        with self._group_lock:
            self._group_ids = None

    def list_group_ids(self) -> list[str]:
        """所有有单元的分组。查一次库后缓存，由本实例的写入增量维护。"""
        with self._group_lock:
            cached = None if self._group_ids is None else sorted(self._group_ids)
        if cached is not None:
            return cached
        with self._read() as conn:
            rows = (
                []
                if conn is None
                else conn.execute("SELECT DISTINCT group_id FROM memory_units").fetchall()
            )
        group_ids = {str(row[0]) for row in rows}
        with self._group_lock:
            if self._group_ids is None:
                self._group_ids = group_ids
            return sorted(self._group_ids)

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------

    def load(
        self,
        group_id: str,
        *,
        with_embeddings: bool = True,
        prompt_only: bool = False,
    ) -> list[MemoryUnit]:
        """读取某分组的单元（按写入顺序）。

        ``with_embeddings=False`` 完全不碰向量表——常驻检索路径只该给可注入单元补
        向量。``prompt_only`` 只取 ``should_prompt=1`` 的单元。
        """
        return self.load_with_revisions(
            group_id, with_embeddings=with_embeddings, prompt_only=prompt_only
        )[0]

    def load_with_revisions(
        self,
        group_id: str,
        *,
        with_embeddings: bool = True,
        prompt_only: bool = False,
    ) -> tuple[list[MemoryUnit], dict[str, int]]:
        """同 ``load``，另给出每条单元当前的 ``revision``，供 ``apply_changes`` 使用。"""
        with self._read() as conn:
            if conn is None:
                return [], {}
            return _select_units(
                conn, group_id, with_embeddings=with_embeddings, prompt_only=prompt_only
            )

    def load_all(self, group_id: str | None = None) -> list[MemoryUnit]:
        """全部（或某一分组的）单元元数据，不带向量。供 WebUI 与离线汇总使用。"""
        with self._read() as conn:
            if conn is None:
                return []
            sql = f"SELECT {_SELECT_META} FROM memory_units"
            params: tuple[Any, ...] = ()
            if group_id is not None:
                sql += " WHERE group_id = ?"
                params = (group_id,)
            sql += " ORDER BY group_id, position, rowid"
            return [_unit_from_row(row) for row in conn.execute(sql, params)]

    def get_unit(self, group_id: str, unit_id: str) -> MemoryUnit | None:
        with self._read() as conn:
            if conn is None:
                return None
            return _get_unit(conn, group_id, unit_id)

    def count(self) -> int:
        with self._read() as conn:
            if conn is None:
                return 0
            return int(conn.execute("SELECT COUNT(*) FROM memory_units").fetchone()[0])

    def created_at_values(self) -> list[str]:
        with self._read() as conn:
            if conn is None:
                return []
            return [str(row[0]) for row in conn.execute("SELECT created_at FROM memory_units")]

    def source_ids(self, group_id: str) -> set[str]:
        """某分组全部单元（含退休）引用的原始条目 ID；只读一列，不解析别的字段。"""
        with self._read() as conn:
            if conn is None:
                return set()
            sources: set[str] = set()
            for (raw,) in conn.execute(
                "SELECT source_ids FROM memory_units WHERE group_id = ?", (group_id,)
            ):
                sources.update(str(item) for item in _json_list(raw))
            return sources

    def hydrate_embeddings(
        self,
        group_id: str,
        units: list[MemoryUnit],
        *,
        unit_ids: set[str] | None = None,
    ) -> int:
        """给 ``units`` 里还没有向量的单元补上库里的向量，返回补上的条数。

        只查 ``unit_ids``（缺省为全部尚未带向量的）对应的那几行；文本指纹对不上的
        向量（文本改过、向量没重算）不会被挂上去。
        """
        pending = {
            unit.unit_id: unit
            for unit in units
            if not unit.embedding and (unit_ids is None or unit.unit_id in unit_ids)
        }
        if not pending:
            return 0
        restored = 0
        with self._read() as conn:
            if conn is None:
                return 0
            for chunk in _chunks(list(pending), _IN_CHUNK):
                marks = ",".join("?" * len(chunk))
                rows = conn.execute(
                    "SELECT unit_id, text_fp, vector FROM memory_unit_vectors "
                    f"WHERE group_id = ? AND unit_id IN ({marks})",
                    (group_id, *chunk),
                )
                for row in rows:
                    unit = pending.get(row["unit_id"])
                    if unit is not None and row["text_fp"] == text_fingerprint(unit):
                        unit.embedding = _unpack(row["vector"]).tolist()
                        restored += 1
        return restored

    def read_retired_vectors(self, group_id: str) -> tuple[list[MemoryUnit], np.ndarray]:
        """读出某分组退休单元的元数据与向量矩阵（float32），供冷层检索。

        只查 ``should_prompt=0`` 且退休原因可能进冷层的行；向量直接拼成矩阵，不变成
        Python 浮点列表（每维 32 字节对 4 字节）。维度不一的取多数那一种。
        """
        with self._read() as conn:
            if conn is None:
                return [], np.zeros((0, 0), dtype=np.float32)
            marks = ",".join("?" * len(_NEVER_COLD_REASONS))
            rows = conn.execute(
                f"SELECT {_SELECT_META_U}, v.text_fp, v.vector FROM memory_units u "
                "JOIN memory_unit_vectors v ON v.group_id = u.group_id AND v.unit_id = u.unit_id "
                "WHERE u.group_id = ? AND u.should_prompt = 0 AND COALESCE("
                "CASE WHEN json_valid(u.metadata) THEN json_extract(u.metadata, '$.retired') END,"
                f" '') NOT IN ({marks}) ORDER BY u.position, u.rowid",
                (group_id, *_NEVER_COLD_REASONS),
            ).fetchall()
        units: list[MemoryUnit] = []
        vectors: list[np.ndarray] = []
        for row in rows:
            unit = _unit_from_row(row)
            if row["text_fp"] != text_fingerprint(unit):
                continue
            vector = _unpack(row["vector"])
            if vector.size:
                units.append(unit)
                vectors.append(vector)
        if not vectors:
            return [], np.zeros((0, 0), dtype=np.float32)
        dim = Counter(vector.shape[0] for vector in vectors).most_common(1)[0][0]
        keep = [index for index, vector in enumerate(vectors) if vector.shape[0] == dim]
        return [units[i] for i in keep], np.vstack([vectors[i] for i in keep])

    # ------------------------------------------------------------------
    # 写
    # ------------------------------------------------------------------

    def save(
        self, group_id: str, units: list[MemoryUnit], *, replace_vectors: bool = False
    ) -> None:
        """让分组的内容恰好等于 ``units``（顺序即位置）。

        只写变了的行：内容哈希相同且位置没变的单元原样不动。没有向量的单元沿用库里
        「同一段文本」的向量；文本变了的单元向量作废，等索引器按新文本重算。
        """
        with self.transaction() as tx:
            self._replace_group_rows(tx, group_id, units, replace_vectors)

    def save_many_atomically(self, groups: dict[str, list[MemoryUnit]]) -> None:
        """在同一个事务里替换多个分组：要么全部生效，要么一个都不变。"""
        with self.transaction() as tx:
            for group_id, units in groups.items():
                self._replace_group_rows(tx, group_id, units, False)

    def _replace_group_rows(
        self,
        tx: "MemoryUnitTransaction",
        group_id: str,
        units: list[MemoryUnit],
        replace_vectors: bool,
    ) -> None:
        tx.replace_group(group_id, units, replace_vectors=replace_vectors)

    def apply_changes(
        self,
        group_id: str,
        units: list[MemoryUnit],
        base: Mapping[str, int],
        *,
        replace_vectors: bool = False,
    ) -> ApplyOutcome:
        """把「从 ``base`` 读出之后改成 ``units``」的改动逐行写回。

        ``base`` 是读取时每条单元的 revision（``load_with_revisions``）。规则：

        - ``units`` 里的新单元：插入（别人已用同一 ID 建过时保留别人的）。
        - 改过的单元：只有库里那行仍是读取时的 revision 才写；别人先改过就保留别人的
          ——那几乎总是 WebUI 里的真人编辑。
        - ``base`` 里有、``units`` 里没有的单元：库里仍是读取时那一版才删。
        - 不在 ``base`` 里的库内单元（读取之后别人加的）一概不动。
        """
        with self.transaction() as tx:
            return tx.apply_changes(group_id, units, base, replace_vectors=replace_vectors)


class MemoryUnitTransaction:
    """``MemoryUnitStore.transaction()`` 里可用的读写操作；只在 ``with`` 块内有效。"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._touched: set[str] = set()

    # -- 读 --------------------------------------------------------------

    def load(self, group_id: str, *, with_embeddings: bool = False) -> list[MemoryUnit]:
        return _select_units(self._conn, group_id, with_embeddings=with_embeddings)[0]

    def get(
        self, group_id: str, unit_id: str, *, with_embedding: bool = False
    ) -> MemoryUnit | None:
        unit = _get_unit(self._conn, group_id, unit_id)
        if unit is not None and with_embedding:
            row = self._conn.execute(
                "SELECT text_fp, vector FROM memory_unit_vectors "
                "WHERE group_id = ? AND unit_id = ?",
                (group_id, unit_id),
            ).fetchone()
            if row is not None and row["text_fp"] == text_fingerprint(unit):
                unit.embedding = _unpack(row["vector"]).tolist()
        return unit

    def summaries(self, group_id: str) -> dict[str, str]:
        """unit_id → summary，只读两列（WebUI 新建时查重用）。"""
        return {
            str(row[0]): str(row[1])
            for row in self._conn.execute(
                "SELECT unit_id, summary FROM memory_units WHERE group_id = ? "
                "ORDER BY position, rowid",
                (group_id,),
            )
        }

    def find_group(self, unit_id: str) -> str | None:
        row = self._conn.execute(
            "SELECT group_id FROM memory_units WHERE unit_id = ? ORDER BY group_id LIMIT 1",
            (unit_id,),
        ).fetchone()
        return None if row is None else str(row[0])

    # -- 写 --------------------------------------------------------------

    def upsert(self, group_id: str, unit: MemoryUnit, *, replace_vector: bool = False) -> None:
        """插入或覆盖一条单元；内容变了 revision 加一。新单元排在分组末尾。"""
        encoded = _encode_unit(unit)
        digest = _content_hash(encoded)
        existing = self._conn.execute(
            "SELECT content_hash FROM memory_units WHERE group_id = ? AND unit_id = ?",
            (group_id, unit.unit_id),
        ).fetchone()
        if existing is None:
            self._insert(group_id, unit.unit_id, encoded, digest, self._next_position(group_id))
        elif existing[0] != digest:
            self._update(group_id, unit.unit_id, encoded, digest)
        self._sync_vector(
            group_id, unit, self._vector_state(group_id, unit.unit_id), replace_vector
        )
        self._touched.add(group_id)

    def delete(self, group_id: str, unit_id: str) -> bool:
        cursor = self._conn.execute(
            "DELETE FROM memory_units WHERE group_id = ? AND unit_id = ?", (group_id, unit_id)
        )
        self._touched.add(group_id)
        return cursor.rowcount > 0

    def replace_group(
        self, group_id: str, units: list[MemoryUnit], *, replace_vectors: bool = False
    ) -> None:
        rows = self._row_states(group_id)
        vectors = self._vector_states(group_id)
        seen: set[str] = set()
        for position, unit in enumerate(units):
            if unit.unit_id in seen:
                continue
            seen.add(unit.unit_id)
            encoded = _encode_unit(unit)
            digest = _content_hash(encoded)
            row = rows.get(unit.unit_id)
            if row is None:
                self._insert(group_id, unit.unit_id, encoded, digest, position)
            elif row[1] != digest:
                self._update(group_id, unit.unit_id, encoded, digest, position=position)
            elif row[2] != position:
                self._conn.execute(
                    "UPDATE memory_units SET position = ? WHERE group_id = ? AND unit_id = ?",
                    (position, group_id, unit.unit_id),
                )
            self._sync_vector(group_id, unit, vectors.get(unit.unit_id), replace_vectors)
        stale = [unit_id for unit_id in rows if unit_id not in seen]
        for chunk in _chunks(stale, _IN_CHUNK):
            marks = ",".join("?" * len(chunk))
            self._conn.execute(
                f"DELETE FROM memory_units WHERE group_id = ? AND unit_id IN ({marks})",
                (group_id, *chunk),
            )
        self._touched.add(group_id)

    def apply_changes(
        self,
        group_id: str,
        units: list[MemoryUnit],
        base: Mapping[str, int],
        *,
        replace_vectors: bool = False,
    ) -> ApplyOutcome:
        rows = self._row_states(group_id)
        vectors = self._vector_states(group_id)
        next_position = max((row[2] for row in rows.values()), default=-1) + 1
        conflicts = 0
        seen: set[str] = set()
        for unit in units:
            if unit.unit_id in seen:
                continue
            seen.add(unit.unit_id)
            encoded = _encode_unit(unit)
            digest = _content_hash(encoded)
            row = rows.get(unit.unit_id)
            base_revision = base.get(unit.unit_id)
            if base_revision is None:
                if row is not None:
                    if row[1] != digest:
                        conflicts += 1
                        continue
                else:
                    self._insert(group_id, unit.unit_id, encoded, digest, next_position)
                    next_position += 1
            elif row is None:
                # 读取之后被别人删掉了：保持删除，不让我们手里的旧副本把它复活。
                conflicts += 1
                continue
            elif row[1] != digest:
                if row[0] != base_revision:
                    conflicts += 1
                    continue
                self._update(group_id, unit.unit_id, encoded, digest)
            elif row[0] != base_revision:
                # 别人也改过，但改成了和我们一样的内容：照常补向量即可。
                pass
            self._sync_vector(group_id, unit, vectors.get(unit.unit_id), replace_vectors)
        removed = [
            unit_id for unit_id, revision in base.items() if unit_id not in seen and unit_id in rows
        ]
        for unit_id in removed:
            if rows[unit_id][0] != base[unit_id]:
                conflicts += 1
                continue
            self._conn.execute(
                "DELETE FROM memory_units WHERE group_id = ? AND unit_id = ?", (group_id, unit_id)
            )
        foreign = any(unit_id not in seen and unit_id not in base for unit_id in rows)
        self._touched.add(group_id)
        return ApplyOutcome(conflicts=conflicts, foreign=foreign)

    def group_presence(self) -> dict[str, bool]:
        """本事务改过的分组此刻是否还有单元（提交前调用，供分组缓存增量维护）。"""
        return {
            group_id: self._conn.execute(
                "SELECT 1 FROM memory_units WHERE group_id = ? LIMIT 1", (group_id,)
            ).fetchone()
            is not None
            for group_id in self._touched
        }

    # -- 内部 ------------------------------------------------------------

    def _row_states(self, group_id: str) -> dict[str, tuple[int, str, int]]:
        """unit_id → (revision, content_hash, position)；不解析任何 JSON。"""
        return {
            str(row[0]): (int(row[1]), str(row[2]), int(row[3]))
            for row in self._conn.execute(
                "SELECT unit_id, revision, content_hash, position FROM memory_units "
                "WHERE group_id = ?",
                (group_id,),
            )
        }

    def _vector_states(self, group_id: str) -> dict[str, tuple[str, int]]:
        return {
            str(row[0]): (str(row[1]), int(row[2]))
            for row in self._conn.execute(
                "SELECT unit_id, text_fp, dim FROM memory_unit_vectors WHERE group_id = ?",
                (group_id,),
            )
        }

    def _vector_state(self, group_id: str, unit_id: str) -> tuple[str, int] | None:
        row = self._conn.execute(
            "SELECT text_fp, dim FROM memory_unit_vectors WHERE group_id = ? AND unit_id = ?",
            (group_id, unit_id),
        ).fetchone()
        return None if row is None else (str(row[0]), int(row[1]))

    def _next_position(self, group_id: str) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(MAX(position), -1) + 1 FROM memory_units WHERE group_id = ?",
            (group_id,),
        ).fetchone()
        return int(row[0])

    def _insert(
        self, group_id: str, unit_id: str, encoded: dict[str, Any], digest: str, position: int
    ) -> None:
        columns = ", ".join(_META_COLUMNS)
        marks = ", ".join("?" * len(_META_COLUMNS))
        self._conn.execute(
            "INSERT INTO memory_units (group_id, unit_id, position, revision, content_hash, "
            f"updated_at, {columns}) VALUES (?, ?, ?, 1, ?, ?, {marks})",
            (group_id, unit_id, position, digest, _now_iso(), *encoded.values()),
        )

    def _update(
        self,
        group_id: str,
        unit_id: str,
        encoded: dict[str, Any],
        digest: str,
        *,
        position: int | None = None,
    ) -> None:
        assignments = ", ".join(f"{column} = ?" for column in _META_COLUMNS)
        params: list[Any] = [*encoded.values(), digest, _now_iso()]
        sql = (
            f"UPDATE memory_units SET {assignments}, content_hash = ?, updated_at = ?, "
            "revision = revision + 1"
        )
        if position is not None:
            sql += ", position = ?"
            params.append(position)
        sql += " WHERE group_id = ? AND unit_id = ?"
        self._conn.execute(sql, (*params, group_id, unit_id))

    def _sync_vector(
        self,
        group_id: str,
        unit: MemoryUnit,
        existing: tuple[str, int] | None,
        replace: bool,
    ) -> None:
        """让库里的向量与单元当前文本一致。

        单元带着向量：库里没有、指纹或维度不同、或调用方要求替换（换模型重建）时写入。
        单元没带向量（常驻路径为省内存不带向量读入）：库里那份若正是这段文本的，原样
        沿用；文本已经变了就删掉，免得过期向量被当成当前事实参与检索。
        """
        fingerprint = text_fingerprint(unit)
        if unit.embedding:
            if replace or existing != (fingerprint, len(unit.embedding)):
                self._conn.execute(
                    "INSERT INTO memory_unit_vectors (group_id, unit_id, dim, text_fp, vector) "
                    "VALUES (?, ?, ?, ?, ?) ON CONFLICT (group_id, unit_id) DO UPDATE SET "
                    "dim = excluded.dim, text_fp = excluded.text_fp, vector = excluded.vector",
                    (
                        group_id,
                        unit.unit_id,
                        len(unit.embedding),
                        fingerprint,
                        pack_vector(unit.embedding),
                    ),
                )
        elif existing is not None and existing[0] != fingerprint:
            self._conn.execute(
                "DELETE FROM memory_unit_vectors WHERE group_id = ? AND unit_id = ?",
                (group_id, unit.unit_id),
            )


# ----------------------------------------------------------------------
# 行 <-> 单元
# ----------------------------------------------------------------------

_SELECT_META = ", ".join(("group_id", "unit_id", *_META_COLUMNS))
_SELECT_META_U = ", ".join(f"u.{column}" for column in ("group_id", "unit_id", *_META_COLUMNS))


def _select_units(
    conn: sqlite3.Connection,
    group_id: str,
    *,
    with_embeddings: bool,
    prompt_only: bool = False,
) -> tuple[list[MemoryUnit], dict[str, int]]:
    where = "u.group_id = ?" + (" AND u.should_prompt = 1" if prompt_only else "")
    if with_embeddings:
        sql = (
            f"SELECT {_SELECT_META_U}, u.revision, v.text_fp, v.vector FROM memory_units u "
            "LEFT JOIN memory_unit_vectors v ON v.group_id = u.group_id AND v.unit_id = u.unit_id "
            f"WHERE {where} ORDER BY u.position, u.rowid"
        )
    else:
        sql = (
            f"SELECT {_SELECT_META_U}, u.revision FROM memory_units u "
            f"WHERE {where} ORDER BY u.position, u.rowid"
        )
    units: list[MemoryUnit] = []
    revisions: dict[str, int] = {}
    for row in conn.execute(sql, (group_id,)):
        unit = _unit_from_row(row)
        if with_embeddings and row["vector"] is not None:
            if row["text_fp"] == text_fingerprint(unit):
                unit.embedding = _unpack(row["vector"]).tolist()
        units.append(unit)
        revisions[unit.unit_id] = int(row["revision"])
    return units, revisions


def _get_unit(conn: sqlite3.Connection, group_id: str, unit_id: str) -> MemoryUnit | None:
    row = conn.execute(
        f"SELECT {_SELECT_META} FROM memory_units WHERE group_id = ? AND unit_id = ?",
        (group_id, unit_id),
    ).fetchone()
    return None if row is None else _unit_from_row(row)


def _unit_from_row(row: sqlite3.Row) -> MemoryUnit:
    metadata = _json_value(row["metadata"], {})
    return MemoryUnit(
        unit_id=str(row["unit_id"]),
        group_id=str(row["group_id"]),
        created_at=str(row["created_at"]),
        unit_type=str(row["unit_type"]),
        scope=str(row["scope"]),
        scope_id=str(row["scope_id"]),
        summary=str(row["summary"]),
        participants=_json_list(row["participants"]),
        topics=_json_list(row["topics"]),
        keywords=_json_list(row["keywords"]),
        retrieval_terms=_json_list(row["retrieval_terms"]),
        identity_aliases=_json_list(row["identity_aliases"]),
        event_time=str(row["event_time"]),
        valid_until=str(row["valid_until"]),
        status=str(row["status"]),
        salience=float(row["salience"]),
        confidence=float(row["confidence"]),
        lifespan=str(row["lifespan"]),
        should_prompt=bool(row["should_prompt"]),
        source_ids=_json_list(row["source_ids"]),
        embedding=None,
        metadata=metadata if isinstance(metadata, dict) else {},
    )


def _encode_unit(unit: MemoryUnit) -> dict[str, Any]:
    """单元 → 元数据列的值（顺序与 ``_META_COLUMNS`` 一致）。"""
    encoded: dict[str, Any] = {}
    for name in _TEXT_FIELDS:
        value = getattr(unit, name, "")
        encoded[name] = "" if value is None else str(value)
    for name in _LIST_FIELDS:
        value = getattr(unit, name, None)
        items = list(value) if isinstance(value, (list, tuple)) else []
        encoded[name] = json.dumps(items, ensure_ascii=False, default=str)
    encoded["salience"] = _as_float(unit.salience, 0.5)
    encoded["confidence"] = _as_float(unit.confidence, 0.7)
    encoded["should_prompt"] = 1 if unit.should_prompt else 0
    metadata = unit.metadata if isinstance(unit.metadata, dict) else {}
    encoded["metadata"] = json.dumps(metadata, ensure_ascii=False, sort_keys=True, default=str)
    return encoded


def _content_hash(encoded: dict[str, Any]) -> str:
    payload = json.dumps(encoded, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def text_fingerprint(unit: MemoryUnit) -> str:
    """向量对应文本的指纹。

    它让「元数据变了但向量没重算」不可能悄悄发生：沿用旧向量前必须指纹相等，否则
    宁可不带向量（下次加载时按当前文本重算），也不能把过期向量当成当前向量。
    """
    return hashlib.sha256(embedding_text(unit).encode("utf-8")).hexdigest()[:16]


def pack_vector(vector: Iterable[float]) -> bytes:
    return np.asarray(list(vector), dtype=np.float32).tobytes()


def _unpack(blob: bytes) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32)


def _json_value(raw: Any, default: Any) -> Any:
    if not isinstance(raw, str) or not raw:
        return default
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return default


def _json_list(raw: Any) -> list[Any]:
    value = _json_value(raw, [])
    return value if isinstance(value, list) else []


def _as_float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _chunks(items: list[str], size: int) -> Iterator[list[str]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
