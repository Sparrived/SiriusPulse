"""Tests for ConfigManager."""

from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from unittest import mock

import pytest

from sirius_pulse.config import ConfigManager, SessionConfig
from sirius_pulse.config.jsonc import load_json_document, write_session_config_jsonc
from sirius_pulse.utils.layout import WorkspaceLayout


class TestConfigManager:
    """Test ConfigManager functionality."""

    @pytest.fixture
    def config_manager(self) -> ConfigManager:
        """Create a ConfigManager instance."""
        return ConfigManager()

    @pytest.fixture
    def temp_config_file(self) -> Path:
        """Create a temporary config file."""
        config_dict = {
            "work_path": "/tmp/test_data",
            "global_system_prompt": "Test prompt",
            "agent": {
                "name": "TestAgent",
                "persona": "Test persona",
                "model": "test-model",
                "temperature": 0.7,
                "max_tokens": 256,
            },
            "orchestration": {
                "task_enabled": {
                    "memory_extract": True,
                },
                "task_models": {},
            },
        }
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".json", delete=False
        ) as f:
            json.dump(config_dict, f)
            return Path(f.name)

    def test_load_from_json(self, config_manager: ConfigManager, temp_config_file: Path) -> None:
        """Test loading config from JSON file."""
        session_config = config_manager.load_from_json(temp_config_file)
        assert isinstance(session_config, SessionConfig)
        assert session_config.agent.name == "TestAgent"
        # Check that work_path ends with the expected path (platform-independent)
        assert session_config.work_path.parts[-2:] == ("tmp", "test_data")

    def test_load_from_json_accepts_jsonc_comments(self, config_manager: ConfigManager, tmp_path: Path) -> None:
        """Test loading config from a JSONC file with comments."""
        config_path = tmp_path / "commented.json"
        config_path.write_text(
            '{\n  // config root\n  "work_path": "./data",\n  "global_system_prompt": "Test prompt",\n  "agent": {\n    "name": "CommentedAgent",\n    "persona": "Test persona",\n    "model": "test-model"\n  },\n  "orchestration": {}\n}\n',
            encoding="utf-8",
        )

        session_config = config_manager.load_from_json(config_path)
        assert session_config.agent.name == "CommentedAgent"
        assert session_config.work_path == tmp_path / "data"

    def test_load_from_json_file_not_found(self, config_manager: ConfigManager) -> None:
        """Test handling of missing config file."""
        with pytest.raises(FileNotFoundError):
            config_manager.load_from_json("/nonexistent/path.json")

    def test_resolve_env_vars(self, config_manager: ConfigManager) -> None:
        """Test environment variable substitution."""
        os.environ["TEST_VAR"] = "test_value"
        result = config_manager._resolve_env_vars("prefix_${TEST_VAR}_suffix")
        assert result == "prefix_test_value_suffix"

    def test_resolve_env_vars_with_default(self, config_manager: ConfigManager) -> None:
        """Test environment variable substitution with missing var."""
        result = config_manager._resolve_env_vars("${NONEXISTENT_VAR}")
        assert result == "${NONEXISTENT_VAR}"

    def test_merge_configs(self, config_manager: ConfigManager) -> None:
        """Test configuration merging."""
        base = {"a": 1, "b": {"c": 2}}
        override = {"b": {"d": 3}, "e": 4}
        result = config_manager.merge_configs(base, override)
        assert result == {"a": 1, "b": {"c": 2, "d": 3}, "e": 4}

    def test_validate_config_missing_keys(self, config_manager: ConfigManager) -> None:
        """Test validation of missing required keys."""
        config = {"agent": {"name": "test"}}
        with pytest.raises(ValueError, match="缺少必要配置键|Missing required config keys"):
            config_manager._validate_config(config)

    def test_validate_config_missing_agent_keys(self, config_manager: ConfigManager) -> None:
        """Test validation of missing agent keys."""
        config = {
            "work_path": "/tmp",
            "agent": {"name": "test"},
            "orchestration": {},
        }
        with pytest.raises(ValueError, match="缺少必要的主角配置键|Missing required agent keys"):
            config_manager._validate_config(config)

    def test_validate_config_invalid_work_path(self, config_manager: ConfigManager) -> None:
        """Test validation of invalid work_path."""
        config = {
            "work_path": {"invalid": "path"},
            "agent": {"name": "test", "persona": "p", "model": "m"},
            "orchestration": {},
        }
        with pytest.raises(ValueError, match="无效的 work_path|Invalid work_path"):
            config_manager._validate_config(config)

    def test_dict_to_session_config(self, config_manager: ConfigManager) -> None:
        """Test conversion from dict to SessionConfig."""
        config_dict = {
            "work_path": "/tmp/test",
            "global_system_prompt": "Test prompt",
            "agent": {
                "name": "TestAgent",
                "persona": "Test persona",
                "model": "test-model",
                "temperature": 0.8,
                "max_tokens": 256,
                "metadata": {"key": "value"},
            },
            "history_max_messages": 32,
            "history_max_chars": 8000,
            "enable_auto_compression": False,
            "orchestration": {
                "enabled": False,
                "task_models": {},
            },
        }
        session_config = config_manager._dict_to_session_config(config_dict, Path("/tmp"))
        assert session_config.agent.name == "TestAgent"
        assert session_config.history_max_messages == 32
        # 当没有指定 unified_model 或 task_models 时，应使用 agent 的模型作为默认
        assert session_config.orchestration.unified_model == "test-model"
        assert session_config.agent.metadata == {"key": "value"}

    def test_dict_to_session_config_parses_intent_analysis_task_settings(self, config_manager: ConfigManager) -> None:
        """Test ConfigManager parses intent_analysis task settings from JSON."""
        config_dict = {
            "work_path": "/tmp/test",
            "global_system_prompt": "Test prompt",
            "agent": {
                "name": "TestAgent",
                "persona": "Test persona",
                "model": "test-model",
            },
            "orchestration": {
                "task_enabled": {
                    "memory_extract": False,
                    "cognition_analyze": True,
                },
                "task_models": {
                    "cognition_analyze": "cognition-model",
                },
                "task_max_tokens": {
                    "cognition_analyze": 192,
                },
                "session_reply_mode": "auto",
                "pending_message_threshold": 0,
            },
        }

        session_config = config_manager._dict_to_session_config(config_dict, Path("/tmp"))

        assert session_config.orchestration.task_enabled["cognition_analyze"] is True
        assert session_config.orchestration.task_models["cognition_analyze"] == "cognition-model"
        assert session_config.orchestration.task_max_tokens["cognition_analyze"] == 192
        assert session_config.orchestration.session_reply_mode == "auto"
        assert session_config.orchestration.pending_message_threshold == 0

    def test_resolve_values_nested(self, config_manager: ConfigManager) -> None:
        """Test recursive environment variable resolution."""
        os.environ["TEST_MODEL"] = "test-model-value"
        obj = {
            "nested": {
                "model": "${TEST_MODEL}",
                "list": ["${TEST_MODEL}", "static"],
            }
        }
        result = config_manager._resolve_values(obj)
        assert result["nested"]["model"] == "test-model-value"
        assert result["nested"]["list"] == ["test-model-value", "static"]

    def test_relative_path_resolution(self, config_manager: ConfigManager, temp_config_file: Path) -> None:
        """Test loading config with relative path."""
        # Create a config in a known location relative to base_path
        temp_dir = temp_config_file.parent
        relative_config = temp_dir / "relative_config.json"
        
        config_dict = {
            "work_path": "./data",
            "global_system_prompt": "Test",
            "agent": {
                "name": "TestAgent",
                "persona": "Test",
                "model": "test-model",
            },
            "orchestration": {
                "task_enabled": {
                    "memory_extract": True,
                }
            },
        }
        with open(relative_config, "w") as f:
            json.dump(config_dict, f)
        
        manager = ConfigManager(temp_dir)
        session_config = manager.load_from_json("relative_config.json")
        assert session_config.agent.name == "TestAgent"
        assert session_config.work_path == temp_dir / "data"

    def test_save_workspace_config_writes_commented_session_snapshot(self, tmp_path: Path) -> None:
        """Test workspace session snapshot is persisted as commented JSONC."""
        manager = ConfigManager(base_path=tmp_path)
        workspace_config = manager.load_workspace_config(tmp_path)

        manager.save_workspace_config(tmp_path, workspace_config)

        snapshot_path = WorkspaceLayout(tmp_path).session_config_path()
        content = snapshot_path.read_text(encoding="utf-8")
        assert "//" in content
        assert '"history_max_messages"' in content
        assert '"intent_analysis_model"' not in content
        assert '"task_enabled"' in content
        assert '"pending_message_threshold"' in content
        assert '"max_concurrent_llm_calls"' in content

        payload = load_json_document(snapshot_path)
        assert payload["orchestration"]["task_enabled"]["cognition_analyze"] is True
        assert payload["orchestration"]["pending_message_threshold"] == 4

    def test_load_workspace_config_prefers_session_snapshot_orchestration_even_if_manifest_newer(
        self,
        tmp_path: Path,
    ) -> None:
        manager = ConfigManager(base_path=tmp_path)
        workspace_config = manager.load_workspace_config(tmp_path)
        workspace_config.active_agent_key = "main_agent"
        workspace_config.orchestration_defaults = {
            "task_models": {"memory_extract": "qwen3.5-plus"},
            "task_enabled": {"memory_extract": True},
        }
        manager.save_workspace_config(tmp_path, workspace_config)

        layout = WorkspaceLayout(tmp_path)
        snapshot_path = layout.session_config_path()
        snapshot_payload = load_json_document(snapshot_path)
        snapshot_payload["generated_agent_key"] = "main_agent"
        snapshot_payload["orchestration"]["task_models"] = {
            "memory_extract": "deepseek-chat",
            "cognition_analyze": "deepseek-chat",
        }
        write_session_config_jsonc(snapshot_path, snapshot_payload)

        manifest_path = layout.workspace_manifest_path()
        newer = time.time() + 5
        os.utime(manifest_path, (newer, newer))

        loaded = manager.load_workspace_config(tmp_path)

        assert loaded.orchestration_defaults["task_models"]["memory_extract"] == "deepseek-chat"
        assert loaded.orchestration_defaults["task_models"]["cognition_analyze"] == "deepseek-chat"

    def test_save_workspace_config_preserves_existing_values_when_new_payload_contains_nulls(
        self,
        tmp_path: Path,
    ) -> None:
        manager = ConfigManager(base_path=tmp_path)
        workspace_config = manager.load_workspace_config(tmp_path)
        workspace_config.active_agent_key = "main_agent"
        workspace_config.session_defaults.history_max_messages = 111
        workspace_config.session_defaults.history_max_chars = 7777
        workspace_config.session_defaults.max_recent_participant_messages = 9
        workspace_config.session_defaults.enable_auto_compression = False
        workspace_config.orchestration_defaults = {
            "task_models": {"memory_extract": "deepseek-chat"},
            "pending_message_threshold": 0,
        }
        manager.save_workspace_config(tmp_path, workspace_config)

        object.__setattr__(workspace_config, "active_agent_key", None)
        object.__setattr__(workspace_config.session_defaults, "history_max_messages", None)
        object.__setattr__(workspace_config.session_defaults, "history_max_chars", None)
        object.__setattr__(workspace_config.session_defaults, "max_recent_participant_messages", None)
        object.__setattr__(workspace_config.session_defaults, "enable_auto_compression", None)
        workspace_config.orchestration_defaults = {
            "task_models": {"memory_extract": None},
            "pending_message_threshold": None,
        }
        object.__setattr__(workspace_config.provider_policy, "prefer_workspace_registry", None)
        manager.save_workspace_config(tmp_path, workspace_config)

        layout = WorkspaceLayout(tmp_path)
        manifest_path = layout.workspace_manifest_path()
        snapshot_path = layout.session_config_path()
        manifest_payload = load_json_document(manifest_path)
        snapshot_payload = load_json_document(snapshot_path)

        assert "null" not in manifest_path.read_text(encoding="utf-8")
        assert "null" not in snapshot_path.read_text(encoding="utf-8")
        assert manifest_payload["active_agent_key"] == "main_agent"
        assert manifest_payload["session_defaults"]["history_max_messages"] == 111
        assert manifest_payload["session_defaults"]["history_max_chars"] == 7777
        assert manifest_payload["session_defaults"]["max_recent_participant_messages"] == 9
        assert manifest_payload["session_defaults"]["enable_auto_compression"] is False
        assert manifest_payload["orchestration_defaults"]["task_models"]["memory_extract"] == "deepseek-chat"
        assert manifest_payload["orchestration_defaults"]["pending_message_threshold"] == 0.0
        assert manifest_payload["provider_policy"]["prefer_workspace_registry"] is True
        assert snapshot_payload["generated_agent_key"] == "main_agent"
        assert snapshot_payload["history_max_messages"] == 111
        assert snapshot_payload["history_max_chars"] == 7777
        assert snapshot_payload["max_recent_participant_messages"] == 9
        assert snapshot_payload["enable_auto_compression"] is False
        assert snapshot_payload["orchestration"]["task_models"]["memory_extract"] == "deepseek-chat"
        assert snapshot_payload["orchestration"]["pending_message_threshold"] == 0.0

class TestEnvVarSubstitution:
    """Test environment variable substitution patterns."""

    @pytest.fixture
    def config_manager(self) -> ConfigManager:
        """Create a ConfigManager instance."""
        return ConfigManager()

    def test_multiple_env_vars(self, config_manager: ConfigManager) -> None:
        """Test string with multiple environment variables."""
        os.environ["HOST"] = "localhost"
        os.environ["PORT"] = "8080"
        result = config_manager._resolve_env_vars("${HOST}:${PORT}")
        assert result == "localhost:8080"

    def test_env_var_in_nested_structure(self, config_manager: ConfigManager) -> None:
        """Test environment variable resolution in nested structures."""
        os.environ["API_KEY"] = "secret123"
        config = {
            "credentials": {
                "api_key": "${API_KEY}",
                "endpoints": ["${API_KEY}_endpoint1", "${API_KEY}_endpoint2"],
            }
        }
        result = config_manager._resolve_values(config)
        assert result["credentials"]["api_key"] == "secret123"
        assert result["credentials"]["endpoints"][0] == "secret123_endpoint1"


class TestConfigIntegration:
    """Integration tests for ConfigManager."""

    def test_load_default_dev_config(self) -> None:
        """Test loading the default dev config file."""
        manager = ConfigManager()
        try:
            config = manager.load_from_json(
                Path(__file__).parent.parent / "sirius_pulse" / "configs" / "dev.json"
            )
            assert config.agent.name == "SiriusAI-Dev"
            # 验证多模型协同已配置（unified_model 或 task_models）
            assert config.orchestration.unified_model or config.orchestration.task_models
        except FileNotFoundError:
            pytest.skip("Default config files not available")

    def test_load_default_test_config(self) -> None:
        """Test loading the default test config file."""
        manager = ConfigManager()
        try:
            config = manager.load_from_json(
                Path(__file__).parent.parent / "sirius_pulse" / "configs" / "test.json"
            )
            assert config.agent.name == "SiriusAI-Test"
            assert config.agent.model == "mock-model"
        except FileNotFoundError:
            pytest.skip("Default config files not available")
