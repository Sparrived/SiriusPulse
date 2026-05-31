"""情景压缩 SQLite 存储层。

共享 persona.db 数据库连接。
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sirius_pulse.utils.sqlite_base import BaseSqliteStore
from sirius_pulse.memory.evolution.models import Triple
from sirius_pulse.memory.situation.models import Situation

logger = logging.getLogger(__name__)

__all__ = ["SituationStore"]


class SituationStore(BaseSqliteStore):
    """情景压缩存储。

    存储暂冷时生成的 Situation，供当日上下文注入和冷寂日记生成使用。
    """

    def _create_tables(self) -> None:
        self.executescript("""
            CREATE TABLE IF NOT EXISTS situations (
                situation_id TEXT PRIMARY KEY,
                group_id TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT '',
                triples TEXT DEFAULT '[]',
                participants TEXT DEFAULT '[]',
                topics TEXT DEFAULT '[]',
                summary TEXT DEFAULT '',
                source_entry_ids TEXT DEFAULT '[]',
                time_range_start TEXT DEFAULT '',
                time_range_end TEXT DEFAULT '',
                validated_triple_count INTEGER DEFAULT 0,
                rejected_triple_count INTEGER DEFAULT 0,
                processed INTEGER DEFAULT 0
            );

            CREATE INDEX IF NOT EXISTS idx_sit_group
                ON situations(group_id);
            CREATE INDEX IF NOT EXISTS idx_sit_created
                ON situations(created_at);
            CREATE INDEX IF NOT EXISTS idx_sit_processed
                ON situations(group_id, processed);
        """)

        # 兼容旧表：添加 processed 列（如果不存在）
        try:
            self.execute(
                "ALTER TABLE situations ADD COLUMN processed INTEGER DEFAULT 0"
            )
        except Exception:
            pass  # 列已存在

    # ── 写入 ──

    def save(self, situation: Situation) -> None:
        """保存一条情景记录。"""
        data = situation.to_dict()
        self.execute(
            """INSERT OR REPLACE INTO situations
               (situation_id, group_id, created_at, triples, participants,
                topics, summary, source_entry_ids, time_range_start,
                time_range_end, validated_triple_count, rejected_triple_count)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                data["situation_id"],
                data["group_id"],
                data["created_at"],
                json.dumps(data["triples"], ensure_ascii=False),
                json.dumps(data["participants"], ensure_ascii=False),
                json.dumps(data["topics"], ensure_ascii=False),
                data["summary"],
                json.dumps(data["source_entry_ids"], ensure_ascii=False),
                data["time_range_start"],
                data["time_range_end"],
                data["validated_triple_count"],
                data["rejected_triple_count"],
            ),
        )

    # ── 查询 ──

    def get(self, situation_id: str) -> Situation | None:
        """按 ID 获取单条情景。"""
        row = self.fetchone(
            "SELECT * FROM situations WHERE situation_id = ?",
            (situation_id,),
        )
        return self._row_to_situation(row) if row else None

    def get_today(self, group_id: str, unprocessed_only: bool = True) -> list[Situation]:
        """获取某群组今天的情景（按时间排序）。

        Args:
            group_id: 群组 ID
            unprocessed_only: 是否只返回未处理的（默认 True）
        """
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if unprocessed_only:
            rows = self.fetchall(
                """SELECT * FROM situations
                   WHERE group_id = ? AND created_at >= ? AND processed = 0
                   ORDER BY created_at""",
                (group_id, f"{today}T00:00:00"),
            )
        else:
            rows = self.fetchall(
                """SELECT * FROM situations
                   WHERE group_id = ? AND created_at >= ?
                   ORDER BY created_at""",
                (group_id, f"{today}T00:00:00"),
            )
        return [self._row_to_situation(r) for r in rows]

    def get_unprocessed(self, group_id: str) -> list[Situation]:
        """获取某群组所有未处理的情景（按时间排序）。

        用于"活跃 → 冷寂"循环中获取待处理的情景。
        """
        rows = self.fetchall(
            """SELECT * FROM situations
               WHERE group_id = ? AND processed = 0
               ORDER BY created_at""",
            (group_id,),
        )
        return [self._row_to_situation(r) for r in rows]

    def get_by_group(
        self,
        group_id: str,
        limit: int = 100,
    ) -> list[Situation]:
        """获取某群组的所有情景。"""
        rows = self.fetchall(
            """SELECT * FROM situations
               WHERE group_id = ?
               ORDER BY created_at DESC
               LIMIT ?""",
            (group_id, limit),
        )
        return [self._row_to_situation(r) for r in rows]

    def mark_processed(self, situation_ids: list[str]) -> None:
        """标记指定情景为已处理（用于日记生成后）。

        Args:
            situation_ids: 情景 ID 列表
        """
        if not situation_ids:
            return
        placeholders = ",".join(["?"] * len(situation_ids))
        self.execute(
            f"UPDATE situations SET processed = 1 WHERE situation_id IN ({placeholders})",
            situation_ids,
        )

    def delete_before(self, timestamp: str) -> int:
        """删除指定时间之前的情景（用于清理旧数据）。"""
        cursor = self.execute(
            "DELETE FROM situations WHERE created_at < ?",
            (timestamp,),
        )
        return cursor.rowcount

    def count_by_group(self, group_id: str) -> int:
        """统计某群组的情景数量。"""
        row = self.fetchone(
            "SELECT COUNT(*) as cnt FROM situations WHERE group_id = ?",
            (group_id,),
        )
        return row["cnt"] if row else 0

    def get_extracted_entry_ids(self, group_id: str) -> set[str]:
        """获取某群组已提取过的消息 ID 集合。

        用于避免重复提取同一批消息为情景。
        """
        rows = self.fetchall(
            "SELECT source_entry_ids FROM situations WHERE group_id = ?",
            (group_id,),
        )
        result: set[str] = set()
        for row in rows:
            try:
                ids = json.loads(row["source_entry_ids"] or "[]")
                result.update(ids)
            except (json.JSONDecodeError, TypeError):
                continue
        return result

    # ── 内部工具 ──

    def _row_to_situation(self, row: Any) -> Situation:
        """将 SQLite 行转换为 Situation。"""
        return Situation(
            situation_id=row["situation_id"],
            group_id=row["group_id"],
            created_at=row["created_at"],
            triples=[
                Triple.from_dict(t)
                for t in json.loads(row["triples"] or "[]")
            ],
            participants=json.loads(row["participants"] or "[]"),
            topics=json.loads(row["topics"] or "[]"),
            summary=row["summary"],
            source_entry_ids=json.loads(row["source_entry_ids"] or "[]"),
            time_range_start=row["time_range_start"],
            time_range_end=row["time_range_end"],
            validated_triple_count=int(row["validated_triple_count"]),
            rejected_triple_count=int(row["rejected_triple_count"]),
        )
