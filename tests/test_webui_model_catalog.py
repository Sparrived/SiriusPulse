from __future__ import annotations

from sirius_pulse.core.model_router import _DEFAULT_TASK_REGISTRY
from sirius_pulse.webui.model_catalog import (
    TASK_LABELS,
    build_model_catalog,
    enrich_model_choices,
    format_model_choice_value,
    parse_model_choice_value,
)


def test_model_catalog_when_built_then_lists_every_registered_task_name():
    """编排页的候选值就是 AMKR 任务名，必须覆盖全部已注册任务。"""
    catalog = build_model_catalog()

    assert catalog["available_models"] == list(_DEFAULT_TASK_REGISTRY)
    assert [choice["value"] for choice in catalog["model_choices"]] == list(_DEFAULT_TASK_REGISTRY)
    # 任务名不是 provider/model 复合形式
    assert all("/" not in value for value in catalog["available_models"])


def test_model_catalog_when_task_has_label_then_label_contains_chinese_and_task_name():
    catalog = build_model_catalog()
    by_value = {choice["value"]: choice["label"] for choice in catalog["model_choices"]}

    assert by_value["cognition_analyze"] == f"{TASK_LABELS['cognition_analyze']}（cognition_analyze）"


def test_model_catalog_when_task_unlabelled_then_falls_back_to_task_name():
    """未登记中文说明的任务名不应丢失或显示为空。"""
    catalog = build_model_catalog()
    values = {choice["value"] for choice in catalog["model_choices"]}

    # 注册表里的任务都应在候选中（无论是否有中文标签）
    assert values == set(_DEFAULT_TASK_REGISTRY)


def test_model_catalog_when_legacy_helpers_called_then_task_names_have_no_provider_prefix():
    """旧调用点仍可用：任务名没有 provider 前缀，解析应返回 None。"""
    assert format_model_choice_value("deepseek", "deepseek-chat") == "deepseek-chat"
    assert parse_model_choice_value("deepseek/deepseek-chat") is None

    choices = [{"label": "x", "value": "cognition_analyze"}]
    enrich_model_choices(None, choices)

    assert choices == [{"label": "x", "value": "cognition_analyze"}]
