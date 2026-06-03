"""Integration checks for provenance hooks in the memory pipeline."""

from __future__ import annotations

import pytest

from sirius_pulse.memory.diary.slicer import DiarySlicer
from sirius_pulse.memory.evolution.chain import EvolutionChain
from sirius_pulse.memory.evolution.models import MetaTag, SituationSource, Triple
from sirius_pulse.memory.situation.models import Situation


@pytest.mark.asyncio
async def test_evolution_record_preserves_source_situation_id(tmp_path):
    chain = EvolutionChain(tmp_path / "persona.db")
    source = SituationSource(
        type="situation_extraction",
        situation_id="sit1",
        group_id="g1",
        model="memory-model",
        message_ids=["m1"],
    )

    result = await chain.validate_and_commit([
        Triple(
            subject="u1",
            subject_user_id="u1",
            predicate="住在",
            obj="深圳",
            confidence=0.7,
            meta_tag=MetaTag.STATED,
        )
    ], source)

    assert result.records[0].source_situation_id == "sit1"


@pytest.mark.asyncio
async def test_diary_slicer_carries_source_record_ids_from_situation_triples():
    situation = Situation(
        situation_id="sit1",
        group_id="g1",
        summary="Alice 说自己住在深圳",
        topics=["深圳"],
        triples=[
            Triple(
                subject="u1",
                subject_user_id="u1",
                predicate="住在",
                obj="深圳",
                source_record_id="rec1",
            )
        ],
        participants=["u1"],
    )

    slices = await DiarySlicer().slice(
        diary_content="Alice 说自己住在深圳。",
        situations=[situation],
        group_id="g1",
        diary_id="d1",
    )

    assert slices[0].source_record_ids == ["rec1"]
