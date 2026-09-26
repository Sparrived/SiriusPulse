"""Shared WebUI model catalog contract.

Sirius Pulse 不再维护 Provider/模型清单：``model`` 字段直接填 AMKR 的**任务名**，
由 AMKR 按任务定义决定真实模型、供应商与采样参数。因此本目录返回的是本框架在
AMKR 工作空间里定义的那些任务名，供编排页下拉选择。
"""

from __future__ import annotations

from typing import Any, TypedDict


class ModelChoice(TypedDict, total=False):
    label: str
    value: str
    tags: list[str]


class ModelCatalog(TypedDict):
    available_models: list[str]
    model_choices: list[ModelChoice]


# 任务名的中文说明，用于下拉框标签；未列出的任务名原样显示。
TASK_LABELS: dict[str, str] = {
    "cognition_analyze": "认知分析",
    "memory_extract": "记忆提取",
    "response_generate": "对话生成",
    "work_mode_generate": "工作模式",
    "proactive_generate": "主动发言",
    "passive_tool": "被动技能",
    "plugin_analyze": "插件分析",
    "plugin_generate": "插件生成",
    "plugin_render": "插件渲染",
    "plugin_raw": "插件原生",
}


def list_task_names() -> list[str]:
    """本框架会在 AMKR 工作空间中定义的任务名。

    任务名即模型名：调用时把它填进 ``model`` 字段，AMKR 就能按任务定义路由。
    """
    from sirius_pulse.core.model_router import _DEFAULT_TASK_REGISTRY

    return list(_DEFAULT_TASK_REGISTRY)


def build_model_catalog(data_path: Any = None) -> ModelCatalog:
    """构建 WebUI 的模型选择契约。

    ``data_path`` 仅为保持既有调用签名兼容，当前不再参与计算。
    """
    task_names = list_task_names()
    return {
        "available_models": list(task_names),
        "model_choices": [
            {"label": f"{TASK_LABELS.get(name, name)}（{name}）", "value": name}
            for name in task_names
        ],
    }


def format_model_choice_value(provider_name: str, model_id: str) -> str:
    """兼容旧调用点：任务名没有 provider 前缀。"""
    return model_id


def parse_model_choice_value(value: str) -> tuple[str, str] | None:
    """兼容旧调用点：任务名没有 provider 前缀，始终返回 ``None``。"""
    return None


def enrich_model_choices(
    data_path: Any = None,
    model_choices: list[ModelChoice] | None = None,
    provider_models: dict[str, list[str]] | None = None,
    provider_types: dict[str, str] | None = None,
) -> None:
    """兼容旧调用点：任务名不带能力标签，无需补全。"""
    return None
