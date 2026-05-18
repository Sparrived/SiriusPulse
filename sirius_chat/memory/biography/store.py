"""人物传记持久化 — JSON 文件存储。

Layout:
    {persona_dir}/memory/biography/
    ├── qq_123456.json          # UserPersonaCard（全局一张卡）
    ├── qq_789012.json
    └── index.json              # 全局别名索引（一对多）
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from sirius_chat.memory.biography.models import AliasEntry, UserPersonaCard

logger = logging.getLogger(__name__)


def _atomic_write(path: Path, data: dict[str, Any]) -> None:
    """原子写 JSON 文件（临时文件 + replace）。"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


class BiographyStore:
    """人物传记持久化层。

    - 卡片文件：{dir}/{user_id}.json
    - 别名索引：{dir}/index.json
    """

    def __init__(self, work_path: Path | str) -> None:
        self._base = Path(work_path) / "memory" / "biography"
        self._base.mkdir(parents=True, exist_ok=True)

    # ── 卡片读写 ──

    def load_card(self, user_id: str) -> UserPersonaCard | None:
        path = self._card_path(user_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return UserPersonaCard.from_dict(data)
        except (OSError, json.JSONDecodeError, TypeError):
            logger.warning("加载传记卡失败: %s", user_id)
            return None

    def save_card(self, card: UserPersonaCard) -> None:
        path = self._card_path(card.user_id)
        _atomic_write(path, card.to_dict())

    def load_all_cards(self) -> list[UserPersonaCard]:
        cards: list[UserPersonaCard] = []
        for path in self._base.glob("*.json"):
            if path.name == "index.json":
                continue
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                cards.append(UserPersonaCard.from_dict(data))
            except (OSError, json.JSONDecodeError, TypeError):
                continue
        return cards

    # ── 别名索引读写 ──

    def load_alias_index(self) -> dict[str, list[AliasEntry]]:
        path = self._base / "index.json"
        if not path.exists():
            return {}
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            index: dict[str, list[AliasEntry]] = {}
            for alias, entries in raw.items():
                index[alias] = []
                for e in entries:
                    if isinstance(e, dict):
                        index[alias].append(AliasEntry.from_dict(e))
            return index
        except (OSError, json.JSONDecodeError, TypeError):
            logger.warning("加载别名索引失败")
            return {}

    def save_alias_index(self, alias_index: dict[str, list[AliasEntry]]) -> None:
        path = self._base / "index.json"
        data: dict[str, list[dict[str, Any]]] = {}
        for alias, entries in alias_index.items():
            data[alias] = [e.to_dict() for e in entries]
        _atomic_write(path, data)

    # ── 工具 ──

    def _card_path(self, user_id: str) -> Path:
        safe = _safe_name(user_id)
        return self._base / f"{safe}.json"


def _safe_name(name: str) -> str:
    """将 user_id 转为安全的文件名。"""
    import re
    base = re.sub(r"[^a-zA-Z0-9_\-\u4e00-\u9fff]+", "_", name.strip())
    base = re.sub(r"_+", "_", base).strip("_")
    return base or "default"


__all__ = ["BiographyStore"]
