"""Dedicated user persona profile memory.

This package stores model-maintained people profiles as structured profile cards.
It is intentionally separate from interaction statistics and alias resolution.
"""

from sirius_pulse.memory.profile.manager import UserPersonaProfileManager
from sirius_pulse.memory.profile.models import (
    ProfileItem,
    ProfileSection,
    ProfileUpdate,
    UserPersonaProfile,
)
from sirius_pulse.memory.profile.prompt import ProfilePromptRenderer
from sirius_pulse.memory.profile.store import UserPersonaProfileStore

__all__ = [
    "ProfileItem",
    "ProfilePromptRenderer",
    "ProfileSection",
    "ProfileUpdate",
    "UserPersonaProfile",
    "UserPersonaProfileManager",
    "UserPersonaProfileStore",
]
