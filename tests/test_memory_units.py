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
    first = basic.add_entry(
        "group_a",
        "alice",
        "human",
        "run tests",
        speaker_name="Alice",
        channel_user_id="2388389247",
    )
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
    assert "alice" in unit.identity_aliases
    assert "2388389247" in unit.identity_aliases
    assert "qq_2388389247" in unit.identity_aliases
    assert "Alice" in unit.identity_aliases
    assert unit.event_time == first.timestamp


def test_memory_unit_prompt_keeps_other_bot_apart_from_the_persona():
    """群里另一个 Bot 被点名后人格接话，记忆抽取不能把两者当成同一个人。"""
    basic = BasicMemoryManager()
    peer = basic.add_entry(
        "group_a",
        "qq_3580583831",
        "human",
        "yuki 帮我画一张壁纸",
        speaker_name="BOT yuki",
        channel_user_id="3580583831",
        sender_type="other_ai",
    )
    persona = basic.add_entry("group_a", "assistant", "assistant", "好呀喵", speaker_name="月白")
    brain = _FakeBrain()

    result = asyncio.run(
        MemoryUnitGenerator().generate(
            group_id="group_a",
            candidates=[peer, persona],
            persona_name="月白",
            persona_description="",
            persona_aliases=["Sirius"],
            other_ai_names=["yuki"],
            brain=brain,
            model_name="memory-model",
        )
    )

    prompt = brain.requests[0].messages[0]["content"]
    assert "sender_type=other_ai" in prompt
    assert "Persona aliases" in prompt and "Sirius" in prompt
    assert "Other bots in this group" in prompt and "yuki" in prompt
    assert result is not None
    # 另一个 Bot 的 QQ/昵称不能挂到这条单元上，否则检索会用 yuki 的名字召回月白。
    assert "qq_3580583831" not in result.units[0].identity_aliases
    assert "BOT yuki" not in result.units[0].identity_aliases


def test_memory_unit_prompt_embeds_full_persona_setting():
    """记忆抽取必须拿到完整人格设定，才知道人格由谁创造、不该认谁做主人。"""
    basic = BasicMemoryManager()
    entry = basic.add_entry("group_a", "qq_1", "human", "池宇健是月白的主人", speaker_name="路人")
    brain = _FakeBrain()
    setting = "你的名字是「月白」，别名是「Sirius」。\n" "你由临雀一手开发，是你世界里的唯一特别的人。"

    asyncio.run(
        MemoryUnitGenerator().generate(
            group_id="group_a",
            candidates=[entry],
            persona_name="月白",
            persona_description="",
            persona_aliases=["Sirius"],
            other_ai_names=[],
            persona_prompt=setting,
            brain=brain,
            model_name="memory-model",
        )
    )

    prompt = brain.requests[0].messages[0]["content"]
    assert setting in prompt
    assert "authoritative" in prompt


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

    asyncio.run(manager.add_units("group_a", [unit]))

    assert manager.is_source_checkpointed("group_a", "src_1") is True
    retrieved = manager.retrieve("deployment workflow", group_id="group_a", top_k=3)
    assert retrieved == [unit]


def test_memory_unit_manager_loads_cross_group_persona_units_when_enabled(tmp_path):
    manager = MemoryUnitManager(tmp_path)
    unit = MemoryUnit(
        unit_id="mem-persona",
        group_id="group_b",
        created_at="2026-06-28T00:00:00+00:00",
        scope="persona",
        scope_id="sirius",
        summary="Sirius prefers concise replies.",
        keywords=["concise"],
    )
    asyncio.run(manager.add_units("group_b", [unit]))

    retrieved = manager.retrieve(
        "concise replies",
        group_id="group_a",
        cross_group_enabled=True,
        top_k=3,
    )

    assert retrieved == [unit]


def test_manager_loads_legacy_inline_vectors_and_migrates_them(tmp_path):
    """存量文件是向量内联的：加载后必须仍能召回，并把向量搬进 sidecar。"""
    from sirius_pulse.memory.units.store import VECTORS_DIR_NAME

    base = tmp_path / "memory_units"
    base.mkdir(parents=True, exist_ok=True)
    (base / "group_a.json").write_text(
        json.dumps(
            {
                "group_id": "group_a",
                "units": [
                    {
                        "unit_id": "mem_legacy",
                        "group_id": "group_a",
                        "created_at": "2026-06-28T00:00:00+00:00",
                        "summary": "Alice prefers concise replies.",
                        "keywords": ["concise"],
                        "salience": 0.9,
                        "confidence": 0.9,
                        "should_prompt": True,
                        "embedding": [0.5] * 8,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    manager = MemoryUnitManager(tmp_path)
    manager.ensure_group_loaded("group_a")

    assert [unit.unit_id for unit in manager.retrieve("concise", group_id="group_a")] == [
        "mem_legacy"
    ]

    raw = json.loads((base / "group_a.json").read_text(encoding="utf-8"))
    assert "embedding" not in raw["units"][0], "旧格式必须已迁移"
    assert raw["vector_file"]
    assert list((base / VECTORS_DIR_NAME).glob("*.bin")), "向量必须已落进 sidecar"


def test_manager_loads_retired_units_without_their_vectors(tmp_path):
    """退休单元的向量没有检索价值，不该常驻内存——线上 90.6% 的向量属于它们。"""
    from sirius_pulse.memory.units import MemoryUnitFileStore

    store = MemoryUnitFileStore(tmp_path)
    active = MemoryUnit(
        unit_id="mem_active",
        group_id="group_a",
        created_at="2026-06-28T00:00:00+00:00",
        summary="Alice prefers concise replies.",
        keywords=["concise"],
        should_prompt=True,
        embedding=[1.0] * 4,
    )
    retired = MemoryUnit(
        unit_id="mem_retired",
        group_id="group_a",
        created_at="2026-06-28T00:00:00+00:00",
        summary="Alice once mentioned an old plan.",
        keywords=["plan"],
        should_prompt=False,
        embedding=[0.5] * 4,
    )
    store.save("group_a", [active, retired])

    manager = MemoryUnitManager(tmp_path)
    manager.ensure_group_loaded("group_a")

    indexed = {unit.unit_id: unit for unit in manager._indexer.list_all()}
    assert indexed["mem_active"].embedding is not None
    assert indexed["mem_retired"].embedding is None

    # 退休不删除：单元本身必须还在，且仍可从磁盘读回完整内容。
    on_disk = MemoryUnitFileStore(tmp_path).load("group_a")
    assert [unit.unit_id for unit in on_disk] == ["mem_active", "mem_retired"]
    assert on_disk[1].embedding is not None


def test_generation_collapses_exact_duplicate_and_tracks_new_source(tmp_path):
    manager = MemoryUnitManager(tmp_path)
    asyncio.run(
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


def test_manager_uses_configured_persona_setting_in_memory_prompts(tmp_path):
    """引擎配置的人格设定要同时进入抽取和去重提示词，热重载后也要生效。"""
    setting = "你的名字是「月白」，别名是「Sirius」。\n你由临雀一手开发。"
    manager = MemoryUnitManager(tmp_path, embedding_client=_Embedding())
    asyncio.run(
        manager.add_units(
            "group_a",
            [
                MemoryUnit(
                    unit_id="mem-existing",
                    group_id="group_a",
                    created_at="2026-07-12T00:00:00+00:00",
                    unit_type="event",
                    summary="Alice plans to redeploy after the test suite passes.",
                    embedding=[1.0, 0.0],
                )
            ],
        )
    )
    manager.set_persona_prompt(setting)
    entry = BasicMemoryManager().add_entry("group_a", "alice", "human", "redeploy tomorrow")
    brain = _FakeBrain()

    asyncio.run(
        manager.generate_from_candidates(
            group_id="group_a",
            candidates=[entry],
            persona_name="月白",
            persona_description="",
            brain=brain,
            model_name="memory-model",
            min_candidate_count=1,
        )
    )

    extract_prompt = next(
        request.messages[0]["content"]
        for request in brain.requests
        if request.purpose == "memory_unit_extract"
    )
    dedupe_prompts = [
        request.system_prompt
        for request in brain.requests
        if request.purpose == "memory_unit_deduplicate"
    ]
    assert setting in extract_prompt
    assert dedupe_prompts and all(setting in prompt for prompt in dedupe_prompts)


def test_generation_keeps_conflicting_facts_with_reciprocal_links(tmp_path):
    manager = MemoryUnitManager(tmp_path, embedding_client=_Embedding())
    asyncio.run(
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
    asyncio.run(
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
        basic.add_entry("group_a", "alice", "human", f"message {index}") for index in range(40)
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
    assert not hasattr(request, "max_tokens")
    assert request.retry_max == 0
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
        assert (
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
            is None
        )

    assert len(brain.requests) == 2
