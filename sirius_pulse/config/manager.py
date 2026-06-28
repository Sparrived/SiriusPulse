"""Configuration management framework for Sirius Chat.

Provides loading, validation, and merging of configurations from JSON files,
environment variables, and secret management.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sirius_pulse.config.config_helpers import (
    _build_session_defaults,
    _build_workspace_config_from_payload,
    _dict_to_session_config,
    _load_workspace_session_snapshot,
    _normalize_orchestration_defaults,
    _normalize_workspace_config,
    _resolve_active_agent_key,
    _resolve_env_vars,
    _resolve_values,
    _validate_config,
    build_orchestration_policy_from_dict,
)
from sirius_pulse.config.jsonc import (
    build_default_orchestration_payload,
    load_json_document,
    write_session_config_jsonc,
)
from sirius_pulse.config.models import (
    Agent,
    AgentPreset,
    SessionConfig,
    WorkspaceConfig,
)
from sirius_pulse.utils.layout import WorkspaceLayout

__all__ = [
    "ConfigManager",
    "_build_session_defaults",
    "_build_workspace_config_from_payload",
    "_dict_to_session_config",
    "_load_workspace_session_snapshot",
    "_normalize_orchestration_defaults",
    "_normalize_workspace_config",
    "_resolve_active_agent_key",
    "_resolve_values",
    "_validate_config",
]


class ConfigManager:
    """Manages configuration loading, validation, and merging."""

    def __init__(self, base_path: Path | None = None) -> None:
        """Initialize ConfigManager.

        Args:
            base_path: Base path for resolving relative config paths.
                      Defaults to package root.
        """
        if base_path is None:
            base_path = Path(__file__).parent
        self.base_path = base_path

    # Backward-compatible delegates for methods moved to config_helpers
    def _resolve_env_vars(self, text: str) -> str:
        return _resolve_env_vars(text)

    def _resolve_values(self, obj: Any) -> Any:
        return _resolve_values(obj)

    def _validate_config(self, config: dict[str, Any]) -> None:
        _validate_config(config)

    def _dict_to_session_config(self, config_dict: dict[str, Any], base_dir: Path) -> SessionConfig:
        return _dict_to_session_config(config_dict, base_dir)

    def load_from_json(self, path: Path | str) -> SessionConfig:
        """Load configuration from JSON file.

        Args:
            path: Path to JSON config file

        Returns:
            SessionConfig instance

        Raises:
            FileNotFoundError: If config file not found
            ValueError: If config is invalid
        """
        config_path = Path(path)
        if not config_path.is_absolute():
            config_path = self.base_path / config_path

        if not config_path.exists():
            raise FileNotFoundError(f"配置文件不存在：{config_path}")

        raw_dict = load_json_document(config_path)

        # Resolve environment variables and secrets
        resolved = _resolve_values(raw_dict)

        # Validate required fields
        _validate_config(resolved)

        # Build SessionConfig from dict
        return _dict_to_session_config(resolved, config_path.parent)

    def merge_configs(
        self,
        base: dict[str, Any],
        override: dict[str, Any],
    ) -> dict[str, Any]:
        """Merge two configuration dictionaries.

        Override dict takes precedence over base dict.

        Args:
            base: Base configuration
            override: Override configuration

        Returns:
            Merged configuration
        """
        merged = dict(base)
        for key, value in override.items():
            if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
                merged[key] = self.merge_configs(merged[key], value)
            else:
                merged[key] = value
        return merged

    def load_workspace_config(
        self,
        work_path: Path | str,
        *,
        data_path: Path | str | None = None,
    ) -> WorkspaceConfig:
        """Load workspace-level config, creating defaults when missing."""
        config_root = Path(work_path)
        runtime_root = Path(data_path) if data_path is not None else config_root
        layout = WorkspaceLayout(runtime_root, config_path=config_root)
        layout.ensure_directories()
        manifest_path = layout.workspace_manifest_path()

        manifest_payload: dict[str, Any] = {}
        manifest_mtime_ns = -1
        if manifest_path.exists():
            payload = load_json_document(manifest_path)
            if isinstance(payload, dict):
                manifest_payload = payload
                manifest_mtime_ns = manifest_path.stat().st_mtime_ns

        session_snapshot = _load_workspace_session_snapshot(layout)
        session_snapshot_mtime_ns = -1
        session_snapshot_path = layout.session_config_path()
        if session_snapshot and session_snapshot_path.exists():
            session_snapshot_mtime_ns = session_snapshot_path.stat().st_mtime_ns
        default_config = WorkspaceConfig(
            work_path=layout.config_root,
            data_path=layout.data_root,
            layout_version=layout.layout_version,
        )
        if manifest_payload:
            config = _build_workspace_config_from_payload(
                manifest_payload,
                layout=layout,
                fallback=default_config,
            )
        else:
            config = default_config

        config.work_path = layout.config_root
        config.data_path = layout.data_root
        config.layout_version = layout.layout_version

        if session_snapshot:
            generated_agent_key = config.active_agent_key
            raw_key = session_snapshot.get("generated_agent_key")
            if raw_key is not None:
                generated_agent_key = str(raw_key).strip() or config.active_agent_key
            if generated_agent_key and (
                not manifest_payload
                or session_snapshot_mtime_ns >= manifest_mtime_ns
                or not config.active_agent_key
            ):
                config.active_agent_key = generated_agent_key
            config.session_defaults = _build_session_defaults(
                session_snapshot,
                config.session_defaults,
            )
            config.orchestration_defaults = _normalize_orchestration_defaults(
                session_snapshot.get("orchestration"),
                fallback=dict(config.orchestration_defaults),
            )

        if not config.active_agent_key:
            config.active_agent_key = _resolve_active_agent_key(layout)
        return config

    def save_workspace_config(
        self,
        work_path: Path | str,
        config: WorkspaceConfig,
        *,
        data_path: Path | str | None = None,
    ) -> None:
        """Persist workspace-level config and a human-readable session snapshot."""
        config_root = Path(work_path)
        runtime_root_source = (
            data_path if data_path is not None else (config.data_path or config.work_path)
        )
        runtime_root = Path(runtime_root_source)
        layout = WorkspaceLayout(runtime_root, config_path=config_root)
        layout.ensure_directories()
        existing_config = self.load_workspace_config(layout.config_root, data_path=layout.data_root)
        normalized_config = _normalize_workspace_config(
            config,
            layout=layout,
            fallback=existing_config,
        )
        payload = normalized_config.to_dict()
        manifest_path = layout.workspace_manifest_path()
        manifest_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        session_snapshot = {
            "generated_agent_key": normalized_config.active_agent_key,
            "history_max_messages": normalized_config.session_defaults.history_max_messages,
            "history_max_chars": normalized_config.session_defaults.history_max_chars,
            "max_recent_participant_messages": normalized_config.session_defaults.max_recent_participant_messages,
            "enable_auto_compression": normalized_config.session_defaults.enable_auto_compression,
            "orchestration": self.merge_configs(
                build_default_orchestration_payload(),
                dict(normalized_config.orchestration_defaults),
            ),
        }
        write_session_config_jsonc(layout.session_config_path(), session_snapshot)

    def build_session_config(
        self,
        *,
        work_path: Path | str,
        data_path: Path | str | None = None,
        session_id: str,
        overrides: dict[str, Any] | None = None,
    ) -> SessionConfig:
        """Build a runtime SessionConfig from workspace config + roleplay assets."""
        from sirius_pulse.persona_generation import load_generated_agent_library

        config_root = Path(work_path)
        runtime_root = Path(data_path) if data_path is not None else config_root
        layout = WorkspaceLayout(runtime_root, config_path=config_root)
        workspace_config = self.load_workspace_config(
            layout.config_root, data_path=layout.data_root
        )
        agents, selected = load_generated_agent_library(layout.config_root)
        agent_key = str((overrides or {}).get("agent_key", "")).strip()
        resolved_agent_key = agent_key or workspace_config.active_agent_key or selected
        if not resolved_agent_key:
            raise ValueError("当前 workspace 尚未选择 generated agent。")
        if resolved_agent_key not in agents:
            raise ValueError(f"找不到生成的主教：{resolved_agent_key}")

        preset = agents[resolved_agent_key]
        session_defaults = workspace_config.session_defaults
        override_payload = dict(overrides or {})
        session_config = SessionConfig(
            work_path=layout.config_root,
            data_path=layout.data_root,
            preset=AgentPreset(
                agent=Agent(
                    name=preset.agent.name,
                    persona=preset.agent.persona,
                    model=preset.agent.model,
                    temperature=preset.agent.temperature,
                    max_tokens=preset.agent.max_tokens,
                    metadata=dict(preset.agent.metadata),
                ),
                global_system_prompt=preset.global_system_prompt,
            ),
            history_max_messages=int(
                override_payload.get(
                    "history_max_messages",
                    session_defaults.history_max_messages,
                )
            ),
            history_max_chars=int(
                override_payload.get("history_max_chars", session_defaults.history_max_chars)
            ),
            max_recent_participant_messages=int(
                override_payload.get(
                    "max_recent_participant_messages",
                    session_defaults.max_recent_participant_messages,
                )
            ),
            enable_auto_compression=bool(
                override_payload.get(
                    "enable_auto_compression",
                    session_defaults.enable_auto_compression,
                )
            ),
            orchestration=build_orchestration_policy_from_dict(
                dict(workspace_config.orchestration_defaults),
                agent_model=preset.agent.model,
            ),
            session_id=session_id,
        )
        return session_config
