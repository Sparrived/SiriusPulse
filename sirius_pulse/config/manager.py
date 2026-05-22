"""Configuration management framework for Sirius Chat.

Provides loading, validation, and merging of configurations from JSON files,
environment variables, and secret management.
"""

from __future__ import annotations

from sirius_pulse.config.config_helpers import (
    _build_session_defaults,
    _build_workspace_config_from_payload,
    _dict_to_session_config,
    _load_workspace_session_snapshot,
    _normalize_orchestration_defaults,
    _normalize_workspace_config,
    _resolve_active_agent_key,
    _resolve_values,
    _validate_config,
)
from sirius_pulse.config.config_manager import ConfigManager

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
