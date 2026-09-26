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
    "provider": "旧版单 provider 兼容字段。已废弃：模型调用统一交给 AMKR。",
    "providers": "旧版 Provider 列表。已废弃：模型调用统一交给 AMKR。",
    "orchestration": "任务级编排配置。模型、温度与最大输出 token 都在 AMKR 的任务定义里配置，这里只保留本地参数。",
    "orchestration.task_enabled": "按任务控制是否启用。常见键包括 cognition_analyze、memory_extract。",
    "orchestration.task_enabled.memory_extract": "是否启用用户记忆提取任务。",
    "orchestration.task_enabled.cognition_analyze": "是否启用认知分析任务。",
    "orchestration.task_retries": "按任务设置失败重试次数。本地传输层参数，与模型无关。",
    "orchestration.task_timeout": "按任务设置请求超时（秒）。本地传输层参数，与模型无关。",
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
    "orchestration.reply_frequency_window_seconds": "回复频率限制的滑动窗口（秒）。",
    "orchestration.reply_frequency_max_replies": "窗口内允许的最大回复次数。",
    "orchestration.reply_frequency_exempt_on_mention": "被直接点名时是否跳过回复频率限制。",
    "orchestration.max_concurrent_llm_calls": "单会话上下文内允许的最大并发 LLM 调用数。0 表示不限制。",
    "orchestration.enable_tools": "是否允许 AI 调用外部 TOOL。",
    "orchestration.max_tool_rounds": "单轮回复最多允许多少轮连续 TOOL 调用。",
    "orchestration.tool_execution_timeout": "单次 TOOL 执行超时时间（秒）。0 表示不限制。",
    "orchestration.auto_install_tool_deps": "加载 TOOL 时是否自动安装缺失依赖。",
}


def build_default_orchestration_payload() -> dict[str, Any]:
    defaults = OrchestrationPolicy()
    memory_defaults = MemoryPolicy()
    return {
        "task_enabled": dict(defaults.task_enabled),
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
        "reply_frequency_window_seconds": defaults.reply_frequency_window_seconds,
        "reply_frequency_max_replies": defaults.reply_frequency_max_replies,
        "reply_frequency_exempt_on_mention": defaults.reply_frequency_exempt_on_mention,
        "max_concurrent_llm_calls": defaults.max_concurrent_llm_calls,
        "enable_tools": defaults.enable_tools,
        "max_tool_rounds": defaults.max_tool_rounds,
        "tool_execution_timeout": defaults.tool_execution_timeout,
        "auto_install_tool_deps": defaults.auto_install_tool_deps,
    }


def build_default_session_config_payload() -> dict[str, Any]:
    return {
        "generated_agent_key": "",
        "history_max_messages": 24,
        "history_max_chars": 6000,
        "max_recent_participant_messages": 5,
        "enable_auto_compression": True,
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
