"""Legacy bridges for creating PersonaProfile instances."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sirius_pulse.models.persona import PersonaProfile

_ARCHETYPES: dict[str, dict[str, Any]] = {}


class PersonaGenerator:
    """Creates PersonaProfile from legacy templates or roleplay presets."""

    @staticmethod
    def from_template(archetype_name: str) -> PersonaProfile:
        """Create persona from a built-in archetype (zero LLM cost)."""
        data = _ARCHETYPES.get(archetype_name)
        if data is None:
            raise ValueError(
                f"Unknown archetype: {archetype_name}. " f"Available: {list(_ARCHETYPES.keys())}"
            )

        profile = PersonaProfile(
            source="template",
            created_at=datetime.now(timezone.utc).isoformat(),
            **data,
        )
        return profile

    @staticmethod
    def from_roleplay_preset(agent_preset: Any) -> PersonaProfile:
        """Convert legacy AgentPreset to a prompt-backed PersonaProfile."""
        from sirius_pulse.config.models import AgentPreset

        if not isinstance(agent_preset, AgentPreset):
            raise TypeError(f"Expected AgentPreset, got {type(agent_preset)}")

        agent = agent_preset.agent
        prompt = agent_preset.global_system_prompt or ""

        profile = PersonaProfile(
            name=agent.name,
            source="roleplay_bridge",
            created_at=datetime.now(timezone.utc).isoformat(),
            full_system_prompt=prompt,
        )
        return profile
