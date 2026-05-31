"""数据迁移脚本：将旧记忆系统数据迁移到演化链。

使用方法：
    python -m sirius_pulse.memory.migration --work-path /path/to/data

迁移内容：
1. UnifiedUser.distilled_points → EvolutionRecord
2. UnifiedUser.identity_anchors → EvolutionRecord
3. UnifiedUser.relationships → EvolutionRecord
4. 旧日记条目 → Situation（简化版）
"""

from __future__ import annotations

import argparse
import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from sirius_pulse.memory.evolution.chain import EvolutionChain
from sirius_pulse.memory.evolution.models import (
    EvolutionRecord,
    MetaTag,
    RecordStatus,
    SituationSource,
    Triple,
)

logger = logging.getLogger(__name__)


def migrate_distilled_points(
    conn: sqlite3.Connection,
    chain: EvolutionChain,
) -> int:
    """迁移 UnifiedUser.distilled_points 到演化链。"""
    cursor = conn.execute(
        "SELECT user_id, name, distilled_points FROM users WHERE distilled_points != '[]'"
    )
    rows = cursor.fetchall()

    migrated = 0
    for row in rows:
        user_id = row[0]
        name = row[1]
        points = json.loads(row[2])

        for point_text in points:
            if not point_text or not point_text.strip():
                continue

            # 从文本中提取简单的三元组
            # 这是一个简化版本，实际应该用 LLM 提取
            record = EvolutionRecord(
                subject=name or user_id,
                subject_user_id=user_id,
                predicate="蒸馏要点",
                obj=point_text.strip()[:100],
                status=RecordStatus.ACTIVE,
                confidence=0.7,
                initial_confidence=0.7,
                source_type=MetaTag.MIGRATION,
                source_group_id="",
                source_message_ids=[],
                extracted_by_model="migration",
            )
            chain._persist_record(record)
            migrated += 1

    logger.info("迁移 distilled_points: %d 条", migrated)
    return migrated


def migrate_identity_anchors(
    conn: sqlite3.Connection,
    chain: EvolutionChain,
) -> int:
    """迁移 UnifiedUser.identity_anchors 到演化链。"""
    cursor = conn.execute(
        "SELECT user_id, name, identity_anchors FROM users WHERE identity_anchors != '[]'"
    )
    rows = cursor.fetchall()

    migrated = 0
    for row in rows:
        user_id = row[0]
        name = row[1]
        anchors = json.loads(row[2])

        for anchor in anchors:
            if not anchor or not anchor.strip():
                continue

            record = EvolutionRecord(
                subject=name or user_id,
                subject_user_id=user_id,
                predicate="是",
                obj=anchor.strip(),
                status=RecordStatus.ACTIVE,
                confidence=0.8,
                initial_confidence=0.8,
                source_type=MetaTag.MIGRATION,
                source_group_id="",
                source_message_ids=[],
                extracted_by_model="migration",
            )
            chain._persist_record(record)
            migrated += 1

    logger.info("迁移 identity_anchors: %d 条", migrated)
    return migrated


def migrate_relationships(
    conn: sqlite3.Connection,
    chain: EvolutionChain,
) -> int:
    """迁移 UnifiedUser.relationships 到演化链。"""
    cursor = conn.execute(
        "SELECT user_id, name, relationships FROM users WHERE relationships != '[]'"
    )
    rows = cursor.fetchall()

    migrated = 0
    for row in rows:
        user_id = row[0]
        name = row[1]
        relationships = json.loads(row[2])

        for rel in relationships:
            if not isinstance(rel, dict):
                continue

            target = rel.get("target_name", "")
            relation = rel.get("relation", "")
            if not target or not relation:
                continue

            record = EvolutionRecord(
                subject=name or user_id,
                subject_user_id=user_id,
                predicate=relation,
                obj=target,
                status=RecordStatus.ACTIVE,
                confidence=0.7,
                initial_confidence=0.7,
                source_type=MetaTag.MIGRATION,
                source_group_id="",
                source_message_ids=[],
                extracted_by_model="migration",
            )
            chain._persist_record(record)
            migrated += 1

    logger.info("迁移 relationships: %d 条", migrated)
    return migrated


def run_migration(work_path: Path) -> None:
    """执行完整迁移。"""
    logger.info("开始迁移: %s", work_path)

    # 连接旧数据库
    old_db_path = work_path / "memory.db"
    if not old_db_path.exists():
        logger.warning("旧数据库不存在: %s", old_db_path)
        return

    conn = sqlite3.connect(str(old_db_path))

    # 初始化演化链
    chain = EvolutionChain(db_path=work_path / "evolution.db")

    try:
        # 执行迁移
        total = 0
        total += migrate_distilled_points(conn, chain)
        total += migrate_identity_anchors(conn, chain)
        total += migrate_relationships(conn, chain)

        logger.info("迁移完成: 共 %d 条记录", total)
    finally:
        conn.close()
        chain.close()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    parser = argparse.ArgumentParser(description="迁移旧记忆数据到演化链")
    parser.add_argument("--work-path", type=Path, required=True, help="数据目录路径")
    args = parser.parse_args()
    run_migration(args.work_path)


if __name__ == "__main__":
    main()
