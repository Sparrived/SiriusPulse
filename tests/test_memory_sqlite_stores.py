from __future__ import annotations

import json

from sirius_pulse.memory.cognition_store import CognitionEventStore
from sirius_pulse.memory.evolution.models import MetaTag, Triple
from sirius_pulse.memory.schema import BehaviorSchema, SchemaInductor, SchemaStore
from sirius_pulse.memory.situation.models import Situation
from sirius_pulse.memory.situation.store import SituationStore


def _situation(situation_id: str, *, group_id: str = "g1", created_at: str = "2026-01-01T00:00:00+00:00") -> Situation:
    return Situation(
        situation_id=situation_id,
        group_id=group_id,
        created_at=created_at,
        triples=[
            Triple(
                subject="Alice",
                subject_user_id="u1",
                predicate="likes",
                obj="tests",
                confidence=0.8,
                meta_tag=MetaTag.STATED,
                source_message_id="m1",
                source_record_id="r1",
            )
        ],
        participants=["u1", "u2"],
        topics=["tests"],
        summary=f"summary-{situation_id}",
        source_entry_ids=["m1", "m2"],
        time_range_start="2026-01-01T00:00:00+00:00",
        time_range_end="2026-01-01T00:01:00+00:00",
        validated_triple_count=1,
        rejected_triple_count=0,
    )


def test_cognition_store_when_events_and_decisions_are_added_then_queries_return_aggregates(tmp_path):
    store = CognitionEventStore(tmp_path / "cognition.db", batch_size=2)

    store.add(
        group_id="g1",
        user_id="u1",
        valence=0.2,
        arousal=0.4,
        basic_emotion="joy",
        intensity=0.7,
        social_intent="chat",
        urgency_score=0.6,
        relevance_score=0.8,
        directed_score=0.3,
        sarcasm_score=0.1,
        entitlement_score=0.2,
        directed_signals={"mention": True},
        timestamp=10.0,
    )
    assert store.get_recent() == []
    store.add(group_id="g1", user_id="u2", basic_emotion="", social_intent="help", timestamp=11.0)
    store.add_decision(group_id="g1", user_id="u1", strategy="delayed", score=0.7, threshold=0.5, reason="gap", timestamp=12.0)
    store.flush()

    recent = store.get_recent(limit=2)
    decisions = store.get_decision_events("g1")

    assert recent[0]["user_id"] == "u2"
    assert recent[1]["directed_signals"] == {"mention": True}
    assert decisions[0]["strategy"] == "delayed"
    assert store.get_group_timeline("g1")[0]["user_id"] == "u2"
    assert store.get_emotion_distribution("g1") == {"unknown": 1, "joy": 1}
    assert store.get_intent_distribution("g1") == {"help": 1, "chat": 1}
    assert store.get_user_stats("g1")[0]["event_count"] == 1
    assert store.get_group_summary()[0]["group_id"] == "g1"
    assert store.get_strategy_distribution("g1") == {"delayed": 1}
    assert store.get_decision_summary("g1")["reason_distribution"] == {"gap": 1}
    assert store.get_decision_timeline("g1")[0]["reason"] == "gap"
    assert store.get_score_distributions("g1") == {
        "directed": [0.3, 0.0],
        "sarcasm": [0.1, 0.0],
        "entitlement": [0.2, 0.0],
    }


def test_cognition_store_when_old_rows_are_cleaned_then_events_and_decisions_are_removed(tmp_path):
    store = CognitionEventStore(tmp_path / "cognition.db")
    store.add(group_id="old", timestamp=1.0)
    store.add(group_id="new", timestamp=10_000_000_000.0)
    store.add_decision(group_id="old", timestamp=1.0)
    store.add_decision(group_id="new", timestamp=10_000_000_000.0)
    store.flush()

    assert store.cleanup_old_events(days=30) == 2
    assert [row["group_id"] for row in store.get_recent()] == ["new"]
    assert [row["group_id"] for row in store.get_decision_events()] == ["new"]


def test_situation_store_when_situations_are_saved_then_queries_and_marks_work(tmp_path):
    store = SituationStore(tmp_path / "situations.db")
    first = _situation("s1", created_at="2026-01-01T00:00:00+00:00")
    second = _situation("s2", created_at="2026-01-01T00:02:00+00:00")

    store.save(first)
    store.save(second)

    assert store.get("s1") == first
    assert [s.situation_id for s in store.get_recent("g1")] == ["s1", "s2"]
    assert [s.situation_id for s in store.get_by_group("g1")] == ["s2", "s1"]
    assert [s.situation_id for s in store.get_all()] == ["s2", "s1"]
    assert store.count_by_group("g1") == 2
    assert store.get_extracted_entry_ids("g1") == {"m1", "m2"}

    store.mark_processed(["s1"])

    assert [s.situation_id for s in store.get_unprocessed("g1")] == ["s2"]
    assert [s.situation_id for s in store.get_recent("g1", unprocessed_only=False)] == ["s1", "s2"]


def test_situation_store_when_rows_are_deleted_then_counts_change(tmp_path):
    store = SituationStore(tmp_path / "situations.db")
    store.save(_situation("old", created_at="2025-01-01T00:00:00+00:00"))
    store.save(_situation("new", created_at="2026-01-01T00:00:00+00:00"))

    assert store.delete_before("2025-06-01T00:00:00+00:00") == 1
    assert store.delete_by_ids(["new", "missing"]) == 1
    assert store.delete_by_ids([]) == 0
    assert store.count_by_group("g1") == 0


def test_schema_store_when_saving_schemas_then_replaces_existing_user_rows(tmp_path):
    store = SchemaStore(tmp_path / "schema.db")
    first = BehaviorSchema(
        schema_id="ignored",
        central_proposition="likes reliable tests",
        supporting_evidence=["e1"],
        expected_inferences=["i1"],
        confidence=0.8,
        formed_at="2026-01-01T00:00:00+00:00",
        last_validated="2026-01-02T00:00:00+00:00",
    )
    replacement = BehaviorSchema(
        central_proposition="prefers focused coverage",
        supporting_evidence=["e2"],
        expected_inferences=["i2"],
        confidence=0.9,
    )

    store.save("u1", [first])
    store.save("u1", [replacement])

    loaded = store.load("u1")
    assert len(loaded) == 1
    assert loaded[0].central_proposition == "prefers focused coverage"
    assert loaded[0].supporting_evidence == ["e2"]
    assert loaded[0].expected_inferences == ["i2"]
    assert loaded[0].confidence == 0.9
    assert loaded[0].formed_at
    assert store.load("missing") == []


def test_schema_inductor_when_response_is_json_or_fenced_then_parses_dict():
    assert SchemaInductor._parse_response('{"schemas": []}') == {"schemas": []}
    assert SchemaInductor._parse_response('```json\n{"schemas": []}\n```') == {"schemas": []}
    assert SchemaInductor._parse_response("[]") is None
    assert SchemaInductor._parse_response("{broken") is None


def test_cognition_store_when_directed_signals_are_corrupt_then_returns_empty_dict(tmp_path):
    store = CognitionEventStore(tmp_path / "cognition.db")
    store.execute(
        """INSERT INTO cognition_events
           (timestamp, group_id, user_id, directed_signals)
           VALUES (?, ?, ?, ?)""",
        (1.0, "g1", "u1", "{broken"),
    )
    store.commit()

    assert store.get_recent()[0]["directed_signals"] == {}
