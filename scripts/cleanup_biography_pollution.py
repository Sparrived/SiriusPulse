"""清理传记系统中被身份混淆污染的存量数据。

修复内容：
1. 别名索引中人格自身名称被错误注册到其他用户的问题
2. 传记卡（UserPersonaCard）中错误包含"人格名"等混淆描述的问题

使用方法：
  python scripts/cleanup_biography_pollution.py <persona_dir>

示例：
  python scripts/cleanup_biography_pollution.py data/personas/sirius
"""

from __future__ import annotations

import json
import logging
import re
import sys
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger("cleanup_biography")


def load_persona_config(persona_dir: Path) -> tuple[str, list[str]]:
    """从 persona.json 加载人格名称和别名列表。"""
    persona_path = persona_dir / "persona.json"
    if not persona_path.exists():
        logger.error("未找到 persona.json: %s", persona_path)
        sys.exit(1)
    with open(persona_path, encoding="utf-8") as f:
        cfg = json.load(f)
    name = cfg.get("name", "")
    aliases = cfg.get("aliases", [])
    logger.info("人格名称: %s, 别名: %s", name, aliases)
    return name, aliases


def clean_alias_index(bio_dir: Path, persona_name: str, persona_aliases: list[str]) -> int:
    """清理别名索引中的人名身份污染条目。"""
    index_path = bio_dir / "index.json"
    if not index_path.exists():
        logger.info("别名索引不存在，跳过")
        return 0

    with open(index_path, encoding="utf-8") as f:
        index = json.load(f)

    persona_keys = {persona_name.lower()} | {a.lower() for a in persona_aliases}
    keys_to_remove = [k for k in index if k.lower() in persona_keys]
    for k in keys_to_remove:
        del index[k]

    if keys_to_remove:
        import tempfile

        tmp = index_path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False, indent=2)
        tmp.replace(index_path)
        logger.info("别名索引已清理 %d 条污染: %s", len(keys_to_remove), keys_to_remove)
    else:
        logger.info("别名索引无污染")

    return len(keys_to_remove)


def clean_biography_cards(bio_dir: Path, persona_name: str, persona_aliases: list[str]) -> int:
    """清理传记卡中的身份混淆内容。"""
    persona_lower = persona_name.lower()
    alias_lower_set = {a.lower() for a in persona_aliases}

    # 组合需要从传记中移除的身份混淆特征词
    identity_patterns = [
        re.compile(rf'人格名[称]*[是为叫]*"?{re.escape(persona_name)}"?', re.IGNORECASE),
        re.compile(rf"人格[名称]*[:：]\s*{re.escape(persona_name)}", re.IGNORECASE),
        re.compile(rf"是{re.escape(persona_name)}的人格档案", re.IGNORECASE),
    ]
    for alias in persona_aliases:
        if alias:
            identity_patterns.append(
                re.compile(rf'人格名[称]*[是为叫]*"?{re.escape(alias)}"?', re.IGNORECASE)
            )

    cleaned_count = 0
    for card_path in bio_dir.glob("qq_*.json"):
        with open(card_path, encoding="utf-8") as f:
            content = f.read()

        original = content
        card_data = json.loads(content)

        # 清理 short_bio 中的身份混淆内容
        if "short_bio" in card_data and card_data["short_bio"]:
            new_bio = card_data["short_bio"]
            for pattern in identity_patterns:
                new_bio = pattern.sub("", new_bio)
            # 清理残留的"人格名月白"或类似表述（通用匹配）
            new_bio = re.sub(rf'人格名[称]*[""]?{re.escape(persona_name)}[""]?', "", new_bio)
            for alias in persona_aliases:
                if alias:
                    new_bio = re.sub(rf'人格名[称]*[""]?{re.escape(alias)}[""]?', "", new_bio)
            # 清理多余的空白
            new_bio = re.sub(r"\s+", " ", new_bio).strip()
            if new_bio != card_data["short_bio"]:
                card_data["short_bio"] = new_bio
                logger.info("已清理传记卡 %s 的身份混淆描述", card_path.name)

        # 清理 identity_anchors 中包含人格名称的条目
        if "identity_anchors" in card_data and card_data["identity_anchors"]:
            filtered = [
                a for a in card_data["identity_anchors"]
                if persona_lower not in a.lower()
                and not any(alias_lower in a.lower() for alias_lower in alias_lower_set if alias_lower)
            ]
            if len(filtered) != len(card_data["identity_anchors"]):
                removed = len(card_data["identity_anchors"]) - len(filtered)
                card_data["identity_anchors"] = filtered
                logger.info("已清理传记卡 %s 的 %d 个锚点", card_path.name, removed)

        # 清理 relationships 中与人格名混淆的关系目标
        if "relationships" in card_data and card_data["relationships"]:
            filtered_rels = [
                r for r in card_data["relationships"]
                if persona_lower not in r.get("target_name", "").lower()
            ]
            if len(filtered_rels) != len(card_data["relationships"]):
                card_data["relationships"] = filtered_rels
                logger.info("已清理传记卡 %s 的人格混淆关系", card_path.name)

        # 清理 distilled_points 中的引用混淆
        if "distilled_points" in card_data and card_data["distilled_points"]:
            filtered_pts = [
                p for p in card_data["distilled_points"]
                if not any(
                    re.search(rf"(?:bot|yuki)[^。]*?称[^。]*?{re.escape(alias)}", p, re.IGNORECASE)
                    for alias in [persona_name] + persona_aliases if alias
                )
            ]
            if len(filtered_pts) != len(card_data["distilled_points"]):
                removed = len(card_data["distilled_points"]) - len(filtered_pts)
                card_data["distilled_points"] = filtered_pts
                logger.info("已清理传记卡 %s 的 %d 个混淆蒸馏点", card_path.name, removed)

        new_content = json.dumps(card_data, ensure_ascii=False, indent=2)
        if new_content != original:
            import tempfile

            tmp = card_path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(new_content)
            tmp.replace(card_path)
            cleaned_count += 1

    return cleaned_count


def main():
    if len(sys.argv) < 2:
        print("用法: python scripts/cleanup_biography_pollution.py <persona_dir>")
        print("示例: python scripts/cleanup_biography_pollution.py data/personas/sirius")
        sys.exit(1)

    persona_dir = Path(sys.argv[1])
    if not persona_dir.exists() or not persona_dir.is_dir():
        logger.error("无效的人格目录: %s", persona_dir)
        sys.exit(1)

    bio_dir = persona_dir / "memory" / "biography"
    if not bio_dir.exists():
        logger.error("传记目录不存在: %s", bio_dir)
        sys.exit(1)

    persona_name, persona_aliases = load_persona_config(persona_dir)

    print("=" * 60)
    print(f"开始清理传记身份混淆污染")
    print(f"人格目录: {persona_dir}")
    print(f"人格名称: {persona_name}")
    print(f"人格别名: {persona_aliases}")
    print("=" * 60)

    # 清理别名索引
    alias_cleaned = clean_alias_index(bio_dir, persona_name, persona_aliases)

    # 清理传记卡
    cards_cleaned = clean_biography_cards(bio_dir, persona_name, persona_aliases)

    print("=" * 60)
    print(f"清理完成！")
    print(f"  别名索引污染条目: {alias_cleaned}")
    print(f"  传记卡清理数量: {cards_cleaned}")
    print("=" * 60)
    print("提示：重启引擎后，传记管理器会自动应用代码层面的身份隔离防御。")


if __name__ == "__main__":
    main()
