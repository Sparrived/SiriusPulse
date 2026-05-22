"""迁移脚本：将名词解释从旧版按群号文件迁移到新版人格级单文件。

用法:
    python scripts/migrate_glossary_to_persona.py <data_dir>

参数:
    data_dir: data/ 目录路径，例如 data/

说明:
    - 扫描 data/personas/* 下每个人格的 work_path
    - 将 <work_path>/glossary/*.json（旧版按群号分文件）
      合并为 <work_path>/glossary/terms.json（新版单文件）
    - 合并时同名术语 usage_count 累加，取更高 confidence 的定义
    - 原文件重命名为 *.json.migrated
    - 如果 terms.json 已存在，跳过该人格
"""

from __future__ import annotations

import json
import logging
import sys
from pathlib import Path

from sirius_pulse.memory.glossary.models import GlossaryTerm

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

MAX_CONTEXT_EXAMPLES = 5


def migrate_persona_glossary(persona_work_path: Path, persona_name: str) -> int:
    """Migrate legacy per-group glossary files to a single terms.json for a persona.

    Returns the number of terms migrated.
    """
    glossary_dir = persona_work_path / "glossary"
    if not glossary_dir.exists():
        return 0

    terms_path = glossary_dir / "terms.json"
    if terms_path.exists():
        logger.info("Skipping %s: terms.json already exists", persona_name)
        return 0

    all_terms: dict[str, GlossaryTerm] = {}
    migrated_count = 0

    for legacy_path in glossary_dir.glob("*.json"):
        if legacy_path.name == "terms.json":
            continue
        if legacy_path.name.endswith(".migrated"):
            continue
        try:
            data = json.loads(legacy_path.read_text(encoding="utf-8"))
            terms = {
                k: GlossaryTerm.from_dict(v)
                for k, v in data.items()
                if isinstance(v, dict)
            }
            for key, term in terms.items():
                existing = all_terms.get(key)
                if existing is not None:
                    existing.usage_count += term.usage_count
                    if term.confidence > existing.confidence:
                        existing.definition = term.definition
                        existing.confidence = term.confidence
                        existing.source = term.source
                    seen = set(existing.context_examples)
                    for ex in term.context_examples:
                        if ex not in seen and len(existing.context_examples) < MAX_CONTEXT_EXAMPLES:
                            existing.context_examples.append(ex)
                            seen.add(ex)
                    related_set = set(existing.related_terms)
                    for rt in term.related_terms:
                        if rt not in related_set:
                            existing.related_terms.append(rt)
                            related_set.add(rt)
                else:
                    all_terms[key] = term

            if terms:
                migrated_count += len(terms)
                backup = legacy_path.with_suffix(".json.migrated")
                legacy_path.rename(backup)
                logger.info(
                    "Migrated %d terms from %s for persona '%s'",
                    len(terms),
                    legacy_path.name,
                    persona_name,
                )
        except Exception as exc:
            logger.warning("Migration failed for %s: %s", legacy_path, exc)

    if all_terms:
        tmp = terms_path.with_suffix(terms_path.suffix + ".tmp")
        tmp.write_text(
            json.dumps({k: v.to_dict() for k, v in all_terms.items()}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(terms_path)
        logger.info(
            "Created terms.json with %d terms for persona '%s'",
            len(all_terms),
            persona_name,
        )

    return migrated_count


def main() -> int:
    if len(sys.argv) < 2:
        print(f"用法: python {sys.argv[0]} <data_dir>")
        print("示例: python scripts/migrate_glossary_to_persona.py data/")
        return 1

    data_dir = Path(sys.argv[1]).resolve()
    personas_dir = data_dir / "personas"

    if not personas_dir.exists():
        logger.error("Personas directory not found: %s", personas_dir)
        return 1

    total_migrated = 0
    for persona_path in sorted(personas_dir.iterdir()):
        if not persona_path.is_dir():
            continue
        persona_name = persona_path.name
        work_path = persona_path

        count = migrate_persona_glossary(work_path, persona_name)
        total_migrated += count

    logger.info("Migration complete. Total terms migrated: %d", total_migrated)
    return 0


if __name__ == "__main__":
    sys.exit(main())
