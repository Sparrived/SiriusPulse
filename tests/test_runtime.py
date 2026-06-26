from __future__ import annotations

from sirius_pulse.platforms.runtime import EngineRuntime
from sirius_pulse.utils.json_io import atomic_write_json


def test_engine_runtime_when_work_path_is_persona_dir_then_loads_global_providers(tmp_path):
    data_dir = tmp_path / "data"
    persona_dir = data_dir / "personas" / "sirius"
    persona_dir.mkdir(parents=True)
    atomic_write_json(
        data_dir / "providers" / "provider_keys.json",
        {
            "providers": {
                "deepseek": {
                    "type": "deepseek",
                    "api_key": "sk-test",
                    "enabled": True,
                    "models": ["deepseek-chat"],
                }
            }
        },
    )

    runtime = EngineRuntime(persona_dir)

    assert runtime.global_data_path == data_dir
    assert runtime.has_provider_config() is True
