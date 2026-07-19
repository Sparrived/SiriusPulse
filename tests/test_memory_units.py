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
    def __init__(self, dedupe_response=None):
        self.dedupe_response = dedupe_response
        self.requests = []

    async def raw_call(self, request):
        self.requests.append(request)
        if request.purpose == "memory_unit_deduplicate":
            return json.dumps(self.dedupe_response or {"decision": "NEW"})
        assert request.purpose == "memory_unit_extract"
        return json.dumps({"units": [_generated_unit()]})


def _generated_unit(summary="Alice agreed to redeploy after tests pass."):
    return {
        "type": "event",
        "scope": "group",
        "summary": summary,
        "participants": ["alice"],
        "topics": ["deploy"],
        "keywords": ["redeploy", "tests"],
        "salience": 0.8,
        "confidence": 0.9,
        "lifespan": "medium",
        "should_prompt": True,
        "source_indices": [1],
    }


class _Embedding:
    available = True

    def encode_single(self, text):
        return [1.0, 0.0]


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
    assert unit.source_ids == [first.entry_id]
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


def test_generation_collapses_exact_duplicate_and_tracks_new_source(tmp_path):
    manager = MemoryUnitManager(tmp_path)
    manager.add_units(
        "group_a",
        [
            MemoryUnit(
                unit_id="mem-existing",
                group_id="group_a",
                created_at="2026-07-12T00:00:00+00:00",
                summary="Alice agreed to redeploy after tests pass.",
                source_ids=["src-existing"],
            )
        ],
    )
    new_entry = BasicMemoryManager().add_entry("group_a", "alice", "human", "redeploy")

    result = asyncio.run(
        manager.generate_from_candidates(
            group_id="group_a",
            candidates=[new_entry],
            persona_name="Sirius",
            persona_description="",
            brain=_FakeBrain(),
            model_name="memory-model",
            min_candidate_count=1,
        )
    )

    units = manager.get_units_for_group("group_a")
    assert result is not None
    assert len(units) == 1
    assert units[0].unit_id == result.units[0].unit_id
    assert units[0].unit_id != "mem-existing"
    assert units[0].source_ids == ["src-existing", new_entry.entry_id]
    assert manager.is_source_checkpointed("group_a", new_entry.entry_id) is True


def test_generation_keeps_conflicting_facts_with_reciprocal_links(tmp_path):
    manager = MemoryUnitManager(tmp_path, embedding_client=_Embedding())
    manager.add_units(
        "group_a",
        [
            MemoryUnit(
                unit_id="mem-existing",
                group_id="group_a",
                created_at="2026-07-12T00:00:00+00:00",
                summary="Alice prefers concise replies.",
            )
        ],
    )
    entry = BasicMemoryManager().add_entry("group_a", "alice", "human", "detailed replies")
    brain = _FakeBrain(
        {"decision": "CONFLICT", "target_unit_id": "mem-existing", "reason": "偏好已改变"}
    )

    asyncio.run(
        manager.generate_from_candidates(
            group_id="group_a",
            candidates=[entry],
            persona_name="Sirius",
            persona_description="",
            brain=brain,
            model_name="memory-model",
            min_candidate_count=1,
        )
    )

    units = manager.get_units_for_group("group_a")
    assert len(units) == 2
    existing = next(unit for unit in units if unit.unit_id == "mem-existing")
    incoming = next(unit for unit in units if unit.unit_id != "mem-existing")
    assert existing.metadata["conflicts_with"] == [incoming.unit_id]
    assert incoming.metadata["conflicts_with"] == ["mem-existing"]


def test_generation_keeps_identical_summaries_in_separate_group_files(tmp_path):
    manager = MemoryUnitManager(tmp_path)
    manager.add_units(
        "group_a",
        [
            MemoryUnit(
                unit_id="mem-a",
                group_id="group_a",
                created_at="2026-07-12T00:00:00+00:00",
                summary="Alice agreed to redeploy after tests pass.",
            )
        ],
    )
    entry = BasicMemoryManager().add_entry("group_b", "alice", "human", "redeploy")

    asyncio.run(
        manager.generate_from_candidates(
            group_id="group_b",
            candidates=[entry],
            persona_name="Sirius",
            persona_description="",
            brain=_FakeBrain(),
            model_name="memory-model",
            min_candidate_count=1,
        )
    )

    assert [unit.unit_id for unit in manager.get_units_for_group("group_a")] == ["mem-a"]
    assert len(manager.get_units_for_group("group_b")) == 1


def test_generation_caps_a_single_checkpoint_batch(tmp_path):
    manager = MemoryUnitManager(tmp_path)
    basic = BasicMemoryManager()
    candidates = [
        basic.add_entry("group_a", "alice", "human", f"message {index}")
        for index in range(40)
    ]
    brain = _FakeBrain()

    asyncio.run(
        manager.generate_from_candidates(
            group_id="group_a",
            candidates=candidates,
            persona_name="Sirius",
            persona_description="",
            brain=brain,
            model_name="memory-model",
            min_candidate_count=1,
        )
    )

    request = next(item for item in brain.requests if item.purpose == "memory_unit_extract")
    content = request.messages[0]["content"]
    assert "source_id=" + candidates[0].entry_id in content
    assert "source_id=" + candidates[31].entry_id in content
    assert "source_id=" + candidates[32].entry_id not in content


def test_failed_checkpoint_batch_enters_backoff(tmp_path):
    class _InvalidBrain(_FakeBrain):
        async def raw_call(self, request):
            self.requests.append(request)
            return "not json"

    manager = MemoryUnitManager(tmp_path)
    entry = BasicMemoryManager().add_entry("group_a", "alice", "human", "message")
    brain = _InvalidBrain()

    for _ in range(2):
        assert asyncio.run(
            manager.generate_from_candidates(
                group_id="group_a",
                candidates=[entry],
                persona_name="Sirius",
                persona_description="",
                brain=brain,
                model_name="memory-model",
                min_candidate_count=1,
            )
        ) is None

    assert len(brain.requests) == 2
