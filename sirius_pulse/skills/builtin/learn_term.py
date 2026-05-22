"""Built-in skill for recording glossary terms from conversations.

Allows the AI (or users) to explicitly teach new terms, slang, jargon,
or proper nouns that the model may not understand.
"""

from __future__ import annotations

from typing import Any

SKILL_META = {
    "name": "learn_term",
    "description": "记录聊天中出现的专有名词、黑话、梗或模型可能不了解的新概念。当遇到陌生术语时调用，供后续对话自动引用。应当主动使用该技能去学习更多的词汇的意思和新梗，提升自己的理解能力。",
    "version": "1.0.0",
    "tags": ["memory", "learning"],
    "silent": True,
    "dependencies": [],
    "parameters": {
        "term": {
            "type": "str",
            "description": "要记录的名词或术语",
            "required": True,
        },
        "definition": {
            "type": "str",
            "description": "该术语的解释或定义",
            "required": True,
        },
    },
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
