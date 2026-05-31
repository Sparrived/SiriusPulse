"""
Token 估算工具模块

提供精确的 Token 数量估算，默认使用 tiktoken 获得精确值。
若 tiktoken 因某种原因不可用，自动降级到 CJK-aware 启发式估算。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Literal

# 模型特性
ModelType = Literal[
    "gpt-4",
    "gpt-3.5-turbo",
    "claude-3",
    "llama-2",
    "doubao-seed",
    "generic",
]

# 不同模型的 token 化比率估算（启发式 fallback 用）
MODEL_TOKEN_RATIO = {
    "gpt-4": {"english": 4, "chinese": 1.0},
    "gpt-3.5-turbo": {"english": 4, "chinese": 1.0},
    "claude-3": {"english": 4, "chinese": 1.0},
    "doubao-seed": {"english": 4, "chinese": 1.0},
    "generic": {"english": 4, "chinese": 1.0},
}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def estimate_tokens(text: str, *, model: ModelType = "generic") -> int:
    """估算文本的 token 数量。

    优先使用 tiktoken 获得精确值；若 tiktoken 不可用或模型无对应编码器，
    自动降级到 ``estimate_tokens_heuristic()``。

    Args:
        text: 待估算的文本
        model: 模型类型（用于 tiktoken 编码器选择及启发式 fallback）

    Returns:
        估算的 token 数量（空文本返回 0）
    """
    if not text:
        return 0

    text = text.strip()
    if not text:
        return 0

    tiktoken_result = _estimate_with_tiktoken(text, model=model)
    if tiktoken_result is not None:
        return tiktoken_result

    return estimate_tokens_heuristic(text, model=model)


def estimate_tokens_heuristic(
    text: str,
    *,
    model: ModelType = "generic",
) -> int:
    """启发式估算文本的 token 数量（无外部依赖）。

    基于语言分析（中英文分离）进行估算。相比简单的 len(text)/4，
    此方法对 CJK 文本更准确。

    Args:
        text: 待估算的文本
        model: 模型类型，用于选择估算参数

    Returns:
        估算的 token 数量
    """
    if not text:
        return 0

    text = text.strip()
    if not text:
        return 0

    model_params = MODEL_TOKEN_RATIO.get(model, MODEL_TOKEN_RATIO["generic"])
    english_chars_per_token = model_params["english"]
    chinese_char_tokens = model_params["chinese"]

    # CJK 统一表意文字 + 日文假名
    chinese_pattern = r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufa6f\u3040-\u309f\u30a0-\u30ff]"
    chinese_chars = re.findall(chinese_pattern, text)
    chinese_count = len(chinese_chars)

    english_words = re.findall(r"[a-zA-Z]+", text)
    english_char_count = sum(len(word) for word in english_words)

    other_count = len(text) - chinese_count - english_char_count

    chinese_tokens = int(chinese_count * chinese_char_tokens)
    english_tokens = max(1, (english_char_count + english_chars_per_token - 1) // english_chars_per_token)
    other_tokens = max(0, (other_count + 3) // 4)

    return int(max(1, chinese_tokens + english_tokens + other_tokens))


def get_token_estimation_stats(text: str) -> dict[str, int | None]:
    """获取文本的 token 估算统计信息（调试用）。

    同时返回 tiktoken 精确值和启发式估算值，便于对比。
    """
    text = text.strip()

    chinese_pattern = r"[\u4e00-\u9fff\u3400-\u4dbf\uf900-\ufa6f\u3040-\u309f\u30a0-\u30ff]"
    chinese_chars = re.findall(chinese_pattern, text)
    english_words = re.findall(r"[a-zA-Z]+", text)
    english_char_count = sum(len(word) for word in english_words)

    chinese_count = len(chinese_chars)
    other_count = len(text) - chinese_count - english_char_count

    heuristic_tokens = estimate_tokens_heuristic(text)
    tiktoken_tokens = _estimate_with_tiktoken(text)

    return {
        "total": tiktoken_tokens if tiktoken_tokens is not None else heuristic_tokens,
        "heuristic": heuristic_tokens,
        "tiktoken": tiktoken_tokens,
        "characters": len(text),
        "chinese_count": chinese_count,
        "english_count": english_char_count,
        "other_count": other_count,
    }


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _estimate_with_tiktoken(text: str, *, model: str = "gpt-4") -> int | None:
    """使用 tiktoken 进行精确 token 估算。

    若 tiktoken 未安装或模型无对应编码器，返回 None。
    """
    try:
        import tiktoken

        try:
            encoding = tiktoken.encoding_for_model(model)
        except KeyError:
            encoding = tiktoken.get_encoding("cl100k_base")

        return len(encoding.encode(text))
    except ImportError:
        return None


# ---------------------------------------------------------------------------
# Prompt token breakdown
# ---------------------------------------------------------------------------


@dataclass
class PromptTokenBreakdown:
    """Per-module token count breakdown for a single prompt.

    Captures how many tokens each section of the assembled prompt consumes,
    enabling precise budget diagnosis and optimisation.
    """

    # PromptFactory sections
    persona: int = 0
    identity: int = 0
    output_constraint: int = 0
    emotion: int = 0
    empathy: int = 0
    relationship: int = 0
    memory: int = 0
    interests: int = 0
    group_style: int = 0
    participants: int = 0
    cross_group: int = 0
    skills: int = 0
    glossary: int = 0
    output_format: int = 0

    # ContextAssembler sections
    diary: int = 0
    history_xml: int = 0
    cross_group_xml: int = 0

    # User message
    user_message: int = 0

    # Output (completion)
    output_total: int = 0

    # Totals
    system_prompt_total: int = 0
    total: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "persona": self.persona,
            "identity": self.identity,
            "output_constraint": self.output_constraint,
            "emotion": self.emotion,
            "empathy": self.empathy,
            "relationship": self.relationship,
            "memory": self.memory,
            "interests": self.interests,
            "group_style": self.group_style,
            "participants": self.participants,
            "cross_group": self.cross_group,
            "skills": self.skills,
            "glossary": self.glossary,
            "output_format": self.output_format,
            "diary": self.diary,
            "history_xml": self.history_xml,
            "cross_group_xml": self.cross_group_xml,
            "user_message": self.user_message,
            "output_total": self.output_total,
            "system_prompt_total": self.system_prompt_total,
            "total": self.total,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))

    @classmethod
    def from_json(cls, raw: str) -> "PromptTokenBreakdown":
        return cls(**json.loads(raw))


__all__ = [
    "estimate_tokens",
    "estimate_tokens_heuristic",
    "get_token_estimation_stats",
    "PromptTokenBreakdown",
    "MODEL_TOKEN_RATIO",
    "ModelType",
]
