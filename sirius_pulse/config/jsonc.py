from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from sirius_pulse.config.models import MemoryPolicy, OrchestrationPolicy

_SESSION_CONFIG_HEADER = [
    "Sirius Chat session config.",
    "This file accepts JSONC-style comments and can be edited directly.",
]

_SESSION_CONFIG_COMMENTS = {
    "generated_agent_key": "当前启用的 generated agent 标识。首次初始化后 main.py 会自动回写这个字段。",
    "history_max_messages": "参与上下文保留的最近消息数量。",
    "history_max_chars": "触发历史压缩前保留的最近字符预算。",
    "max_recent_participant_messages": "每个参与者额外保留的最近发言条数。",
    "enable_auto_compression": "超过上下文预算时是否自动压缩历史。",
    "provider": "旧版单 provider 兼容字段。新配置优先使用 providers 列表。",
    "provider.type": "Provider 类型，例如 openai-compatible / bigmodel / deepseek / siliconflow。",
    "provider.base_url": "Provider 基地址。留空时使用该平台默认值。",
    "provider.api_key": "Provider API Key。",
    "provider.healthcheck_model": "可用性检测模型名。用于 /provider add 或自动探测流程。",
    "provider.enabled": "是否启用该 provider。",
    "provider.models": "显式声明该 provider 可处理的模型列表；自动路由时优先按这里精确匹配。",
    "providers": "Provider 列表。main.py 和 sirius-chat CLI 会优先读取这个字段。",
    "providers[].type": "Provider 类型，例如 openai-compatible / bigmodel / deepseek / siliconflow。",
    "providers[].base_url": "Provider 基地址。留空时使用该平台默认值。",
    "providers[].api_key": "Provider API Key。",
    "providers[].healthcheck_model": "可用性检测模型名。用于 /provider add 或自动探测流程。",
    "providers[].enabled": "是否启用该 provider。",
    "providers[].models": "显式声明该 provider 可处理的模型列表；自动路由时优先按这里精确匹配。",
    "orchestration": "任务级编排配置，可为 cognition_analyze、memory_extract、response_generate、vision 等任务单独设置模型与参数。",
    "orchestration.unified_model": "统一模型。若 task_models 未单独指定任务模型，则优先使用这里。",
    "orchestration.task_models": "按任务单独指定模型。常见键包括 cognition_analyze、memory_extract、response_generate、vision。",
    "orchestration.task_enabled": "按任务控制是否启用。常见键包括 cognition_analyze、memory_extract。",
    "orchestration.task_enabled.memory_extract": "是否启用用户记忆提取任务。",
    "orchestration.task_enabled.cognition_analyze": "是否启用认知分析任务。",
    "orchestration.task_temperatures": "按任务设置温度参数。键通常与 task_models 相同。",
    "orchestration.task_max_tokens": "按任务设置最大输出 token。键通常与 task_models 相同。",
    "orchestration.task_retries": "按任务设置失败重试次数。键通常与 task_models 相同。",
    "orchestration.max_multimodal_inputs_per_turn": "单轮最多保留多少个多模态输入。",
    "orchestration.max_multimodal_value_length": "单个多模态值的最大长度。",
    "orchestration.enable_prompt_driven_splitting": "是否启用提示词驱动的消息分割。分割标记已内置为 <MSG_SPLIT>。",
    "orchestration.memory_extract_batch_size": "每累计多少条消息触发一次用户记忆提取。",
    "orchestration.memory_extract_min_content_length": "消息长度至少达到多少字符才触发用户记忆提取。",
    "orchestration.consolidation_interval_seconds": "后台记忆归纳执行间隔（秒）。框架启动并进入 live session 后会静默常驻运行。",
    "orchestration.consolidation_min_entries": "事件条目达到多少条后才触发归纳。",
    "orchestration.consolidation_min_notes": "摘要条目达到多少条后才触发归纳。",
    "orchestration.consolidation_min_facts": "事实条目达到多少条后才触发归纳。",
    "orchestration.engagement_sensitivity": "参与敏感度，范围 0 到 1。越大越主动。",
    "orchestration.heat_window_seconds": "热度分析的滑动时间窗口（秒）。",
    "orchestration.pending_message_threshold": "单会话待处理消息积压超过该阈值后，runtime 会进入静默批处理并合并同一说话人的连续消息。设为 0 表示关闭。",
    "orchestration.min_reply_interval_seconds": "两次 AI 实际回复之间的最小间隔（秒）。大于 0 时，runtime 会在间隔内继续蓄积消息，并在下次判断前按静默批处理方式合并。",
    "orchestration.memory": "中央记忆系统参数。",
    "orchestration.memory.max_facts_per_user": "每个用户最多保留多少条记忆事实。",
    "orchestration.memory.transient_confidence_threshold": "临时记忆阈值。高于该置信度的事实更可能保留。",
    "orchestration.memory.event_dedup_window_minutes": "事件去重时间窗（分钟）。",
    "orchestration.memory.max_observed_set_size": "观测集合最大长度。",
    "orchestration.memory.max_summary_facts_per_type": "每类摘要最多保留多少条事实。",
    "orchestration.memory.max_summary_total_chars": "摘要注入提示词时的总字符上限。",
    "orchestration.memory.decay_schedule": "遗忘曲线配置。键为天数，值为衰减系数。",
    "orchestration.enable_self_memory": "是否启用 AI 自身记忆（日记 + 术语表）。",
    "orchestration.self_memory_extract_batch_size": "每多少条 AI 回复触发一次自身记忆提取。",
    "orchestration.self_memory_min_chars": "当单条 AI 回复达到多少字符时也会触发自身记忆提取。",
    "orchestration.self_memory_max_diary_prompt_entries": "注入提示词的 diary 条目上限。",
    "orchestration.self_memory_max_glossary_prompt_terms": "注入提示词的 glossary 术语上限。",
    "orchestration.reply_frequency_window_seconds": "回复频率限制的滑动窗口（秒）。",
    "orchestration.reply_frequency_max_replies": "窗口内允许的最大回复次数。",
    "orchestration.reply_frequency_exempt_on_mention": "被直接点名时是否跳过回复频率限制。",
    "orchestration.max_concurrent_llm_calls": "单会话上下文内允许的最大并发 LLM 调用数。0 表示不限制。",
    "orchestration.enable_skills": "是否允许 AI 调用外部 SKILL。",
    "orchestration.max_skill_rounds": "单轮回复最多允许多少轮连续 SKILL 调用。",
    "orchestration.skill_execution_timeout": "单次 SKILL 执行超时时间（秒）。0 表示不限制。",
    "orchestration.agent_max_skill_candidates": "每个 Agent 回合最多向模型注入多少个与当前请求相关的 SKILL schema。",
    "orchestration.auto_install_skill_deps": "加载 SKILL 时是否自动安装缺失依赖。",
    "orchestration.plan_mode_enabled": "Enable hidden planning sessions.",
    "orchestration.plan_mode_limit_normal_tools": (
        "Limit normal chat to lightweight runtime tools when plan mode is enabled."
    ),
    "orchestration.plan_mode_allow_light_chat": (
        "Allow unrelated light chat in the same group while a plan is active."
    ),
    "orchestration.plan_mode_chat_awareness_enabled": (
        "Inject the active plan's public progress snapshot into normal chat prompts."
    ),
    "orchestration.plan_mode_presence_enabled": (
        "Send a short persona-style status message when hidden planning starts."
    ),
    "orchestration.plan_mode_presence_min_interval_seconds": (
        "Minimum seconds between planning status messages."
    ),
}


def build_default_orchestration_payload() -> dict[str, Any]:
    defaults = OrchestrationPolicy()
    memory_defaults = MemoryPolicy()
    return {
        "unified_model": defaults.unified_model,
        "task_models": dict(defaults.task_models),
        "task_enabled": dict(defaults.task_enabled),
        "task_temperatures": dict(defaults.task_temperatures),
        "task_max_tokens": dict(defaults.task_max_tokens),
        "task_retries": dict(defaults.task_retries),
        "max_multimodal_inputs_per_turn": defaults.max_multimodal_inputs_per_turn,
        "max_multimodal_value_length": defaults.max_multimodal_value_length,
        "enable_prompt_driven_splitting": defaults.enable_prompt_driven_splitting,
        "memory_extract_batch_size": defaults.memory_extract_batch_size,
        "memory_extract_min_content_length": defaults.memory_extract_min_content_length,
        "event_extract_batch_size": defaults.event_extract_batch_size,
        "consolidation_interval_seconds": defaults.consolidation_interval_seconds,
        "memory_idle_consolidation_seconds": defaults.memory_idle_consolidation_seconds,
        "consolidation_min_entries": defaults.consolidation_min_entries,
        "consolidation_min_notes": defaults.consolidation_min_notes,
        "consolidation_min_facts": defaults.consolidation_min_facts,
        "engagement_sensitivity": defaults.engagement_sensitivity,
        "heat_window_seconds": defaults.heat_window_seconds,
        "pending_message_threshold": defaults.pending_message_threshold,
        "min_reply_interval_seconds": defaults.min_reply_interval_seconds,
        "memory": {
            "max_facts_per_user": memory_defaults.max_facts_per_user,
            "transient_confidence_threshold": memory_defaults.transient_confidence_threshold,
            "event_dedup_window_minutes": memory_defaults.event_dedup_window_minutes,
            "max_observed_set_size": memory_defaults.max_observed_set_size,
            "max_summary_facts_per_type": memory_defaults.max_summary_facts_per_type,
            "max_summary_total_chars": memory_defaults.max_summary_total_chars,
            "decay_schedule": dict(memory_defaults.decay_schedule),
        },
        "enable_self_memory": defaults.enable_self_memory,
        "self_memory_extract_batch_size": defaults.self_memory_extract_batch_size,
        "self_memory_min_chars": defaults.self_memory_min_chars,
        "self_memory_max_diary_prompt_entries": defaults.self_memory_max_diary_prompt_entries,
        "self_memory_max_glossary_prompt_terms": defaults.self_memory_max_glossary_prompt_terms,
        "reply_frequency_window_seconds": defaults.reply_frequency_window_seconds,
        "reply_frequency_max_replies": defaults.reply_frequency_max_replies,
        "reply_frequency_exempt_on_mention": defaults.reply_frequency_exempt_on_mention,
        "max_concurrent_llm_calls": defaults.max_concurrent_llm_calls,
        "enable_skills": defaults.enable_skills,
        "max_skill_rounds": defaults.max_skill_rounds,
        "skill_execution_timeout": defaults.skill_execution_timeout,
        "agent_max_skill_candidates": defaults.agent_max_skill_candidates,
        "auto_install_skill_deps": defaults.auto_install_skill_deps,
        "plan_mode_enabled": defaults.plan_mode_enabled,
        "plan_mode_limit_normal_tools": defaults.plan_mode_limit_normal_tools,
        "plan_mode_allow_light_chat": defaults.plan_mode_allow_light_chat,
        "plan_mode_chat_awareness_enabled": defaults.plan_mode_chat_awareness_enabled,
        "plan_mode_presence_enabled": defaults.plan_mode_presence_enabled,
        "plan_mode_presence_min_interval_seconds": (
            defaults.plan_mode_presence_min_interval_seconds
        ),
    }


def build_default_session_config_payload() -> dict[str, Any]:
    sample_provider = {
        "type": "openai-compatible",
        "base_url": "https://api.openai.com",
        "api_key": "your-api-key-here",
        "healthcheck_model": "",
        "enabled": True,
        "models": [],
    }
    return {
        "generated_agent_key": "",
        "history_max_messages": 24,
        "history_max_chars": 6000,
        "max_recent_participant_messages": 5,
        "enable_auto_compression": True,
        "provider": dict(sample_provider),
        "providers": [dict(sample_provider)],
        "orchestration": build_default_orchestration_payload(),
    }


def strip_json_comments(content: str) -> str:
    """Strip JSONC-style comments while preserving quoted strings."""

    result: list[str] = []
    in_string = False
    escaped = False
    line_comment = False
    block_comment = False
    index = 0

    while index < len(content):
        char = content[index]
        next_char = content[index + 1] if index + 1 < len(content) else ""

        if line_comment:
            if char in "\r\n":
                line_comment = False
                result.append(char)
            index += 1
            continue

        if block_comment:
            if char == "*" and next_char == "/":
                block_comment = False
                index += 2
                continue
            if char in "\r\n":
                result.append(char)
            index += 1
            continue

        if in_string:
            result.append(char)
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            index += 1
            continue

        if char == '"':
            in_string = True
            result.append(char)
            index += 1
            continue

        if char == "/" and next_char == "/":
            line_comment = True
            index += 2
            continue

        if char == "/" and next_char == "*":
            block_comment = True
            index += 2
            continue

        result.append(char)
        index += 1

    return "".join(result)


def loads_json_document(content: str) -> Any:
    """Parse JSON or JSONC content."""

    return json.loads(strip_json_comments(content))


def load_json_document(path: Path | str) -> Any:
    """Load a JSON or JSONC document from disk."""

    document_path = Path(path)
    return loads_json_document(document_path.read_text(encoding="utf-8-sig"))


def _indent_lines(lines: list[str], spaces: int) -> list[str]:
    prefix = " " * spaces
    return [f"{prefix}{line}" if line else prefix for line in lines]


def _render_jsonc_value(value: Any, *, path: str) -> list[str]:
    if isinstance(value, Mapping):
        return _render_jsonc_mapping(value, path=path)
    if isinstance(value, list):
        return _render_jsonc_list(value, path=path)
    return [json.dumps(value, ensure_ascii=False)]


def _render_jsonc_mapping(payload: Mapping[str, Any], *, path: str) -> list[str]:
    lines = ["{"]
    items = list(payload.items())
    for index, (key, value) in enumerate(items):
        child_path = f"{path}.{key}" if path else str(key)
        comment = _SESSION_CONFIG_COMMENTS.get(child_path, "")
        if comment:
            lines.append(f"// {comment}")

        rendered_value = _render_jsonc_value(value, path=child_path)
        suffix = "," if index < len(items) - 1 else ""
        if len(rendered_value) == 1:
            lines.append(f"{json.dumps(str(key), ensure_ascii=False)}: {rendered_value[0]}{suffix}")
            continue

        lines.append(f"{json.dumps(str(key), ensure_ascii=False)}: {rendered_value[0]}")
        lines.extend(_indent_lines(rendered_value[1:-1], 2))
        lines.append(f"{rendered_value[-1]}{suffix}")

    lines.append("}")
    return lines


def _render_jsonc_list(payload: list[Any], *, path: str) -> list[str]:
    lines = ["["]
    for index, item in enumerate(payload):
        child_path = f"{path}[]" if path else "[]"
        rendered_value = _render_jsonc_value(item, path=child_path)
        suffix = "," if index < len(payload) - 1 else ""
        if len(rendered_value) == 1:
            lines.append(f"{rendered_value[0]}{suffix}")
            continue

        lines.append(rendered_value[0])
        lines.extend(_indent_lines(rendered_value[1:-1], 2))
        lines.append(f"{rendered_value[-1]}{suffix}")

    lines.append("]")
    return lines


def render_session_config_jsonc(payload: Mapping[str, Any]) -> str:
    """Render a JSONC session config with inline guidance comments."""

    lines = [f"// {line}" for line in _SESSION_CONFIG_HEADER]
    rendered = _render_jsonc_mapping(payload, path="")
    lines.append(rendered[0])
    lines.extend(_indent_lines(rendered[1:-1], 2))
    lines.append(rendered[-1])
    return "\n".join(lines) + "\n"


def write_session_config_jsonc(path: Path | str, payload: Mapping[str, Any]) -> None:
    """Write a commented session config document to disk."""

    document_path = Path(path)
    document_path.parent.mkdir(parents=True, exist_ok=True)
    document_path.write_text(render_session_config_jsonc(payload), encoding="utf-8")
