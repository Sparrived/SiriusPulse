"""演化链 SQLite 存储层。

独立数据库 evolution.db，与主记忆库分离。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from sirius_pulse.utils.sqlite_base import BaseSqliteStore
from sirius_pulse.memory.evolution.models import (
    EvolutionRecord,
    RecordStatus,
    Triple,
)

logger = logging.getLogger(__name__)

__all__ = ["EvolutionStore"]


class EvolutionStore(BaseSqliteStore):
    """演化链 SQLite 存储。

    独立于主 memory.db，确保演化链数据的隔离性和可追溯性。
    """

    def _create_tables(self) -> None:
        self.executescript("""
            CREATE TABLE IF NOT EXISTS evolution_records (
                record_id TEXT PRIMARY KEY,
                subject TEXT NOT NULL,
                subject_user_id TEXT DEFAULT '',
                predicate TEXT NOT NULL,
                obj TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                confidence REAL NOT NULL DEFAULT 0.5,
                initial_confidence REAL NOT NULL DEFAULT 0.5,
                supersedes TEXT DEFAULT '[]',
                superseded_by TEXT,
                source_type TEXT NOT NULL DEFAULT 'stated',
                source_situation_id TEXT DEFAULT '',
                source_group_id TEXT DEFAULT '',
                source_message_ids TEXT DEFAULT '[]',
                extracted_at TEXT NOT NULL DEFAULT '',
                extracted_by_model TEXT DEFAULT '',
                verifications TEXT DEFAULT '[]',
                corrections TEXT DEFAULT '[]',
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE INDEX IF NOT EXISTS idx_evo_subject
                ON evolution_records(subject);
            CREATE INDEX IF NOT EXISTS idx_evo_user_id
                ON evolution_records(subject_user_id);
            CREATE INDEX IF NOT EXISTS idx_evo_status
                ON evolution_records(status);
            CREATE INDEX IF NOT EXISTS idx_evo_source_situation
                ON evolution_records(source_situation_id);
            CREATE INDEX IF NOT EXISTS idx_evo_group
                ON evolution_records(source_group_id);
        """)

    # ── 写入 ──

    def save_record(self, record: EvolutionRecord) -> None:
        """保存或更新一条演化链记录。"""
        data = record.to_dict()
        self.execute(
            """INSERT OR REPLACE INTO evolution_records
               (record_id, subject, subject_user_id, predicate, obj, status, confidence,
                initial_confidence, supersedes, superseded_by,
                source_type, source_situation_id, source_group_id,
                source_message_ids, extracted_at, extracted_by_model,
                verifications, corrections)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                data["record_id"],
                data["subject"],
                data["subject_user_id"],
                data["predicate"],
                data["obj"],
                data["status"],
                data["confidence"],
                data["initial_confidence"],
                json.dumps(data["supersedes"], ensure_ascii=False),
                data["superseded_by"],
                data["source_type"],
                data["source_situation_id"],
                data["source_group_id"],
                json.dumps(data["source_message_ids"], ensure_ascii=False),
                data["extracted_at"],
                data["extracted_by_model"],
                json.dumps(data["verifications"], ensure_ascii=False),
                json.dumps(data["corrections"], ensure_ascii=False),
            ),
        )

    def save_records(self, records: list[EvolutionRecord]) -> None:
        """批量保存演化链记录。"""
        for record in records:
            self.save_record(record)

    # ── 查询 ──

    def get_record(self, record_id: str) -> EvolutionRecord | None:
        """按 ID 获取单条记录。"""
        row = self.fetchone(
            "SELECT * FROM evolution_records WHERE record_id = ?",
            (record_id,),
        )
        return self._row_to_record(row) if row else None

    def get_active_by_subject(self, subject: str) -> list[EvolutionRecord]:
        """获取某主体的所有 active 记录。"""
        rows = self.fetchall(
            "SELECT * FROM evolution_records WHERE subject = ? AND status = ?",
            (subject, RecordStatus.ACTIVE),
        )
        return [self._row_to_record(r) for r in rows]

    def get_all_by_subject(self, subject: str) -> list[EvolutionRecord]:
        """获取某主体的所有记录（含 shadow）。"""
        rows = self.fetchall(
            "SELECT * FROM evolution_records WHERE subject = ? ORDER BY extracted_at",
            (subject,),
        )
        return [self._row_to_record(r) for r in rows]

    def get_by_group(self, group_id: str, status: str | None = None) -> list[EvolutionRecord]:
        """获取某群组的所有记录。"""
        if status:
            rows = self.fetchall(
                "SELECT * FROM evolution_records WHERE source_group_id = ? AND status = ?",
                (group_id, status),
            )
        else:
            rows = self.fetchall(
                "SELECT * FROM evolution_records WHERE source_group_id = ?",
                (group_id,),
            )
        return [self._row_to_record(r) for r in rows]

    def get_by_situation(self, situation_id: str) -> list[EvolutionRecord]:
        """获取某情景来源的所有记录。"""
        rows = self.fetchall(
            "SELECT * FROM evolution_records WHERE source_situation_id = ?",
            (situation_id,),
        )
        return [self._row_to_record(r) for r in rows]

    def get_uncertain_records(self, limit: int = 50) -> list[EvolutionRecord]:
        """获取所有待验证的记录。"""
        rows = self.fetchall(
            "SELECT * FROM evolution_records WHERE status = ? LIMIT ?",
            (RecordStatus.UNCERTAIN, limit),
        )
        return [self._row_to_record(r) for r in rows]

    def find_by_content(
        self,
        subject: str,
        predicate: str = "",
        obj: str = "",
        status: str | None = None,
    ) -> list[EvolutionRecord]:
        """按内容查找记录（用于矛盾检测）。"""
        conditions = ["subject = ?"]
        params: list[Any] = [subject]

        if predicate:
            conditions.append("predicate = ?")
            params.append(predicate)
        if obj:
            conditions.append("obj = ?")
            params.append(obj)
        if status:
            conditions.append("status = ?")
            params.append(status)

        where = " AND ".join(conditions)
        rows = self.fetchall(
            f"SELECT * FROM evolution_records WHERE {where}",
            tuple(params),
        )
        return [self._row_to_record(r) for r in rows]

    def get_all_subjects(self) -> list[str]:
        """获取所有有记录的主体名称。"""
        rows = self.fetchall(
            "SELECT DISTINCT subject FROM evolution_records"
        )
        return [r["subject"] for r in rows]

    def get_active_by_user_id(self, user_id: str) -> list[EvolutionRecord]:
        """按 user_id 获取所有 active 记录（别名系统关联）。"""
        rows = self.fetchall(
            "SELECT * FROM evolution_records WHERE subject_user_id = ? AND status = ?",
            (user_id, RecordStatus.ACTIVE),
        )
        return [self._row_to_record(r) for r in rows]

    def get_all_by_user_id(self, user_id: str) -> list[EvolutionRecord]:
        """按 user_id 获取所有记录。"""
        rows = self.fetchall(
            "SELECT * FROM evolution_records WHERE subject_user_id = ?",
            (user_id,),
        )
        return [self._row_to_record(r) for r in rows]

    # ── 内部工具 ──

    def _row_to_record(self, row: Any) -> EvolutionRecord:
        """将 SQLite 行转换为 EvolutionRecord。"""
        return EvolutionRecord(
            record_id=row["record_id"],
            subject=row["subject"],
            subject_user_id=row["subject_user_id"] if "subject_user_id" in row.keys() else "",
            predicate=row["predicate"],
            obj=row["obj"],
            status=row["status"],
            confidence=float(row["confidence"]),
            initial_confidence=float(row["initial_confidence"]),
            supersedes=json.loads(row["supersedes"] or "[]"),
            superseded_by=row["superseded_by"],
            source_type=row["source_type"],
            source_situation_id=row["source_situation_id"],
            source_group_id=row["source_group_id"],
            source_message_ids=json.loads(row["source_message_ids"] or "[]"),
            extracted_at=row["extracted_at"],
            extracted_by_model=row["extracted_by_model"],
            verifications=json.loads(row["verifications"] or "[]"),
            corrections=json.loads(row["corrections"] or "[]"),
        )
