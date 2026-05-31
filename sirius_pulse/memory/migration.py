"""数据迁移脚本：将旧记忆系统数据迁移到演化链。

使用方法：
    python -m sirius_pulse.memory.migration --work-path /path/to/data

迁移内容：
1. UnifiedUser.distilled_points → LLM 提取三元组 → EvolutionRecord
2. UnifiedUser.identity_anchors → EvolutionRecord
3. UnifiedUser.relationships → EvolutionRecord

迁移策略：
- 使用 LLM 从 distilled_points 中提取结构化三元组
- 迁移数据标记为 MetaTag.MIGRATION
- 置信度设为 0.5（中等偏低），便于演化链后续自动核实或替换
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sqlite3
from pathlib import Path

from sirius_pulse.memory.evolution.chain import EvolutionChain
from sirius_pulse.memory.evolution.models import (
    EvolutionRecord,
    MetaTag,
    RecordStatus,
)

logger = logging.getLogger(__name__)

# 迁移数据的置信度：中等偏低，便于演化链后续核实
MIGRATION_CONFIDENCE = 0.5


_MIGRATION_TRIPLE_PROMPT = """从以下用户描述中提取结构化事实。

用户描述：
{points_text}

输出 JSON：
{{
  "triples": [
    {{"subject": "人名", "predicate": "关系/动作", "obj": "宾语/值"}}
  ]
}}

规则：
- 主语必须是具体人名，不要用代词
- 只提取明确的事实，不要推测
- 如果描述中没有可提取的事实，返回空数组
"""


async def migrate_distilled_points(
    conn: sqlite3.Connection,
    chain: EvolutionChain,
    brain: any,
    model_name: str,
) -> int:
    """迁移 UnifiedUser.distilled_points 到演化链。

    使用 LLM 从蒸馏要点中提取结构化三元组。
    """
    cursor = conn.execute(
        "SELECT user_id, name, distilled_points FROM users WHERE distilled_points != '[]'"
    )
    rows = cursor.fetchall()

    migrated = 0
    for row in rows:
        user_id = row[0]
        name = row[1]
        points = json.loads(row[2])

        if not points:
            continue

        # 合并要点为一段文本
        points_text = "\n".join(f"- {p}" for p in points if p and p.strip())
        if not points_text:
            continue

        # 调用 LLM 提取三元组
        triples = await _extract_triples_from_text(
            points_text, brain, model_name
        )

        if triples:
            for t in triples:
                record = EvolutionRecord(
                    subject=t.get("subject", "") or name or user_id,
                    subject_user_id=user_id,
                    predicate=t.get("predicate", "是"),
                    obj=t.get("obj", ""),
                    status=RecordStatus.ACTIVE,
                    confidence=MIGRATION_CONFIDENCE,
                    initial_confidence=MIGRATION_CONFIDENCE,
                    source_type=MetaTag.MIGRATION,
                    source_group_id="",
                    source_message_ids=[],
                    extracted_by_model=f"migration:{model_name}",
                )
                chain._persist_record(record)
                migrated += 1
        else:
            # LLM 提取失败，降级为原始文本存储
            for point_text in points:
                if not point_text or not point_text.strip():
                    continue
                record = EvolutionRecord(
                    subject=name or user_id,
                    subject_user_id=user_id,
                    predicate="蒸馏要点",
                    obj=point_text.strip()[:100],
                    status=RecordStatus.ACTIVE,
                    confidence=MIGRATION_CONFIDENCE,
                    initial_confidence=MIGRATION_CONFIDENCE,
                    source_type=MetaTag.MIGRATION,
                    source_group_id="",
                    source_message_ids=[],
                    extracted_by_model="migration:fallback",
                )
                chain._persist_record(record)
                migrated += 1

    logger.info("迁移 distilled_points: %d 条", migrated)
    return migrated


async def _extract_triples_from_text(
    text: str,
    brain: any,
    model_name: str,
) -> list[dict] | None:
    """调用 LLM 从文本中提取三元组。"""
    from sirius_pulse.core.brain import RawRequest

    prompt = _MIGRATION_TRIPLE_PROMPT.format(points_text=text)
    raw_request = RawRequest(
        model=model_name,
        system_prompt="你是事实提取助手。只输出 JSON。",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1,
        max_tokens=512,
        purpose="migration_extract",
        response_format={"type": "json_object"},
    )

    try:
        raw = await brain.raw_call(raw_request)
    except Exception as exc:
        logger.warning("迁移 LLM 提取失败: %s", exc)
        return None

    # 解析 JSON
    parsed = _parse_response(raw)
    if not parsed:
        return None

    triples = parsed.get("triples", [])
    # 过滤无效三元组
    valid = [
        t for t in triples
        if t.get("subject") and t.get("predicate") and t.get("obj")
    ]
    return valid if valid else None


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
                confidence=MIGRATION_CONFIDENCE,
                initial_confidence=MIGRATION_CONFIDENCE,
                source_type=MetaTag.MIGRATION,
                source_group_id="",
                source_message_ids=[],
                extracted_by_model="migration:direct",
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
                confidence=MIGRATION_CONFIDENCE,
                initial_confidence=MIGRATION_CONFIDENCE,
                source_type=MetaTag.MIGRATION,
                source_group_id="",
                source_message_ids=[],
                extracted_by_model="migration:direct",
            )
            chain._persist_record(record)
            migrated += 1

    logger.info("迁移 relationships: %d 条", migrated)
    return migrated


def _parse_response(raw: str) -> dict | None:
    """解析 LLM 返回的 JSON。"""
    text = raw.strip()
    if "```" in text:
        parts = text.split("```")
        if len(parts) >= 3:
            text = parts[1]
            if text.startswith("json"):
                text = text[4:]
            text = text.strip()
    try:
        result = json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return None
    return result if isinstance(result, dict) else None


async def run_migration(work_path: Path, brain: any = None, model_name: str = "") -> None:
    """执行完整迁移。

    Args:
        work_path: 数据目录路径
        brain: Brain 实例（用于 LLM 提取），为 None 时跳过 LLM 提取
        model_name: 模型名称
    """
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
        total = 0

        # 迁移 distilled_points（需要 LLM）
        if brain and model_name:
            total += await migrate_distilled_points(conn, chain, brain, model_name)
        else:
            logger.info("未提供 Brain 实例，跳过 distilled_points 的 LLM 提取")

        # 迁移 identity_anchors 和 relationships（不需要 LLM）
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
    parser.add_argument("--no-llm", action="store_true", help="跳过 LLM 提取（降级模式）")
    args = parser.parse_args()

    brain = None
    model_name = ""

    if not args.no_llm:
        logger.info("提示：使用 --no-llm 可跳过 LLM 提取（降级模式）")
        logger.info("当前使用降级模式，distilled_points 将以原始文本迁移")
        # 降级模式：不调用 LLM
        asyncio.run(run_migration(args.work_path))
    else:
        asyncio.run(run_migration(args.work_path))


if __name__ == "__main__":
    main()
