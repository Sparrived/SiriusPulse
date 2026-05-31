"""数据迁移脚本：将旧记忆系统数据迁移到演化链。

使用方法：
    python scripts/migrate_to_evolution.py --work-path /path/to/data/persona_name

迁移内容：
1. UnifiedUser.distilled_points → LLM 提取三元组 → EvolutionRecord
2. UnifiedUser.identity_anchors → EvolutionRecord
3. UnifiedUser.relationships → EvolutionRecord

迁移策略：
- 使用 LLM 从 distilled_points 中提取结构化三元组
- LLM 模型和 API Key 从 provider 配置自动读取
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

# 确保 sirius_pulse 在 sys.path 中
import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sirius_pulse.memory.evolution.chain import EvolutionChain
from sirius_pulse.memory.evolution.models import (
    EvolutionRecord,
    MetaTag,
    RecordStatus,
)

logger = logging.getLogger(__name__)

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


def _build_brain_from_provider(work_path: Path):
    """从 provider 配置构建 Brain 实例。

    优先级：
    1. data/providers/provider_keys.json（全局配置）
    2. work_path/provider_keys.json（人格配置）
    3. 环境变量
    """
    from sirius_pulse.providers.routing import AutoRoutingProvider, ProviderRegistry
    from sirius_pulse.core.brain import Brain
    from sirius_pulse.core.model_router import ModelRouter, TaskConfig
    from sirius_pulse.models.persona import PersonaProfile

    provider_keys_rel = Path("providers") / "provider_keys.json"
    data_dir = None
    for ancestor in [work_path, *work_path.parents]:
        if (ancestor / provider_keys_rel).exists():
            data_dir = ancestor
            break
    if data_dir is not None:
        registry = ProviderRegistry(data_dir)
    elif (work_path / "provider_keys.json").exists():
        registry = ProviderRegistry(work_path)
    else:
        import os
        api_key = os.getenv("SIRIUS_API_KEY", "")
        base_url = os.getenv("SIRIUS_BASE_URL", "")
        model = os.getenv("SIRIUS_MODEL", "gpt-4o-mini")
        if not api_key:
            return None, ""
        from sirius_pulse.providers.routing import ProviderConfig
        cfg = ProviderConfig(
            provider_type="openai-compatible",
            api_key=api_key,
            base_url=base_url,
            healthcheck_model=model,
            enabled=True,
            models=[model],
        )
        provider = AutoRoutingProvider({"openai-compatible": cfg})
        model_router = ModelRouter({"memory_extract": TaskConfig(model_name=model, temperature=0.3, max_tokens=512)})
        persona = PersonaProfile(name="迁移助手")
        brain = Brain(
            provider_async=provider,
            model_router=model_router,
            persona=persona,
        )
        return brain, model

    loaded = registry.load()
    if not loaded:
        return None, ""

    provider = AutoRoutingProvider(loaded)

    available_models: list[tuple[str, str]] = []
    for provider_name, cfg in loaded.items():
        for m in cfg.models:
            available_models.append((provider_name, m))
        if cfg.healthcheck_model and cfg.healthcheck_model not in [m for _, m in available_models]:
            available_models.append((provider_name, cfg.healthcheck_model))

    if not available_models:
        return None, ""

    print("\n可用模型列表：")
    for i, (pname, mname) in enumerate(available_models, 1):
        print(f"  [{i}] {mname}  (provider: {pname})")

    if len(available_models) == 1:
        model_name = available_models[0][1]
        print(f"\n仅一个可用模型，自动选择: {model_name}")
    else:
        while True:
            raw = input(f"\n请选择模型编号 [1-{len(available_models)}]（回车默认 1）: ").strip()
            if not raw:
                choice = 0
                break
            if raw.isdigit() and 1 <= int(raw) <= len(available_models):
                choice = int(raw) - 1
                break
            print("输入无效，请重新选择")
        model_name = available_models[choice][1]

    print(f"已选择模型: {model_name}")

    model_router = ModelRouter({"memory_extract": TaskConfig(model_name=model_name, temperature=0.3, max_tokens=512)})
    persona = PersonaProfile(name="迁移助手")
    brain = Brain(
        provider_async=provider,
        model_router=model_router,
        persona=persona,
    )
    return brain, model_name


async def migrate_distilled_points(
    conn: sqlite3.Connection,
    chain: EvolutionChain,
    brain: any,
    model_name: str,
) -> int:
    """迁移 UnifiedUser.distilled_points 到演化链（必须使用 LLM）。"""
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

        points_text = "\n".join(f"- {p}" for p in points if p and p.strip())
        if not points_text:
            continue

        triples = await _extract_triples_from_text(points_text, brain, model_name)

        if not triples:
            logger.warning("用户 %s (%s) 的 distilled_points LLM 提取无结果，跳过", name, user_id)
            continue

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
        logger.error("迁移 LLM 提取失败: %s", exc)
        return None

    parsed = _parse_response(raw)
    if not parsed:
        return None

    triples = parsed.get("triples", [])
    valid = [
        t for t in triples
        if t.get("subject") and t.get("predicate") and t.get("obj")
    ]
    return valid if valid else None


def migrate_identity_anchors(conn: sqlite3.Connection, chain: EvolutionChain) -> int:
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


def migrate_relationships(conn: sqlite3.Connection, chain: EvolutionChain) -> int:
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


async def run_migration(work_path: Path, brain: any, model_name: str) -> None:
    """执行完整迁移。"""
    logger.info("开始迁移: %s", work_path)
    logger.info("使用模型: %s", model_name)

    db_path = work_path / "persona.db"
    if not db_path.exists():
        db_path = work_path / "memory.db"
        if not db_path.exists():
            logger.error("数据库不存在: %s", db_path)
            return

    conn = sqlite3.connect(str(db_path))
    chain = EvolutionChain(conn=conn)

    try:
        total = 0
        total += await migrate_distilled_points(conn, chain, brain, model_name)
        total += migrate_identity_anchors(conn, chain)
        total += migrate_relationships(conn, chain)
        logger.info("迁移完成: 共 %d 条记录", total)
    finally:
        conn.close()
        chain.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="迁移旧记忆数据到演化链")
    parser.add_argument("--work-path", type=Path, required=True, help="数据目录路径")
    args = parser.parse_args()

    brain, model_name = _build_brain_from_provider(args.work_path)
    if not brain:
        logger.error("未找到 provider 配置，请确保 data/providers/provider_keys.json 存在且包含有效的 api_key")
        return

    logger.info("已从 provider 配置加载模型: %s", model_name)
    asyncio.run(run_migration(args.work_path, brain=brain, model_name=model_name))


if __name__ == "__main__":
    main()
