"""Configuration package for Sirius Chat.

Provides centralized configuration management for sessions, agents, and orchestration.
Exports all configuration-related classes and utilities.
"""

from __future__ import annotations

# Configuration data models
from sirius_pulse.config.models import (
    Agent,
    AgentPreset,
    ConfigParameter,
    MemoryPolicy,
    MultiModelConfig,
    OrchestrationPolicy,
    ProviderPolicy,
    SessionConfig,
    SessionDefaults,
    TokenUsageRecord,
    WorkspaceBootstrap,
    WorkspaceConfig,
)

# Configuration management
from sirius_pulse.config.manager import ConfigManager

# Orchestration configuration utilities
from sirius_pulse.config.helpers import (
    configure_full_orchestration,
    configure_orchestration_models,
    configure_orchestration_retries,
    configure_orchestration_temperatures,
    auto_configure_multimodal_agent,
    create_agent_with_multimodal,
    create_multimodel_config,
    setup_multimodel_config,
)

# File I/O utilities
from sirius_pulse.config.file_io import atomic_json_save

__all__ = [
    # Models
    "Agent",
    "AgentPreset",
    "ConfigParameter",
    "MemoryPolicy",
    "MultiModelConfig",
    "OrchestrationPolicy",
    "ProviderPolicy",
    "SessionConfig",
    "SessionDefaults",
    "TokenUsageRecord",
    "WorkspaceBootstrap",
    "WorkspaceConfig",
    # Management
    "ConfigManager",
    # File I/O
    "atomic_json_save",
    # Helpers
    "configure_full_orchestration",
    "configure_orchestration_models",
    "configure_orchestration_retries",
    "configure_orchestration_temperatures",
    "auto_configure_multimodal_agent",
    "create_agent_with_multimodal",
    "create_multimodel_config",
    "setup_multimodel_config",
]
