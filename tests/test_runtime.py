from __future__ import annotations

from sirius_pulse.platforms.runtime import EngineRuntime
from sirius_pulse.persona_config import PersonaExperienceConfig
from sirius_pulse.persona_worker import PersonaWorker
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


def test_persona_worker_passes_main_model_reply_cooldown_to_runtime_config(tmp_path):
    worker = PersonaWorker("sirius", tmp_path)
    experience = PersonaExperienceConfig(main_model_reply_cooldown_seconds=7.5)

    plugin_config = worker._build_plugin_config(experience)

    assert plugin_config["main_model_reply_cooldown_seconds"] == 7.5


def test_engine_runtime_includes_main_model_reply_cooldown_in_engine_config(tmp_path):
    runtime = EngineRuntime(
        tmp_path,
        plugin_config={"main_model_reply_cooldown_seconds": 7.5},
    )

    config = runtime._build_engine_runtime_config(PersonaExperienceConfig())

    assert config["main_model_reply_cooldown_seconds"] == 7.5
