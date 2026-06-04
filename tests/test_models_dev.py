from __future__ import annotations

from types import SimpleNamespace

from sirius_pulse.providers.models_dev import (
    ModelCost,
    ModelFilter,
    ModelsDevCache,
    auto_fill_models_from_dev,
    estimate_cost,
    filter_models,
    get_context_length,
    get_model_info,
    get_models_dev_provider_ids,
    get_provider_models,
    list_provider_model_details,
    list_provider_model_ids,
    parse_model_cost,
)
from sirius_pulse.utils.json_io import atomic_write_json


def _models_data() -> dict:
    return {
        "openai": {
            "models": {
                "gpt-tool": {
                    "name": "GPT Tool",
                    "tool_call": True,
                    "reasoning": False,
                    "structured_output": True,
                    "modalities": {"input": ["text", "image"]},
                    "limit": {"context": 128_000},
                    "cost": {"input": 1.0, "output": 2.0},
                    "open_weights": False,
                },
                "gpt-text": {
                    "name": "GPT Text",
                    "tool_call": False,
                    "reasoning": True,
                    "modalities": {"input": ["text"]},
                    "limit": {"context": 16_000},
                    "cost": {"input": 0.2, "output": 0.6},
                    "open_weights": True,
                },
            }
        },
        "anthropic": {
            "models": {
                "claude-tool": {
                    "tool_call": True,
                    "modalities": {"input": ["text"]},
                    "limit": {"context": 200_000},
                    "cost": {"input": 3.0, "output": 15.0},
                },
                "gpt-tool": {
                    "tool_call": True,
                    "modalities": {"input": ["text"]},
                    "limit": {"context": 64_000},
                    "cost": {"input": 9.0, "output": 9.0},
                },
            }
        },
        "deepseek": {"models": {"deepseek-chat": {"tool_call": True}}},
        "broken": {"models": []},
    }


def test_models_dev_when_provider_mapping_is_requested_then_known_ids_are_returned():
    assert "openai" in get_models_dev_provider_ids("openai-compatible")
    assert get_models_dev_provider_ids("unknown") == []


def test_models_dev_when_provider_or_model_is_missing_then_safe_empty_values_are_returned():
    data = _models_data()

    assert get_provider_models(data, "broken") == {}
    assert get_provider_models(data, "missing") == {}
    assert get_model_info(data, "openai", "gpt-tool")["name"] == "GPT Tool"
    assert get_model_info(data, "openai", "missing") is None
    assert get_context_length({"limit": {"context": 123}}) == 123
    assert get_context_length({"limit": {"context": "bad"}}) == 0


def test_models_dev_when_cost_has_tiers_then_context_selects_matching_price():
    model = {
        "cost": {
            "input": 1.0,
            "output": 2.0,
            "cache_read": "0.1",
            "tiers": [
                {
                    "tier": {"type": "context", "size": 1000},
                    "input": 4.0,
                    "output": 8.0,
                    "cache_read": "0.5",
                    "cache_write": "0.7",
                }
            ],
        }
    }

    base = parse_model_cost(model, context_tokens=1000)
    tier = parse_model_cost(model, context_tokens=1001)

    assert base == ModelCost(input_per_m=1.0, output_per_m=2.0, cache_read_per_m=0.1)
    assert tier == ModelCost(input_per_m=4.0, output_per_m=8.0, cache_read_per_m=0.5, cache_write_per_m=0.7)
    assert parse_model_cost({}) == ModelCost(input_per_m=0.0, output_per_m=0.0)
    assert estimate_cost(tier, input_tokens=1_000_000, output_tokens=500_000) == 8.0


def test_models_dev_when_filtering_models_then_capabilities_and_cost_are_applied():
    data = _models_data()

    results = filter_models(
        data,
        "openai",
        ModelFilter(tool_call=True, vision=True, min_context=100_000, max_input_cost=2.0),
    )
    open_weights = filter_models(data, "openai", ModelFilter(open_weights_only=True))

    assert [row["id"] for row in results] == ["gpt-tool"]
    assert [row["id"] for row in open_weights] == ["gpt-text"]


def test_models_dev_when_listing_provider_models_then_deduplicates_and_sorts_ids_and_details():
    data = _models_data()

    ids = list_provider_model_ids(data, "openai-compatible", tool_call_only=True)
    details = list_provider_model_details(data, "openai-compatible")

    assert ids == ["claude-tool", "gpt-tool"]
    assert [row["id"] for row in details] == ["claude-tool", "gpt-text", "gpt-tool"]
    assert next(row for row in details if row["id"] == "gpt-tool") == {
        "id": "gpt-tool",
        "name": "GPT Tool",
        "tool_call": True,
        "reasoning": False,
        "structured_output": True,
        "vision": True,
        "audio": False,
        "context": 128_000,
        "input_cost": 1.0,
        "output_cost": 2.0,
    }


def test_models_dev_cache_when_disk_cache_exists_then_uses_it_without_network(tmp_path, monkeypatch):
    data = _models_data()
    atomic_write_json(tmp_path / "models_dev_cache.json", data)
    cache = ModelsDevCache(tmp_path)

    monkeypatch.setattr(cache, "_fetch_from_network", lambda: {"network": {}})

    assert cache.get() == data
    assert cache.get(force_refresh=True) == {"network": {}}


def test_models_dev_cache_when_network_fails_then_returns_expired_memory_cache(tmp_path, monkeypatch):
    cache = ModelsDevCache(tmp_path, ttl=0)
    cache._memory_cache = {"old": {}}
    cache._cache_time = 0
    monkeypatch.setattr(cache, "_fetch_from_network", lambda: None)

    assert cache.get(force_refresh=True) == {"old": {}}


def test_models_dev_when_auto_fill_runs_then_only_empty_provider_model_lists_are_changed(tmp_path, monkeypatch):
    data = _models_data()

    class FakeCache:
        def __init__(self, config_root):
            self.config_root = config_root

        def get(self):
            return data

    monkeypatch.setattr("sirius_pulse.providers.models_dev.ModelsDevCache", FakeCache)
    providers = {
        "openai-compatible": SimpleNamespace(models=[]),
        "deepseek": SimpleNamespace(models=["already-set"]),
        "unknown": SimpleNamespace(models=[]),
    }

    assert auto_fill_models_from_dev(tmp_path, providers, tool_call_only=True) is True
    assert providers["openai-compatible"].models == ["claude-tool", "gpt-tool"]
    assert providers["deepseek"].models == ["already-set"]
    assert providers["unknown"].models == []
