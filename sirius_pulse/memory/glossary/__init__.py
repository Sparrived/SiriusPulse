"""Glossary: term definitions learned from conversations."""

from __future__ import annotations

from sirius_pulse.memory.glossary.manager import GlossaryManager
from sirius_pulse.memory.glossary.models import GlossaryTerm

__all__ = [
    "GlossaryTerm",
    "GlossaryManager",
]
