"""Intent analysis enums (kept for cognition rule engine)."""

from __future__ import annotations

from enum import Enum


class SocialIntent(Enum):
    """Purpose-driven intent taxonomy"""

    HELP_SEEKING = "help_seeking"
    EMOTIONAL = "emotional"
    SOCIAL = "social"
    SILENT = "silent"
    PLUGIN_COMMAND = "plugin_command"


class HelpSubtype(Enum):
    TECH_HELP = "tech_help"
    INFO_QUERY = "info_query"
    DECISION_HELP = "decision_help"


class EmotionalSubtype(Enum):
    VENTING = "venting"
    SEEKING_EMPATHY = "seeking_empathy"
    COMPANIONSHIP = "companionship"
    CELEBRATION = "celebration"


class SocialSubtype(Enum):
    TOPIC_DISCUSSION = "topic_discussion"
    RELATIONSHIP_MAINTENANCE = "relationship_maintenance"
    HUMOR = "humor"


class SilentSubtype(Enum):
    PRIVATE_CHAT = "private_chat"
    FILLER = "filler"
    IRRELEVANT = "irrelevant"


INTENT_SUBTYPE_MAP: dict[SocialIntent, type[Enum]] = {
    SocialIntent.HELP_SEEKING: HelpSubtype,
    SocialIntent.EMOTIONAL: EmotionalSubtype,
    SocialIntent.SOCIAL: SocialSubtype,
    SocialIntent.SILENT: SilentSubtype,
}
