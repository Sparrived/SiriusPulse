from __future__ import annotations

import json

import pytest

from sirius_pulse.config import (
    ConfigManager,
    OrchestrationPolicy,
    ProviderPolicy,
    SessionDefaults,
    WorkspaceConfig,
)
from sirius_pulse.config.config_builder import ConfigBuilder, build_parameters_from_class, config_param, secret
from sirius_pulse.config.file_io import atomic_json_save
from sirius_pulse.config.helpers import build_orchestration_policy_from_dict
from sirius_pulse.config.jsonc import load_json_document, loads_json_document, write_session_config_jsonc


def test_jsonc_when_comments_are_present_then_document_is_loaded():
    payload = loads_json_document(
        """
        {
          // Outside comments are ignored.
          "url": "https://example.test/path//inside-string",
          /* block comments are ignored too */
          "nested": {"value": 3}
        }
        """
    )

    assert payload == {
        "url": "https://example.test/path//inside-string",
        "nested": {"value": 3},
    }


def test_session_config_jsonc_when_written_then_round_trips_payload(tmp_path):
    target = tmp_path / "config" / "session_config.json"
    payload = {
        "generated_agent_key": "agent-alpha",
        "history_max_messages": 12,
        "orchestration": {"unified_model": "model-a"},
    }

    write_session_config_jsonc(target, payload)

    assert target.read_text(encoding="utf-8").startswith("// Sirius Chat session config.")
    assert load_json_document(target) == payload


def test_config_manager_when_loading_json_then_resolves_env_and_relative_paths(tmp_path, monkeypatch):
    monkeypatch.setenv("SIRIUS_TEST_MODEL", "env-model")
    config_path = tmp_path / "configs" / "session.json"
    config_path.parent.mkdir()
    config_path.write_text(
        json.dumps(
            {
                "work_path": "workspace",
                "data_path": "runtime",
                "agent": {
                    "name": "Tester",
                    "persona": "helpful",
                    "model": "${SIRIUS_TEST_MODEL}",
                    "temperature": 0.3,
                    "max_tokens": 128,
                },
                "global_system_prompt": "system",
                "orchestration": {
                    "unified_model": "${SIRIUS_TEST_MODEL}",
                    "memory_extract_batch_size": 2,
                },
            }
        ),
        encoding="utf-8",
    )

    config = ConfigManager().load_from_json(config_path)

    assert config.work_path == config_path.parent / "workspace"
    assert config.data_path == config_path.parent / "runtime"
    assert config.agent.model == "env-model"
    assert config.orchestration.unified_model == "env-model"
    assert config.orchestration.memory_extract_batch_size == 2


def test_config_manager_when_workspace_config_is_saved_then_manifest_and_snapshot_round_trip(tmp_path):
    manager = ConfigManager()
    work_path = tmp_path / "workspace"
    data_path = tmp_path / "runtime"
    config = WorkspaceConfig(
        work_path=work_path,
        data_path=data_path,
        active_agent_key="agent-alpha",
        session_defaults=SessionDefaults(
            history_max_messages=9,
            history_max_chars=1234,
            max_recent_participant_messages=2,
            enable_auto_compression=False,
        ),
        orchestration_defaults={"unified_model": "workspace-model", "memory_extract_batch_size": 2},
        provider_policy=ProviderPolicy(prefer_workspace_registry=False),
    )

    manager.save_workspace_config(work_path, config, data_path=data_path)
    reloaded = manager.load_workspace_config(work_path, data_path=data_path)

    assert reloaded.work_path == work_path
    assert reloaded.data_path == data_path
    assert reloaded.active_agent_key == "agent-alpha"
    assert reloaded.session_defaults.history_max_messages == 9
    assert reloaded.session_defaults.enable_auto_compression is False
    assert reloaded.orchestration_defaults["unified_model"] == "workspace-model"
    assert reloaded.orchestration_defaults["memory_extract_batch_size"] == 2
    assert reloaded.provider_policy.prefer_workspace_registry is False
    manifest = json.loads((work_path / "workspace.json").read_text(encoding="utf-8"))
    assert manifest["provider_policy"] == {"prefer_workspace_registry": False}


def test_models_when_orchestration_policy_resolves_models_then_validates_modes():
    policy = build_orchestration_policy_from_dict(
        {
            "task_models": {"memory_extract": "memory-model"},
            "task_enabled": {"memory_extract": False},
            "memory": {"max_facts_per_user": 3, "decay_schedule": {"7": 0.1}},
        },
        agent_model="fallback-model",
    )

    assert policy.resolve_model_for_task("memory_extract", default_model="default") == "memory-model"
    assert policy.resolve_model_for_task("response_generate", default_model="default") == "default"
    assert policy.is_task_enabled("memory_extract") is False
    assert policy.is_task_enabled("unknown_task") is True
    assert policy.memory.max_facts_per_user == 3
    assert policy.memory.decay_schedule[7] == 0.1
    with pytest.raises(ValueError):
        OrchestrationPolicy().validate()
    with pytest.raises(ValueError):
        OrchestrationPolicy(unified_model="one", task_models={"memory_extract": "two"}).validate()


def test_config_builder_when_params_are_declared_then_metadata_is_rendered():
    class DemoConfig:
        api_key = secret("API key", required=True, group="auth")
        limit = config_param("Limit", type=int, default=3, group="runtime")

    class_params = build_parameters_from_class(DemoConfig)
    builder_params = (
        ConfigBuilder()
        .group("auth")
        .add("api_key", type="password", required=True)
        .add("enabled", type=bool, default=True)
        .build()
    )

    assert class_params == [
        {"name": "api_key", "type": "password", "description": "API key", "required": True, "group": "auth"},
        {"name": "limit", "type": "int", "description": "Limit", "required": False, "default": 3, "group": "runtime"},
    ]
    assert builder_params == [
        {"name": "api_key", "type": "password", "description": "", "required": True, "group": "auth"},
        {"name": "enabled", "type": "boolean", "description": "", "required": False, "default": True, "group": "auth"},
    ]


def test_atomic_json_save_when_target_parent_is_missing_then_writes_without_tmp_file(tmp_path):
    target = tmp_path / "nested" / "config.json"

    atomic_json_save(target, {"name": "alpha", "enabled": True})

    assert json.loads(target.read_text(encoding="utf-8")) == {"name": "alpha", "enabled": True}
    assert not target.with_suffix(".json.tmp").exists()


def test_workspace_config_when_serialized_then_paths_and_nested_defaults_round_trip(tmp_path):
    config = WorkspaceConfig(
        work_path=tmp_path / "work",
        data_path=tmp_path / "data",
        layout_version=4,
        bootstrap_signature="sig",
        active_agent_key="agent-alpha",
        session_defaults=SessionDefaults(history_max_messages=7, enable_auto_compression=False),
        orchestration_defaults={"unified_model": "model-a"},
        provider_policy=ProviderPolicy(prefer_workspace_registry=False),
    )

    restored = WorkspaceConfig.from_dict(config.to_dict())

    assert restored.work_path == tmp_path / "work"
    assert restored.data_path == tmp_path / "data"
    assert restored.layout_version == 4
    assert restored.bootstrap_signature == "sig"
    assert restored.active_agent_key == "agent-alpha"
    assert restored.session_defaults.history_max_messages == 7
    assert restored.session_defaults.enable_auto_compression is False
    assert restored.orchestration_defaults == {"unified_model": "model-a"}
    assert restored.provider_policy.prefer_workspace_registry is False
