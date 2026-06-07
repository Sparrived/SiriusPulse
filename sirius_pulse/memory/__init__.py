"""Memory management module."""
from __future__ import annotations

from sirius_pulse.memory.basic import BasicMemoryFileStore, BasicMemoryManager, HeatCalculator
from sirius_pulse.memory.cold_detector import ColdDetector, ColdState
from sirius_pulse.memory.context_assembler import ContextAssembler
from sirius_pulse.memory.diary import (
    DiaryEntry,
    DiaryGenerator,
    DiaryIndexer,
    DiaryManager,
    DiaryRetriever,
)
from sirius_pulse.memory.evolution import (
    EvolutionAction,
    EvolutionChain,
    EvolutionRecord,
    Triple,
    ValidationResult,
)
from sirius_pulse.memory.glossary import GlossaryManager, GlossaryTerm
from sirius_pulse.memory.situation import Situation, SituationExtractor, SituationStore
from sirius_pulse.memory.user.unified_manager import UnifiedUserManager
from sirius_pulse.memory.user.unified_models import UnifiedUser

__all__ = [
    "BasicMemoryManager",
    "BasicMemoryFileStore",
    "HeatCalculator",
    "DiaryManager",
    "DiaryGenerator",
    "DiaryIndexer",
    "DiaryRetriever",
    "DiaryEntry",
    "ContextAssembler",
    "UnifiedUser",
    "UnifiedUserManager",
    "GlossaryManager",
    "GlossaryTerm",
    "EvolutionChain",
    "EvolutionRecord",
    "EvolutionAction",
    "Triple",
    "ValidationResult",
    "Situation",
    "SituationStore",
    "SituationExtractor",
    "ColdDetector",
    "ColdState",
]
