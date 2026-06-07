"""人格资产生成：问卷 → LLM 生成 → 人格预设文件。

子模块
------
templates   数据模型 + 文件 I/O（原 prompt_templates）
builders    LLM 异步生成（原 prompt_builders）
"""

from __future__ import annotations

from sirius_pulse.persona_generation.builders import (
    abuild_roleplay_prompt_from_answers_and_apply,
    agenerate_agent_prompts_from_answers,
    agenerate_from_persona_spec,
    aregenerate_agent_prompt_from_dependencies,
    aupdate_agent_prompt,
)
from sirius_pulse.persona_generation.templates import (
    DependencyFileSnapshot,
    GeneratedSessionPreset,
    PersonaGenerationResponseError,
    PersonaGenerationTrace,
    PersonaSpec,
    PreparedPersonaGenerationInput,
    RolePlayAnswer,
    RolePlayQuestion,
    create_session_config_from_selected_agent,
    generate_humanized_roleplay_questions,
    list_roleplay_question_templates,
    load_generated_agent_library,
    load_persona_generation_traces,
    load_persona_spec,
    persist_generated_agent_profile,
    select_generated_agent_profile,
)

__all__ = [
    "RolePlayQuestion",
    "RolePlayAnswer",
    "PersonaSpec",
    "DependencyFileSnapshot",
    "PersonaGenerationTrace",
    "PreparedPersonaGenerationInput",
    "PersonaGenerationResponseError",
    "GeneratedSessionPreset",
    "list_roleplay_question_templates",
    "generate_humanized_roleplay_questions",
    "load_persona_generation_traces",
    "load_generated_agent_library",
    "load_persona_spec",
    "persist_generated_agent_profile",
    "select_generated_agent_profile",
    "create_session_config_from_selected_agent",
    "agenerate_from_persona_spec",
    "agenerate_agent_prompts_from_answers",
    "abuild_roleplay_prompt_from_answers_and_apply",
    "aupdate_agent_prompt",
    "aregenerate_agent_prompt_from_dependencies",
]
