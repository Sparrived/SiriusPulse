from __future__ import annotations

import os
from types import SimpleNamespace

from sirius_pulse.persona_config import PersonaExperienceConfig
from sirius_pulse.persona_worker import PersonaWorker
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


def test_persona_worker_passes_main_model_reply_cooldown_to_runtime_config(tmp_path):
    worker = PersonaWorker("sirius", tmp_path)
    experience = PersonaExperienceConfig(
        main_model_reply_cooldown_seconds=7.5,
        diary_top_k=7,
        diary_token_budget=900,
        memory_unit_top_k=4,
    )

    plugin_config = worker._build_plugin_config(experience)

    assert plugin_config["main_model_reply_cooldown_seconds"] == 7.5
    assert plugin_config["diary_top_k"] == 7
    assert plugin_config["diary_token_budget"] == 900
    assert plugin_config["memory_unit_top_k"] == 4


def test_persona_worker_experience_reload_updates_runtime_config_keys(tmp_path):
    worker = PersonaWorker(tmp_path)
    tmp_path.mkdir(exist_ok=True)
    PersonaExperienceConfig(
        engagement_sensitivity=0.8,
        min_reply_interval_seconds=13,
        max_sentence_chars=31,
        diary_top_k=6,
        diary_token_budget=700,
        memory_unit_top_k=2,
    ).save(tmp_path / "experience.json")

    class Brain:
        config = {}

    class Engine:
        config = {}
        brain = Brain()

    engine = Engine()

    worker._reload_experience(engine)

    assert engine.config["sensitivity"] == 0.8
    assert engine.config["reply_cooldown_seconds"] == 13
    assert engine.config["max_sentence_chars"] == 31
    assert engine.config["diary_top_k"] == 6
    assert engine.config["diary_token_budget"] == 700
    assert engine.config["memory_unit_top_k"] == 2
    assert "engagement_sensitivity" not in engine.config
    assert engine.brain.config["memory_unit_top_k"] == 2


def test_persona_worker_config_reload_consumes_experience_flag(tmp_path):
    worker = PersonaWorker(tmp_path)
    PersonaExperienceConfig(memory_unit_top_k=15).save(tmp_path / "experience.json")
    engine = SimpleNamespace(config={}, brain=SimpleNamespace(config={}))
    worker._runtime = SimpleNamespace(engine=engine)
    flag = tmp_path / "engine_state" / "reload_requested"
    flag.parent.mkdir()
    flag.write_text("experience", encoding="utf-8")
    os.utime(flag, (0, 0))

    worker._check_config_reload()

    assert not flag.exists()
    assert engine.config["memory_unit_top_k"] == 15
    assert engine.brain.config["memory_unit_top_k"] == 15


def test_engine_runtime_includes_main_model_reply_cooldown_in_engine_config(tmp_path):
    runtime = EngineRuntime(
        tmp_path,
        plugin_config={"main_model_reply_cooldown_seconds": 7.5},
    )

    config = runtime._build_engine_runtime_config(PersonaExperienceConfig())

    assert config["main_model_reply_cooldown_seconds"] == 7.5
