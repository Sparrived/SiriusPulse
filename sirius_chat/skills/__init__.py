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

from sirius_chat.skills.models import (
    BackgroundTaskSpec,
    SkillDefinition,
    SkillEngineContext,
    SkillInvocationContext,
    SkillParameter,
    SkillPassiveType,
    SkillResult,
    SkillChainContext,
    TriggerSpec,
)
from sirius_chat.skills.registry import SkillRegistry
from sirius_chat.skills.executor import SkillExecutor
from sirius_chat.skills.data_store import SkillDataStore
from sirius_chat.skills.dependency_resolver import resolve_skill_dependencies

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
]
