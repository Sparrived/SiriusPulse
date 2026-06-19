"""Shared policy for person-alias registration."""

from __future__ import annotations

import re

_SPACE_RE = re.compile(r"\s+")
_WRAPPER_CHARS = "\"'“”‘’「」『』《》<>（）()[]【】"
_GENERIC_ALIAS_TERMS = {
    "你",
    "你们",
    "他",
    "她",
    "ta",
    "大家",
    "各位",
    "所有人",
    "朋友",
    "朋友们",
    "同学",
    "同学们",
    "老师",
    "老师们",
    "哥",
    "哥哥",
    "大哥",
    "老哥",
    "小哥",
    "哥们",
    "哥们儿",
    "兄弟",
    "兄弟们",
    "姐",
    "姐姐",
    "大姐",
    "老姐",
    "小姐",
    "小姐姐",
    "姐妹",
    "姐妹们",
    "弟",
    "弟弟",
    "老弟",
    "小弟",
    "妹",
    "妹妹",
    "老妹",
    "小妹",
    "大佬",
    "老板",
    "老板们",
    "亲",
    "亲亲",
    "宝",
    "宝宝",
    "宝贝",
    "主人",
}
_KINSHIP_ONLY_CHARS = set("哥姐弟妹兄姊")


def normalize_person_alias(alias: str) -> str:
    """Normalize an alias key for storage and lookup."""
    return _SPACE_RE.sub(" ", str(alias or "").strip().strip(_WRAPPER_CHARS)).lower()


def is_generic_person_alias(alias: str) -> bool:
    """Return True when the text is only a broad way to address people."""
    normalized = normalize_person_alias(alias)
    compact = normalized.replace(" ", "")
    if not compact:
        return True
    if compact in _GENERIC_ALIAS_TERMS:
        return True
    if compact.endswith("们") and compact[:-1] in _GENERIC_ALIAS_TERMS:
        return True
    return all(ch in _KINSHIP_ONLY_CHARS for ch in compact)


def validate_person_alias(alias: str) -> tuple[bool, str, str]:
    """Validate and normalize a person alias.

    Returns (is_valid, normalized_alias, reason).
    """
    normalized = normalize_person_alias(alias)
    if not normalized:
        return False, "", "alias 不能为空"
    if is_generic_person_alias(normalized):
        return False, normalized, "alias 是宽泛称呼，不能登记为某个人的别称"
    return True, normalized, ""


__all__ = [
    "is_generic_person_alias",
    "normalize_person_alias",
    "validate_person_alias",
]
