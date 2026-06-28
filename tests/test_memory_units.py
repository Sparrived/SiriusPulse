from __future__ import annotations

import asyncio
import json

from sirius_pulse.memory.basic.manager import BasicMemoryManager
from sirius_pulse.memory.units import (
    MemoryUnit,
    MemoryUnitFileStore,
    MemoryUnitGenerator,
    MemoryUnitManager,
)


class _FakeBrain:
    async def raw_call(self, request):
        assert request.purpose == "memory_unit_extract"
        return json.dumps(
            {
                "units": [
                    {
                        "type": "event",
                        "scope": "group",
                        "summary": "Alice agreed to redeploy after tests pass.",
                        "participants": ["alice"],
                        "topics": ["deploy"],
                        "keywords": ["redeploy", "tests"],
                        "salience": 0.8,
                        "confidence": 0.9,
                        "lifespan": "medium",
                        "should_prompt": True,
                        "source_indices": [1, 2],
                    }
                ]
            }
        )


def test_memory_unit_generator_maps_source_indices_to_entry_ids():
    basic = BasicMemoryManager()
    first = basic.add_entry("group_a", "alice", "human", "run tests", speaker_name="Alice")
    second = basic.add_entry(
        "group_a", "assistant", "assistant", "then redeploy", speaker_name="Bot"
    )

    result = asyncio.run(
        MemoryUnitGenerator().generate(
            group_id="group_a",
            candidates=[first, second],
            persona_name="Sirius",
            persona_description="",
            brain=_FakeBrain(),
            model_name="memory-model",
        )
    )

    assert result is not None
    assert len(result.units) == 1
    unit = result.units[0]
    assert unit.unit_type == "event"
    assert unit.source_ids == [first.entry_id, second.entry_id]
    assert unit.summary == "Alice agreed to redeploy after tests pass."


def test_memory_unit_store_round_trips(tmp_path):
    store = MemoryUnitFileStore(tmp_path)
    unit = MemoryUnit(
        unit_id="mem_1",
        group_id="group_a",
        created_at="2026-06-28T00:00:00+00:00",
        summary="Alice prefers concise replies.",
        keywords=["concise"],
        source_ids=["src_1"],
    )

    store.save("group_a", [unit])
    loaded = store.load("group_a")

    assert loaded == [unit]


def test_memory_unit_manager_retrieves_and_tracks_checkpointed_sources(tmp_path):
    manager = MemoryUnitManager(tmp_path)
    unit = MemoryUnit(
        unit_id="mem_1",
        group_id="group_a",
        created_at="2026-06-28T00:00:00+00:00",
        summary="Alice asked Sirius to remember deployment workflow.",
        keywords=["deployment", "workflow"],
        salience=0.9,
        confidence=0.9,
        source_ids=["src_1"],
    )

    manager.add_units("group_a", [unit])

    assert manager.is_source_checkpointed("group_a", "src_1") is True
    retrieved = manager.retrieve("deployment workflow", group_id="group_a", top_k=3)
    assert retrieved == [unit]
