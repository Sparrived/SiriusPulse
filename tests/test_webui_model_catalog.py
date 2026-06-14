from __future__ import annotations

from sirius_pulse.utils.json_io import atomic_write_json
from sirius_pulse.webui.model_catalog import build_model_catalog, enrich_model_choices


def test_webui_model_catalog_when_building_then_uses_shared_contract_and_provider_tags(tmp_path):
    atomic_write_json(
        tmp_path / "providers" / "provider_keys.json",
        {
            "providers": {
                "openai-compatible": {
                    "type": "openai-compatible",
                    "api_key": "sk-openai",
                    "enabled": True,
                    "models": ["shared-model", "openai-only"],
                },
                "deepseek": {
                    "type": "deepseek",
                    "api_key": "sk-deepseek",
                    "enabled": True,
                    "models": ["shared-model", "deepseek-chat"],
                },
                "siliconflow": {
                    "type": "siliconflow",
                    "api_key": "sk-siliconflow",
                    "enabled": False,
                    "models": ["disabled-model"],
                },
            }
        },
    )
    atomic_write_json(
        tmp_path / "models_dev_cache.json",
        {
            "openai": {
                "models": {
                    "shared-model": {
                        "tool_call": True,
                        "structured_output": True,
                        "modalities": {"input": ["text", "image"]},
                    },
                    "openai-only": {"reasoning": True},
                }
            },
            "deepseek": {
                "models": {
                    "shared-model": {"reasoning": True, "modalities": {"input": ["audio"]}},
                    "deepseek-chat": {"tool_call": True},
                }
            },
            "siliconflow": {"models": {"disabled-model": {"tool_call": True}}},
        },
    )

    catalog = build_model_catalog(tmp_path)

    assert catalog["available_models"] == ["shared-model", "openai-only", "deepseek-chat"]
    assert catalog["model_choices"] == [
        {
            "label": "openai-compatible/shared-model",
            "value": "openai-compatible/shared-model",
            "tags": ["函数调用", "结构化", "视觉"],
        },
        {
            "label": "openai-compatible/openai-only",
            "value": "openai-compatible/openai-only",
            "tags": ["推理"],
        },
        {
            "label": "deepseek/shared-model",
            "value": "deepseek/shared-model",
            "tags": ["推理", "音频"],
        },
        {
            "label": "deepseek/deepseek-chat",
            "value": "deepseek/deepseek-chat",
            "tags": ["函数调用"],
        },
    ]


def test_webui_model_catalog_when_models_dev_is_unavailable_then_choices_still_return(
    tmp_path, monkeypatch
):
    atomic_write_json(
        tmp_path / "providers" / "provider_keys.json",
        {
            "providers": {
                "deepseek": {
                    "type": "deepseek",
                    "api_key": "sk-deepseek",
                    "enabled": True,
                    "models": ["deepseek-chat"],
                }
            }
        },
    )

    monkeypatch.setattr("sirius_pulse.webui.model_catalog.ModelsDevCache.get", lambda self: None)

    catalog = build_model_catalog(tmp_path)

    assert catalog == {
        "available_models": ["deepseek-chat"],
        "model_choices": [{"label": "deepseek/deepseek-chat", "value": "deepseek/deepseek-chat"}],
    }


def test_webui_model_catalog_when_enriching_legacy_values_then_ignores_unscoped_choices(tmp_path):
    atomic_write_json(
        tmp_path / "models_dev_cache.json",
        {"deepseek": {"models": {"deepseek-chat": {"tool_call": True}}}},
    )
    choices = [
        {"label": "deepseek/deepseek-chat", "value": "deepseek/deepseek-chat"},
        {"label": "legacy", "value": "legacy"},
    ]

    enrich_model_choices(tmp_path, choices)

    assert choices == [
        {"label": "deepseek/deepseek-chat", "value": "deepseek/deepseek-chat", "tags": ["函数调用"]},
        {"label": "legacy", "value": "legacy"},
    ]
