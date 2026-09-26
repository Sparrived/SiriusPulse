"""本地编排配置工具。

提供便捷的配置函数，用于在运行时调整 OrchestrationPolicy 的本地参数。

模型与采样参数**不在**这里配置：它们属于 AMKR 的任务定义。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from sirius_pulse.config.models import (
    Agent,
    MemoryPolicy,
    OrchestrationPolicy,
    SessionConfig,
)


def build_orchestration_policy_from_dict(
    orch_dict: dict[str, Any] | None,
    *,
    return_none_if_empty: bool = False,
) -> OrchestrationPolicy | None:
    """Build an OrchestrationPolicy from raw JSON-like data.

    历史配置里的 ``unified_model`` / ``task_models`` / ``task_temperatures`` /
    ``task_max_tokens`` 会被忽略：模型与采样参数由 AMKR 的任务定义决定。
    """
    raw = dict(orch_dict or {})
    recognized_keys = {
        "task_enabled",
        "task_budgets",
        "task_retries",
        "max_multimodal_inputs_per_turn",
        "max_multimodal_value_length",
        "enable_prompt_driven_splitting",
        "split_marker",
        "memory_extract_batch_size",
        "memory_extract_min_content_length",
        "consolidation_enabled",
        "consolidation_interval_seconds",
        "memory_idle_consolidation_seconds",
        "consolidation_min_entries",
        "consolidation_min_notes",
        "consolidation_min_facts",
        "engagement_sensitivity",
        "heat_window_seconds",
        "pending_message_threshold",
        "min_reply_interval_seconds",
        "message_debounce_seconds",
        "memory",
        "reply_frequency_window_seconds",
        "reply_frequency_max_replies",
        "reply_frequency_exempt_on_mention",
        "max_concurrent_llm_calls",
        "enable_tools",
        "max_tool_rounds",
        "tool_execution_timeout",
        "auto_install_tool_deps",
    }
    has_config = any(key in raw for key in recognized_keys)
    if return_none_if_empty and not has_config:
        return None

    kwargs: dict[str, Any] = {}

    if "task_enabled" in raw and isinstance(raw.get("task_enabled"), dict):
        kwargs["task_enabled"] = {
            str(key).strip(): bool(value)
            for key, value in dict(raw.get("task_enabled", {})).items()
            if str(key).strip()
        }
    if "task_retries" in raw and isinstance(raw.get("task_retries"), dict):
        kwargs["task_retries"] = {
            str(key).strip(): int(value)
            for key, value in dict(raw.get("task_retries", {})).items()
            if str(key).strip()
        }

    scalar_fields: dict[str, tuple[type, Any]] = {
        "max_multimodal_inputs_per_turn": (int, 4),
        "max_multimodal_value_length": (int, 4096),
        "enable_prompt_driven_splitting": (bool, True),
        "memory_extract_batch_size": (int, 1),
        "memory_extract_min_content_length": (int, 0),
        "event_extract_batch_size": (int, 5),
        "consolidation_interval_seconds": (int, 7200),
        "memory_idle_consolidation_seconds": (int, 3600),
        "consolidation_min_entries": (int, 6),
        "consolidation_min_notes": (int, 4),
        "consolidation_min_facts": (int, 15),
        "engagement_sensitivity": (float, 0.5),
        "heat_window_seconds": (float, 60.0),
        "pending_message_threshold": (int, 4),
        "min_reply_interval_seconds": (float, 0.0),
        "reply_frequency_window_seconds": (float, 60.0),
        "reply_frequency_max_replies": (int, 8),
        "reply_frequency_exempt_on_mention": (bool, True),
        "max_concurrent_llm_calls": (int, 1),
        "enable_tools": (bool, True),
        "max_tool_rounds": (int, 3),
        "tool_execution_timeout": (float, 30.0),
        "auto_install_tool_deps": (bool, True),
    }
    for field_name, (caster, _) in scalar_fields.items():
        if field_name not in raw:
            continue
        value = raw.get(field_name)
        kwargs[field_name] = caster(value) if caster is not bool else bool(value)

    memory_raw = raw.get("memory")
    if isinstance(memory_raw, dict):
        decay_raw = memory_raw.get("decay_schedule", {})
        decay_schedule = (
            {int(key): float(value) for key, value in dict(decay_raw).items()}
            if isinstance(decay_raw, dict)
            else MemoryPolicy().decay_schedule
        )
        kwargs["memory"] = MemoryPolicy(
            max_facts_per_user=int(memory_raw.get("max_facts_per_user", 50)),
            transient_confidence_threshold=float(
                memory_raw.get("transient_confidence_threshold", 0.85)
            ),
            event_dedup_window_minutes=int(memory_raw.get("event_dedup_window_minutes", 5)),
            max_observed_set_size=int(memory_raw.get("max_observed_set_size", 100)),
            max_summary_facts_per_type=int(memory_raw.get("max_summary_facts_per_type", 5)),
            max_summary_total_chars=int(memory_raw.get("max_summary_total_chars", 2000)),
            decay_schedule=decay_schedule,
        )

    return OrchestrationPolicy(**kwargs)


def auto_configure_multimodal_agent(
    agent: Agent,
    *,
    multimodal_model: str | None = None,
) -> Agent:
    """为 Agent 配置多模态模型（如果有图片输入时使用）。

    不进行自动推断，而是要求用户显式指定或在 Agent.metadata 中设置。
    这样可以兼容各种平台（有些平台可能没有 vision 版本）。

    Args:
        agent: AI Agent 配置对象
        multimodal_model: 多模态模型名称（可选）。如果提供，将覆盖 agent.metadata 中的设置。
                         如果不提供，将检查 agent.metadata 中是否已有配置。

    Returns:
        更新后的 Agent 对象（原对象被修改）

    Example:
        >>> agent = Agent(name="Assistant", persona="helpful", model="gpt-4o-mini")
        >>> agent = auto_configure_multimodal_agent(agent, multimodal_model="gpt-4o")
        >>> agent.metadata["multimodal_model"]
        'gpt-4o'
    """
    # 如果参数中指定了多模态模型，直接设置
    if multimodal_model:
        agent.metadata["multimodal_model"] = multimodal_model
        return agent

    # 如果 metadata 中已经有了，就保留现有配置
    if "multimodal_model" in agent.metadata:
        return agent

    # 否则不做任何操作，让用户显式配置
    return agent


def create_agent_with_multimodal(
    *,
    name: str,
    persona: str,
    model: str,
    multimodal_model: str,
    temperature: float = 0.7,
    max_tokens: int = 512,
    **metadata: Any,
) -> Agent:
    """便捷函数：一次性创建带有多模态模型的 Agent。

    Args:
        name: Agent 名称
        persona: Agent 人设
        model: 主模型名称
        multimodal_model: 多模态模型名称（当有图片输入时使用）
        temperature: 温度参数
        max_tokens: 最大输出 token 数
        **metadata: 其他元数据

    Returns:
        已配置多模态模型的 Agent 对象

    Example:
        >>> agent = create_agent_with_multimodal(
        ...     name="Assistant",
        ...     persona="helpful",
        ...     model="gpt-4o-mini",
        ...     multimodal_model="gpt-4o",
        ... )
    """
    agent = Agent(
        name=name,
        persona=persona,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        metadata={
            "multimodal_model": multimodal_model,
            **metadata,
        },
    )
    return agent


def configure_orchestration_retries(
    config: SessionConfig,
    **task_retries: int,
) -> SessionConfig:
    """配置各任务的失败重试次数。

    重试属于本地传输层参数（与模型无关），因此仍然保留在这里。

    Args:
        config: 会话配置对象
        **task_retries: 任务名称到重试次数的映射

    Returns:
        更新后的 SessionConfig 对象
    """
    if not config.orchestration:
        raise ValueError("config.orchestration 为 None，无法配置")

    updated_retries = dict(config.orchestration.task_retries)
    updated_retries.update(task_retries)

    updated_orchestration = replace(
        config.orchestration,
        task_retries=updated_retries,
    )

    updated_config = replace(
        config,
        orchestration=updated_orchestration,
    )

    return updated_config


def configure_full_orchestration(
    config: SessionConfig,
    task_retries: dict[str, int] | None = None,
    **extra_fields: Any,
) -> SessionConfig:
    """一次性配置本地编排参数。

    模型与采样参数不在这里：它们由 AMKR 的任务定义决定。``task_models`` /
    ``task_temperatures`` 这类入参已被移除。

    Args:
        config: 会话配置对象
        task_retries: 任务重试次数映射
        **extra_fields: 其他 OrchestrationPolicy 字段（如 pending_message_threshold）

    Returns:
        更新后的 SessionConfig 对象

    Example:
        >>> config = configure_full_orchestration(
        ...     config,
        ...     task_retries={"memory_extract": 3},
        ...     pending_message_threshold=0,
        ... )
    """
    if not config.orchestration:
        raise ValueError("config.orchestration 为 None，无法配置")

    # 准备更新字段
    update_fields: dict[str, Any] = {}

    if task_retries is not None:
        merged_retries = dict(config.orchestration.task_retries)
        merged_retries.update(task_retries)
        update_fields["task_retries"] = merged_retries

    # 合并其他字段
    update_fields.update(extra_fields)

    # 创建新的 OrchestrationPolicy
    updated_orchestration = replace(
        config.orchestration,
        **update_fields,
    )

    # 创建并返回新的 SessionConfig
    updated_config = replace(
        config,
        orchestration=updated_orchestration,
    )

    return updated_config
