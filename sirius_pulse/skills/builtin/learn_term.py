"""Built-in skill for recording glossary terms from conversations.

Allows the AI (or users) to explicitly teach new terms, slang, jargon,
or proper nouns that the model may not understand.
"""

from __future__ import annotations

from typing import Any

from sirius_pulse.config.config_builder import ConfigBuilder

_config = ConfigBuilder()
_config.group("术语学习").add(
    "term",
    type="str",
    description="要记录的名词或术语",
    required=True,
)
_config.group("术语学习").add(
    "definition",
    type="str",
    description="该术语的解释或定义",
    required=True,
)

SKILL_META = {
    "name": "learn_term",
    "description": "群聊里出现大家反复使用的新梗、黑话、项目代号或专有名词，且含义已被解释清楚时使用；记录后方便以后自然接话。",
    "version": "1.0.0",
    "tags": ["memory", "learning"],
    "silent": True,
    "dependencies": [],
    "parameters": _config.build(),
}


def run(
    term: str = "",
    definition: str = "",
    **kwargs: Any,
) -> dict[str, Any]:
    """Validate term parameters and return a structured result.

    The actual glossary persistence is handled by the engine after
    skill execution (engine checks skill_name == 'learn_term').
    """
    if not term or not term.strip():
        return {
            "success": False,
            "error": "term 不能为空",
            "summary": "术语记录失败：缺少 term",
        }
    if not definition or not definition.strip():
        return {
            "success": False,
            "error": "definition 不能为空",
            "summary": "术语记录失败：缺少 definition",
        }

    clean_term = term.strip()
    clean_definition = definition.strip()

    return {
        "success": True,
        "summary": f"已记录术语「{clean_term}」",
        "text_blocks": [f"术语「{clean_term}」: {clean_definition}"],
        "internal_metadata": {
            "term": clean_term,
            "definition": clean_definition,
        },
    }
