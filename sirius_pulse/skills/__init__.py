"""Skill system for Sirius Chat.

Provides a mechanism for AI agents to invoke external code through
structured skill definitions. Skills are loaded from the work_path/skills/
directory and presented to the AI as callable tools.

Key components:
- SkillDefinition / SkillParameter / SkillResult: Data models
- SkillChainContext: Per-round chain context for multi-skill pipelines
- SkillRegistry: Discovers, loads, and manages skills
- SkillExecutor: Safely executes skills with parameter validation
- SkillDataStore: Persistent key-value storage for skills
"""

# Re-export config builder utilities for skill developers
from sirius_pulse.config.config_builder import (
    ConfigBuilder,
    ParamDefinition,
    build_parameters_from_class,
    config_param,
    secret,
)
from sirius_pulse.skills.data_store import SkillDataStore
from sirius_pulse.skills.dependency_resolver import resolve_skill_dependencies
from sirius_pulse.skills.executor import SkillExecutor
from sirius_pulse.skills.models import (
    BackgroundTaskSpec,
    SkillChainContext,
    SkillDefinition,
    SkillEngineContext,
    SkillInvocationContext,
    SkillParameter,
    SkillPassiveType,
    SkillResult,
    TriggerSpec,
)
from sirius_pulse.skills.registry import SkillRegistry

__all__ = [
    "BackgroundTaskSpec",
    "SkillDefinition",
    "SkillEngineContext",
    "SkillInvocationContext",
    "SkillParameter",
    "SkillPassiveType",
    "SkillResult",
    "SkillChainContext",
    "TriggerSpec",
    "SkillRegistry",
    "SkillExecutor",
    "SkillDataStore",
    "resolve_skill_dependencies",
    # Config builder utilities
    "ConfigBuilder",
    "ParamDefinition",
    "config_param",
    "secret",
    "build_parameters_from_class",
]
