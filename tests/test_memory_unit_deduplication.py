import json

import pytest

from sirius_pulse.memory.units import MemoryUnit
from sirius_pulse.memory.units.deduplicator import (
    DedupVerdict,
    MemoryUnitDeduplicator,
    link_conflict,
    merge_memory_units,
    normalize_summary,
    same_boundary,
)


def _unit(unit_id: str, summary: str, **changes) -> MemoryUnit:
    values = {
        "unit_id": unit_id,
        "group_id": "group-a",
        "created_at": "2026-07-12T00:00:00+00:00",
        "unit_type": "preference",
        "scope": "user",
        "scope_id": "alice",
        "summary": summary,
        "participants": ["alice"],
        "topics": ["reply-style"],
        "keywords": ["concise"],
        "salience": 0.6,
        "confidence": 0.7,
        "lifespan": "medium",
        "source_ids": ["src-1"],
        "embedding": [1.0, 0.0],
    }
    values.update(changes)
    return MemoryUnit(**values)


def test_normalized_equal_summary_is_duplicate_only_inside_same_boundary():
    old = _unit("mem-old", "Alice prefers concise replies。")
    same = _unit("mem-new", "  alice prefers concise replies!  ")
    other_group = _unit("mem-other", same.summary, group_id="group-b")

    assert normalize_summary(old.summary) == normalize_summary(same.summary)
    assert same_boundary(old, same) is True
    assert same_boundary(old, other_group) is False


def test_merge_uses_incoming_identity_and_keeps_all_sources():
    old = _unit("mem-old", "Alice prefers concise replies.")
    new = _unit(
        "mem-new",
        "Alice prefers concise replies with examples.",
        created_at="2026-07-12T01:00:00+00:00",
        participants=["alice", "sirius"],
        topics=["reply-style", "examples"],
        keywords=["examples"],
        salience=0.9,
        confidence=0.8,
        lifespan="long",
        source_ids=["src-2"],
    )
    merged = merge_memory_units(
        old,
        new,
        DedupVerdict("MERGE", "mem-old", "Alice prefers concise replies with examples.", "补充"),
        now_iso="2026-07-12T02:00:00+00:00",
    )

    assert merged.unit_id == "mem-new"
    assert merged.created_at == old.created_at
    assert merged.source_ids == ["src-1", "src-2"]
    assert merged.participants == ["alice", "sirius"]
    assert merged.topics == ["reply-style", "examples"]
    assert merged.keywords == ["concise", "examples"]
    assert merged.salience == 0.9
    assert merged.confidence == 0.8
    assert merged.lifespan == "long"
    assert merged.embedding is None
    assert merged.metadata["merged_unit_ids"] == ["mem-old"]
    assert merged.metadata["revision_count"] == 1


def test_conflict_keeps_both_units_and_links_them():
    old = _unit("mem-old", "Alice prefers concise replies.")
    new = _unit("mem-new", "Alice now prefers detailed explanations.")

    linked_old, linked_new = link_conflict(old, new, "偏好发生变化")

    assert linked_old.metadata["conflicts_with"] == ["mem-new"]
    assert linked_new.metadata["conflicts_with"] == ["mem-old"]
    assert linked_old.metadata["conflict_reason"] == "偏好发生变化"


class _Embedding:
    available = True

    def encode_single(self, text: str) -> list[float]:
        return [1.0, 0.0] if "concise" in text.lower() else [0.0, 1.0]


def test_indexer_returns_only_same_boundary_semantic_candidates():
    from sirius_pulse.memory.units import MemoryUnitIndexer

    indexer = MemoryUnitIndexer(_Embedding())
    match = _unit("mem-match", "Alice prefers concise replies.")
    indexer.add(match)
    indexer.add(_unit("mem-group", match.summary, group_id="group-b"))
    indexer.add(_unit("mem-type", match.summary, unit_type="note"))

    incoming = _unit("mem-new", "Alice likes concise answers.", embedding=None)
    candidates = indexer.semantic_candidates(incoming, top_k=5, min_similarity=0.80)

    assert [(unit.unit_id, score) for unit, score in candidates] == [("mem-match", 1.0)]


def test_indexer_replace_group_removes_stale_units():
    from sirius_pulse.memory.units import MemoryUnitIndexer

    indexer = MemoryUnitIndexer()
    indexer.add(_unit("old", "old"))
    replacement = _unit("new", "new")
    indexer.replace_group("group-a", [replacement])
    assert indexer.list_all() == [replacement]


def test_indexer_retrieves_user_memory_across_qq_and_display_name_forms():
    from sirius_pulse.memory.units import MemoryUnitIndexer

    indexer = MemoryUnitIndexer()
    target = _unit(
        "mem-target",
        "临雀确认测试通过后重新部署。",
        scope_id="qq_2388389247",
        participants=["临雀。"],
        keywords=["重新部署"],
        identity_aliases=["2388389247", "qq_2388389247", "临雀。", "临雀"],
    )
    other = _unit(
        "mem-other",
        "临雀确认测试通过后重新部署。",
        scope_id="qq_2940667688",
        participants=["月白"],
        identity_aliases=["2940667688", "qq_2940667688"],
    )
    indexer.add(target)
    indexer.add(other)

    results = indexer.search(
        "@临雀 之前是否重新部署",
        group_id="group-a",
        user_id="2388389247",
        identity_aliases=["临雀。", "qq_2388389247"],
        top_k=5,
    )

    assert [unit.unit_id for unit, _score in results] == ["mem-target"]


def test_indexer_uses_cross_group_persona_memory_but_keeps_group_memory_local():
    from sirius_pulse.memory.units import MemoryUnitIndexer

    indexer = MemoryUnitIndexer()
    persona = _unit(
        "mem-persona",
        "Sirius prefers concise replies.",
        scope="persona",
        scope_id="sirius",
        group_id="group-b",
        participants=[],
        keywords=["concise"],
    )
    group = _unit(
        "mem-group",
        "Sirius prefers concise replies.",
        scope="group",
        group_id="group-b",
        participants=[],
        keywords=["concise"],
    )
    indexer.add(persona)
    indexer.add(group)

    results = indexer.search(
        "concise replies",
        group_id="group-a",
        cross_group_enabled=True,
        top_k=5,
    )

    assert [unit.unit_id for unit, _score in results] == ["mem-persona"]


def test_indexer_splits_query_and_matches_chinese_ngrams():
    from sirius_pulse.memory.units import MemoryUnitIndexer

    indexer = MemoryUnitIndexer()
    unit = _unit(
        "mem-deploy",
        "已完成服务重新部署。",
        scope="group",
        scope_id="",
        keywords=["部署"],
        retrieval_terms=["上线"],
        status="completed",
    )
    indexer.add(unit)

    results = indexer.search("上次情况如何？现在部署了吗？", group_id="group-a")

    assert results and results[0][0].unit_id == "mem-deploy"


class _Brain:
    def __init__(self, response=None, error=None):
        self.response = response
        self.error = error
        self.requests = []

    async def raw_call(self, request):
        self.requests.append(request)
        if self.error:
            raise self.error
        return json.dumps(self.response)


@pytest.mark.asyncio
async def test_adjudicator_accepts_only_valid_candidate_target():
    brain = _Brain(
        {
            "decision": "MERGE",
            "target_unit_id": "mem-old",
            "merged_summary": "Alice prefers concise replies with examples.",
            "reason": "兼容补充",
        }
    )

    verdict = await MemoryUnitDeduplicator().adjudicate(
        _unit("mem-new", "Alice likes short answers with examples."),
        [_unit("mem-old", "Alice prefers concise replies.")],
        brain=brain,
        model_name="memory-model",
    )

    assert verdict.decision == "MERGE"
    assert brain.requests[0].purpose == "memory_unit_deduplicate"
    assert brain.requests[0].response_format == {"type": "json_object"}


@pytest.mark.asyncio
async def test_adjudicator_gets_full_persona_setting_as_identity_baseline():
    """去重也要带上完整人格设定，避免把别人的主人并到人格身上。"""
    brain = _Brain({"decision": "NEW"})
    setting = "你的名字是「月白」，别名是「Sirius」。\n你由临雀一手开发，是你世界里的唯一特别的人。"

    await MemoryUnitDeduplicator(persona_context=setting).adjudicate(
        _unit("mem-new", "池宇健是月白的主人。"),
        [_unit("mem-old", "临雀是月白的主人。")],
        brain=brain,
        model_name="memory-model",
    )

    system_prompt = brain.requests[0].system_prompt
    assert setting in system_prompt
    assert "人格设定" in system_prompt


@pytest.mark.asyncio
async def test_adjudicator_omits_large_provenance_fields_from_model_payload():
    brain = _Brain({"decision": "NEW"})
    old = _unit(
        "mem-old",
        "Alice prefers concise replies.",
        source_ids=[f"src-{index}" for index in range(500)],
        metadata={"merged_unit_ids": [f"mem-{index}" for index in range(100)]},
    )

    await MemoryUnitDeduplicator().adjudicate(
        _unit("mem-new", "Alice likes short answers."),
        [old],
        brain=brain,
        model_name="memory-model",
    )

    payload = json.loads(brain.requests[0].messages[0]["content"])
    candidate = payload["candidates"][0]
    assert "source_ids" not in candidate
    assert "embedding" not in candidate
    assert "metadata" not in candidate
    assert len(brain.requests[0].messages[0]["content"]) < 2_000


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "error"),
    [
        ({"decision": "DUPLICATE", "target_unit_id": "unknown"}, None),
        (
            {
                "decision": "MERGE",
                "target_unit_id": "mem-old",
                "merged_summary": "x" * 181,
            },
            None,
        ),
        (None, RuntimeError("model unavailable")),
    ],
)
async def test_adjudicator_falls_back_to_new_for_invalid_or_failed_response(response, error):
    verdict = await MemoryUnitDeduplicator().adjudicate(
        _unit("mem-new", "Alice likes short answers."),
        [_unit("mem-old", "Alice prefers concise replies.")],
        brain=_Brain(response, error),
        model_name="memory-model",
    )

    assert verdict == DedupVerdict("NEW")
