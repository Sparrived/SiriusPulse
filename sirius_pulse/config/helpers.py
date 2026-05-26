"""多模型协同配置工具。

提供便捷的配置函数，用于在运行时配置多模型协同参数。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from sirius_pulse.config.models import Agent, MemoryPolicy, MultiModelConfig, OrchestrationPolicy, SessionConfig


_TASK_COGNITION_ANALYZE = "cognition_analyze"


def build_orchestration_policy_from_dict(
    orch_dict: dict[str, Any] | None,
    *,
    agent_model: str,
    return_none_if_empty: bool = False,
) -> OrchestrationPolicy | None:
    """Build an OrchestrationPolicy from raw JSON-like data."""
    raw = dict(orch_dict or {})
    recognized_keys = {
        "unified_model",
        "task_models",
        "task_enabled",
        "task_budgets",
        "task_temperatures",
        "task_max_tokens",
        "task_retries",
        "max_multimodal_inputs_per_turn",
        "max_multimodal_value_length",
        "enable_prompt_driven_splitting",
        "split_marker",
        "memory_extract_batch_size",
        "memory_extract_min_content_length",
        "consolidation_enabled",
        "consolidation_interval_seconds",
        "consolidation_min_entries",
        "consolidation_min_notes",
        "consolidation_min_facts",
        "session_reply_mode",
        "engagement_sensitivity",
        "heat_window_seconds",
        "pending_message_threshold",
        "min_reply_interval_seconds",
        "message_debounce_seconds",
        "memory",
        "enable_self_memory",
        "self_memory_extract_batch_size",
        "self_memory_min_chars",
        "self_memory_max_diary_prompt_entries",
        "self_memory_max_glossary_prompt_terms",
        "reply_frequency_window_seconds",
        "reply_frequency_max_replies",
        "reply_frequency_exempt_on_mention",
        "max_concurrent_llm_calls",
        "enable_skills",
        "skill_call_marker",
        "max_skill_rounds",
        "skill_execution_timeout",
        "auto_install_skill_deps",
    }
    has_config = any(key in raw for key in recognized_keys)
    if return_none_if_empty and not has_config:
        return None

    unified_model = str(raw.get("unified_model", "")).strip()
    task_models = {
        str(key).strip(): str(value).strip()
        for key, value in dict(raw.get("task_models", {})).items()
        if str(key).strip() and str(value).strip()
    }

    kwargs: dict[str, Any] = {
        "unified_model": unified_model,
        "task_models": task_models,
    }

    if "task_enabled" in raw and isinstance(raw.get("task_enabled"), dict):
        kwargs["task_enabled"] = {
            str(key).strip(): bool(value)
            for key, value in dict(raw.get("task_enabled", {})).items()
            if str(key).strip()
        }
    if "task_temperatures" in raw and isinstance(raw.get("task_temperatures"), dict):
        kwargs["task_temperatures"] = {
            str(key).strip(): float(value)
            for key, value in dict(raw.get("task_temperatures", {})).items()
            if str(key).strip()
        }
    if "task_max_tokens" in raw and isinstance(raw.get("task_max_tokens"), dict):
        kwargs["task_max_tokens"] = {
            str(key).strip(): int(value)
            for key, value in dict(raw.get("task_max_tokens", {})).items()
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
        "consolidation_min_entries": (int, 6),
        "consolidation_min_notes": (int, 4),
        "consolidation_min_facts": (int, 15),
        "session_reply_mode": (str, "always"),
        "engagement_sensitivity": (float, 0.5),
        "heat_window_seconds": (float, 60.0),
        "pending_message_threshold": (int, 4),
        "min_reply_interval_seconds": (float, 0.0),
        "enable_self_memory": (bool, True),
        "self_memory_extract_batch_size": (int, 3),
        "self_memory_min_chars": (int, 0),
        "self_memory_max_diary_prompt_entries": (int, 6),
        "self_memory_max_glossary_prompt_terms": (int, 15),
        "reply_frequency_window_seconds": (float, 60.0),
        "reply_frequency_max_replies": (int, 8),
        "reply_frequency_exempt_on_mention": (bool, True),
        "max_concurrent_llm_calls": (int, 1),
        "enable_skills": (bool, True),
        "max_skill_rounds": (int, 3),
        "skill_execution_timeout": (float, 30.0),
        "auto_install_skill_deps": (bool, True),
    }
    for field_name, (caster, _) in scalar_fields.items():
        if field_name not in raw:
            continue
        value = raw.get(field_name)
        kwargs[field_name] = caster(value) if caster is not bool else bool(value)

    if not kwargs.get("unified_model") and not kwargs.get("task_models"):
        kwargs["unified_model"] = agent_model

    memory_raw = raw.get("memory")
    if isinstance(memory_raw, dict):
        decay_raw = memory_raw.get("decay_schedule", {})
        decay_schedule = {
            int(key): float(value)
            for key, value in dict(decay_raw).items()
        } if isinstance(decay_raw, dict) else MemoryPolicy().decay_schedule
        kwargs["memory"] = MemoryPolicy(
            max_facts_per_user=int(memory_raw.get("max_facts_per_user", 50)),
            transient_confidence_threshold=float(memory_raw.get("transient_confidence_threshold", 0.85)),
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


def configure_orchestration_models(
    config: SessionConfig,
    **task_models: str,
) -> SessionConfig:
    """为会话配置多模型协同的任务模型。
    
    这个函数允许外部代码在收到 OrchestrationConfigError 后动态添加模型配置。
    使用此函数时，会自动切换到按任务配置模式（task_models）。
    
    Args:
        config: 会话配置对象
        **task_models: 任务名称到模型名称的映射。
            支持的任务名：
            - cognition_analyze: 认知分析
            - memory_extract: 记忆提取
            - response_generate: 回复生成
            - proactive_generate: 主动发言
            - vision: 多模态
            
    Returns:
        更新后的 SessionConfig 对象（原对象被修改并返回）
        
    Example:
        >>> config = SessionConfig(...)
        >>> from sirius_pulse.config import configure_orchestration_models
        >>> config = configure_orchestration_models(
        ...     config,
        ...     cognition_analyze="gpt-4-mini",
        ...     memory_extract="gpt-4-mini",
        ... )
    """
    if not config.orchestration:
        raise ValueError("config.orchestration 为 None，无法配置")
    
    # 合并新的任务模型配置
    updated_models = dict(config.orchestration.task_models)
    updated_models.update(task_models)
    
    # 当使用 task_models 时，清除 unified_model（两种模式互斥）
    # 创建新的 OrchestrationPolicy 对象
    updated_orchestration = replace(
        config.orchestration,
        unified_model="",  # 清除统一模型，切换到按任务配置模式
        task_models=updated_models,
    )
    
    # 创建并返回新的 SessionConfig
    updated_config = replace(
        config,
        orchestration=updated_orchestration,
    )
    
    return updated_config


def setup_multimodel_config(
    *,
    session_config: SessionConfig,
    task_models: dict[str, str],
    task_temperatures: dict[str, float] | None = None,
    task_max_tokens: dict[str, int] | None = None,
    task_retries: dict[str, int] | None = None,
    max_multimodal_inputs_per_turn: int = 4,
    max_multimodal_value_length: int = 4096,
) -> SessionConfig:
    """在现有会话配置中设置多模型编排。"""
    config = MultiModelConfig(
        task_models=task_models,
        task_temperatures=task_temperatures or {},
        task_max_tokens=task_max_tokens or {},
        task_retries=task_retries or {},
        max_multimodal_inputs_per_turn=max_multimodal_inputs_per_turn,
        max_multimodal_value_length=max_multimodal_value_length,
    )
    session_config.orchestration = config.to_orchestration_policy()
    return session_config


def create_multimodel_config(
    *,
    task_models: dict[str, str],
    task_temperatures: dict[str, float] | None = None,
    task_max_tokens: dict[str, int] | None = None,
    task_retries: dict[str, int] | None = None,
    max_multimodal_inputs_per_turn: int = 4,
    max_multimodal_value_length: int = 4096,
) -> MultiModelConfig:
    """创建多模型配置对象。"""
    return MultiModelConfig(
        task_models=task_models,
        task_temperatures=task_temperatures or {},
        task_max_tokens=task_max_tokens or {},
        task_retries=task_retries or {},
        max_multimodal_inputs_per_turn=max_multimodal_inputs_per_turn,
        max_multimodal_value_length=max_multimodal_value_length,
    )


def configure_orchestration_temperatures(
    config: SessionConfig,
    **task_temperatures: float,
) -> SessionConfig:
    """配置多模型协同任务的采样温度。
    
    Args:
        config: 会话配置对象
        **task_temperatures: 任务名称到温度值（0.0-2.0）的映射
        
    Returns:
        更新后的 SessionConfig 对象
    """
    if not config.orchestration:
        raise ValueError("config.orchestration 为 None，无法配置")
    
    updated_temps = dict(config.orchestration.task_temperatures)
    updated_temps.update(task_temperatures)
    
    updated_orchestration = replace(
        config.orchestration,
        task_temperatures=updated_temps,
    )
    
    updated_config = replace(
        config,
        orchestration=updated_orchestration,
    )
    
    return updated_config


def configure_orchestration_retries(
    config: SessionConfig,
    **task_retries: int,
) -> SessionConfig:
    """配置多模型协同任务的重试次数。
    
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
    task_models: dict[str, str] | None = None,
    task_temperatures: dict[str, float] | None = None,
    task_retries: dict[str, int] | None = None,
    **extra_fields: Any,
) -> SessionConfig:
    """一次性配置多模型协同的所有参数。
    
    这是一个便捷方法，可以一次性设置多个配置字段。
    如果指定了 task_models，会自动切换到按任务配置模式（task_models）。
    
    Args:
        config: 会话配置对象
        task_models: 任务模型映射
        task_temperatures: 任务温度映射
        task_retries: 任务重试次数映射
        **extra_fields: 其他 OrchestrationPolicy 字段（如 pending_message_threshold）
        
    Returns:
        更新后的 SessionConfig 对象
        
    Example:
        >>> config = configure_full_orchestration(
        ...     config,
        ...     task_models={
        ...         "memory_extract": "gpt-4-mini",
        ...         "event_extract": "gpt-4-mini",
        ...     },
        ...     task_temperatures={
        ...         "memory_extract": 0.1,
        ...     },
        ...     pending_message_threshold=0,
        ... )
    """
    if not config.orchestration:
        raise ValueError("config.orchestration 为 None，无法配置")
    
    # 准备更新字段
    update_fields: dict[str, Any] = {}
    
    # 如果指定了 task_models，清除 unified_model（切换到按任务配置模式）
    if task_models is not None:
        merged_models = dict(config.orchestration.task_models)
        merged_models.update(task_models)
        update_fields["task_models"] = merged_models
        update_fields["unified_model"] = ""  # 清除统一模型
    
    if task_temperatures is not None:
        merged_temps = dict(config.orchestration.task_temperatures)
        merged_temps.update(task_temperatures)
        update_fields["task_temperatures"] = merged_temps
    
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
