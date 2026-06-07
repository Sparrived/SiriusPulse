from __future__ import annotations

import json

import pytest

from sirius_pulse.config import (
    Agent,
    AgentPreset,
    ConfigManager,
    OrchestrationPolicy,
    ProviderPolicy,
    SessionConfig,
    SessionDefaults,
    WorkspaceConfig,
)
from sirius_pulse.config.config_builder import (
    ConfigBuilder,
    build_parameters_from_class,
    config_param,
    secret,
)
from sirius_pulse.config.config_helpers import (
    _build_session_defaults,
    _build_workspace_config_from_payload,
    _coerce_bool,
    _coerce_int,
    _coerce_path,
    _coerce_string,
    _dict_to_session_config,
    _normalize_orchestration_defaults,
    _normalize_workspace_config,
    _resolve_env_vars,
    _resolve_values,
    _sanitize_nullable_mapping,
    _validate_config,
)
from sirius_pulse.config.file_io import atomic_json_save
from sirius_pulse.config.helpers import (
    auto_configure_multimodal_agent,
    build_orchestration_policy_from_dict,
    configure_full_orchestration,
    configure_orchestration_models,
    configure_orchestration_retries,
    configure_orchestration_temperatures,
    create_agent_with_multimodal,
    create_multimodel_config,
    setup_multimodel_config,
)
from sirius_pulse.config.jsonc import (
    load_json_document,
    loads_json_document,
    write_session_config_jsonc,
)
from sirius_pulse.utils.layout import WorkspaceLayout


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


def test_config_manager_when_loading_json_then_resolves_env_and_relative_paths(
    tmp_path, monkeypatch
):
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


def test_config_manager_when_workspace_config_is_saved_then_manifest_and_snapshot_round_trip(
    tmp_path,
):
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

    assert (
        policy.resolve_model_for_task("memory_extract", default_model="default") == "memory-model"
    )
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
        {
            "name": "api_key",
            "type": "password",
            "description": "API key",
            "required": True,
            "group": "auth",
        },
        {
            "name": "limit",
            "type": "int",
            "description": "Limit",
            "required": False,
            "default": 3,
            "group": "runtime",
        },
    ]
    assert builder_params == [
        {
            "name": "api_key",
            "type": "password",
            "description": "",
            "required": True,
            "group": "auth",
        },
        {
            "name": "enabled",
            "type": "boolean",
            "description": "",
            "required": False,
            "default": True,
            "group": "auth",
        },
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


def test_config_helpers_when_values_need_coercion_then_defaults_and_nested_cleanup_are_applied(
    tmp_path,
):
    fallback = {
        "keep": "fallback",
        "nested": {"old": 1, "missing": "kept"},
    }
    cleaned = _sanitize_nullable_mapping(
        {
            "keep": None,
            "nested": {"new": 2, "missing": None},
            "items": [1, None, {"drop": None, "value": "ok"}, [None, "tail"]],
        },
        fallback=fallback,
    )

    assert _coerce_int(None, 5) == 5
    assert _coerce_int(True, 5) == 1
    assert _coerce_int("bad", 5) == 5
    assert _coerce_bool("yes", False) is True
    assert _coerce_bool("off", True) is False
    assert _coerce_bool([], True) is False
    assert _coerce_string("  ", "default") == "default"
    assert _coerce_path("", tmp_path) == tmp_path
    assert cleaned == {
        "keep": "fallback",
        "nested": {"old": 1, "missing": "kept", "new": 2},
        "items": [1, {"value": "ok"}, ["tail"]],
    }


def test_config_helpers_when_env_vars_are_resolved_then_nested_values_are_replaced(monkeypatch):
    monkeypatch.setenv("SIRIUS_MODEL", "env-model")

    resolved = _resolve_values(
        {
            "model": "${SIRIUS_MODEL}",
            "missing": "${SIRIUS_MISSING}",
            "nested": ["prefix-${SIRIUS_MODEL}", 3],
        }
    )

    assert _resolve_env_vars("model=${SIRIUS_MODEL}") == "model=env-model"
    assert resolved == {
        "model": "env-model",
        "missing": "${SIRIUS_MISSING}",
        "nested": ["prefix-env-model", 3],
    }


def test_config_helpers_when_workspace_payload_is_built_then_nulls_keep_fallback_and_legacy_fields_are_removed(
    tmp_path,
):
    layout = WorkspaceLayout(tmp_path / "data", config_path=tmp_path / "config", layout_version=7)
    fallback = WorkspaceConfig(
        work_path=tmp_path / "fallback-work",
        data_path=tmp_path / "fallback-data",
        bootstrap_signature="sig",
        active_agent_key="agent-old",
        session_defaults=SessionDefaults(history_max_messages=8, enable_auto_compression=False),
        orchestration_defaults={
            "unified_model": "fallback-model",
            "memory_extract_batch_size": 3,
            "task_budgets": {"old": 1},
        },
        provider_policy=ProviderPolicy(prefer_workspace_registry=False),
    )

    built = _build_workspace_config_from_payload(
        {
            "work_path": tmp_path / "payload-work",
            "data_path": tmp_path / "payload-data",
            "layout_version": "9",
            "bootstrap_signature": "",
            "active_agent_key": "agent-new",
            "session_defaults": {
                "history_max_messages": "12",
                "enable_auto_compression": "true",
            },
            "orchestration_defaults": {
                "unified_model": "payload-model",
                "memory_extract_batch_size": "2",
                "task_budgets": {"drop": 1},
                "split_marker": "drop",
            },
            "provider_policy": {"prefer_workspace_registry": "on"},
        },
        layout=layout,
        fallback=fallback,
    )
    normalized = _normalize_workspace_config(built, layout=layout, fallback=fallback)

    assert built.work_path == tmp_path / "payload-work"
    assert built.data_path == tmp_path / "payload-data"
    assert built.layout_version == 9
    assert built.bootstrap_signature == "sig"
    assert built.session_defaults.history_max_messages == 12
    assert built.session_defaults.history_max_chars == fallback.session_defaults.history_max_chars
    assert built.session_defaults.enable_auto_compression is True
    assert built.orchestration_defaults == {
        "unified_model": "payload-model",
        "memory_extract_batch_size": 2,
    }
    assert built.provider_policy.prefer_workspace_registry is True
    assert normalized.work_path == layout.config_root
    assert normalized.data_path == layout.data_root
    assert normalized.layout_version == layout.layout_version
    assert normalized.active_agent_key == "agent-new"
    assert _normalize_orchestration_defaults({"task_budgets": {"drop": 1}}) == {}


def test_config_helpers_when_session_defaults_payload_is_missing_then_fallback_is_copied():
    fallback = SessionDefaults(
        history_max_messages=4,
        history_max_chars=400,
        max_recent_participant_messages=2,
        enable_auto_compression=False,
    )

    copied = _build_session_defaults(None, fallback)

    assert copied == fallback
    assert copied is not fallback


def test_config_helpers_when_session_config_dict_is_loaded_then_paths_agent_and_orchestration_are_built(
    tmp_path,
):
    payload = {
        "work_path": "work",
        "data_path": "data",
        "agent": {
            "name": "Agent",
            "persona": "Helpful",
            "model": "agent-model",
            "temperature": "0.2",
            "max_tokens": "256",
            "metadata": {"role": "test"},
        },
        "global_system_prompt": "system",
        "orchestration": {"task_models": {"memory_extract": "memory-model"}},
        "history_max_messages": "5",
        "history_max_chars": "500",
        "max_recent_participant_messages": "3",
        "enable_auto_compression": False,
    }

    _validate_config(payload)
    config = _dict_to_session_config(payload, tmp_path)

    assert config.work_path == tmp_path / "work"
    assert config.data_path == tmp_path / "data"
    assert config.agent.name == "Agent"
    assert config.agent.temperature == 0.2
    assert config.agent.max_tokens == 256
    assert config.global_system_prompt == "system"
    assert config.orchestration.task_models == {"memory_extract": "memory-model"}
    assert config.history_max_messages == 5
    assert config.enable_auto_compression is False
    with pytest.raises(ValueError):
        _validate_config({"work_path": "work", "agent": {}})


def test_config_helpers_when_multimodal_agent_helpers_are_used_then_metadata_is_set_explicitly():
    agent = Agent(name="Agent", persona="Helpful", model="text-model", metadata={})

    assert auto_configure_multimodal_agent(agent).metadata == {}
    assert auto_configure_multimodal_agent(agent, multimodal_model="vision-model") is agent
    assert agent.metadata["multimodal_model"] == "vision-model"
    assert auto_configure_multimodal_agent(agent).metadata["multimodal_model"] == "vision-model"

    created = create_agent_with_multimodal(
        name="Agent2",
        persona="Helpful",
        model="text-model",
        multimodal_model="vision-model",
        temperature=0.1,
        max_tokens=42,
        provider="test",
    )

    assert created.metadata == {"multimodal_model": "vision-model", "provider": "test"}
    assert created.temperature == 0.1
    assert created.max_tokens == 42


def test_config_helpers_when_orchestration_shortcuts_are_used_then_session_config_is_replaced(
    tmp_path,
):
    config = SessionConfig(
        work_path=tmp_path,
        preset=AgentPreset(
            agent=Agent(name="Agent", persona="Helpful", model="base-model"),
            global_system_prompt="system",
        ),
        orchestration=OrchestrationPolicy(unified_model="base-model"),
    )

    model_config = create_multimodel_config(
        task_models={"memory_extract": "memory-model"},
        task_temperatures={"memory_extract": 0.1},
        task_max_tokens={"memory_extract": 128},
        task_retries={"memory_extract": 2},
        max_multimodal_inputs_per_turn=2,
        max_multimodal_value_length=99,
    )
    setup = setup_multimodel_config(
        session_config=config,
        task_models={"response_generate": "response-model"},
        task_temperatures={"response_generate": 0.8},
        task_max_tokens={"response_generate": 512},
        task_retries={"response_generate": 1},
    )
    with_models = configure_orchestration_models(setup, memory_extract="memory-model")
    with_temps = configure_orchestration_temperatures(with_models, memory_extract=0.2)
    with_retries = configure_orchestration_retries(with_temps, memory_extract=3)
    full = configure_full_orchestration(
        with_retries,
        task_models={"event_extract": "event-model"},
        task_temperatures={"event_extract": 0.3},
        task_retries={"event_extract": 4},
        pending_message_threshold=0,
    )

    assert model_config.to_dict()["task_models"] == {"memory_extract": "memory-model"}
    assert model_config.to_orchestration_policy().max_multimodal_inputs_per_turn == 2
    assert setup is config
    assert setup.orchestration.task_models == {"response_generate": "response-model"}
    assert with_models is not setup
    assert with_models.orchestration.unified_model == ""
    assert with_models.orchestration.task_models["memory_extract"] == "memory-model"
    assert with_temps.orchestration.task_temperatures["memory_extract"] == 0.2
    assert with_retries.orchestration.task_retries["memory_extract"] == 3
    assert full.orchestration.task_models["event_extract"] == "event-model"
    assert full.orchestration.task_temperatures["event_extract"] == 0.3
    assert full.orchestration.task_retries["event_extract"] == 4
    assert full.orchestration.pending_message_threshold == 0
