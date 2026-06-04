from __future__ import annotations

import json

from sirius_pulse.memory.diary.models import DiaryEntry
from sirius_pulse.memory.diary.store import DiaryFileStore
from sirius_pulse.memory.glossary.manager import GlossaryManager
from sirius_pulse.memory.glossary.models import GlossaryTerm
from sirius_pulse.memory.semantic.models import (
    AtmosphereSnapshot,
    GroupSemanticProfile,
    ResponseRecord,
    UserSemanticProfile,
)
from sirius_pulse.memory.semantic.store import SemanticProfileStore


def _diary_entry(entry_id: str = "d1") -> DiaryEntry:
    return DiaryEntry(
        entry_id=entry_id,
        group_id="group/A",
        created_at="2026-01-01T00:00:00+00:00",
        source_ids=["m1"],
        content="entry content",
        keywords=["topic"],
        summary="summary",
        embedding=[0.1, 0.2],
        merge_count=2,
    )


def test_diary_file_store_when_saving_entries_then_loads_them_from_safe_group_path(tmp_path):
    store = DiaryFileStore(tmp_path)
    entry = _diary_entry()

    store.save("group/A", [entry])

    loaded = store.load("group/A")
    assert loaded == [entry]
    assert (tmp_path / "diary" / "group_A.json").exists()


def test_diary_file_store_when_file_is_missing_or_invalid_then_returns_empty_list(tmp_path):
    store = DiaryFileStore(tmp_path)
    broken_path = tmp_path / "diary" / "broken.json"
    broken_path.write_text("{broken", encoding="utf-8")

    assert store.load("missing") == []
    assert store.load("broken") == []
    assert DiaryFileStore._safe_name(" / ") == "default"


def test_glossary_manager_when_term_is_added_and_updated_then_merges_metadata(tmp_path):
    manager = GlossaryManager(tmp_path)

    manager.add_or_update(
        "ignored-group",
        GlossaryTerm(
            term="Codex",
            definition="first",
            confidence=0.4,
            context_examples=["example-1"],
            related_terms=["ai"],
            domain="tech",
        ),
    )
    manager.add_or_update(
        "another-group",
        GlossaryTerm(
            term="codex",
            definition="better",
            confidence=0.9,
            context_examples=["example-1", "example-2"],
            related_terms=["ai", "agent"],
            domain="custom",
        ),
    )

    term = manager.get_term("", "CODEX")

    assert term is not None
    assert term.definition == "better"
    assert term.confidence == 0.9
    assert term.usage_count == 2
    assert term.context_examples == ["example-1", "example-2"]
    assert term.related_terms == ["ai", "agent"]
    assert term.domain == "tech"
    assert manager.search("", "codex is here")[0].term == "Codex"
    assert "Codex" in manager.build_prompt_section("", "codex")


def test_glossary_manager_when_legacy_files_exist_then_migrates_to_terms_file(tmp_path):
    glossary_dir = tmp_path / "glossary"
    glossary_dir.mkdir()
    legacy_path = glossary_dir / "old_group.json"
    legacy_path.write_text(
        json.dumps(
            {
                "alpha": GlossaryTerm(
                    term="Alpha",
                    definition="legacy",
                    confidence=0.7,
                    usage_count=3,
                ).to_dict()
            }
        ),
        encoding="utf-8",
    )

    manager = GlossaryManager(tmp_path)
    migrated = manager.get_term("", "alpha")

    assert migrated is not None
    assert migrated.definition == "legacy"
    assert (glossary_dir / "terms.json").exists()
    assert (glossary_dir / "old_group.json.migrated").exists()


def test_glossary_manager_when_bad_or_empty_terms_are_added_then_safe_defaults_are_used(tmp_path):
    manager = GlossaryManager(tmp_path)

    manager.add_or_update("", GlossaryTerm(term=""))

    assert manager.get_term("", "") is None

    broken_terms = tmp_path / "glossary" / "terms.json"
    broken_terms.write_text("{broken", encoding="utf-8")

    assert GlossaryManager(tmp_path).build_prompt_section("") == ""


def test_semantic_profile_store_when_group_profile_round_trips_then_nested_records_are_restored(tmp_path):
    store = SemanticProfileStore(tmp_path)
    profile = GroupSemanticProfile(
        group_id="group/A",
        group_name="Group",
        interest_topics=["tests"],
        atmosphere_history=[AtmosphereSnapshot(timestamp="t1", group_valence=0.2, group_arousal=0.4, active_participants=3)],
        group_norms={"tone": "calm"},
        taboo_topics=["spam"],
        dominant_topic="coverage",
        pending_ai_responses=[
            ResponseRecord(sent_at="t2", target_user_id="u1", topic_hint="tests", response_length=12)
        ],
    )

    store.save_group_profile("group/A", profile)
    loaded = store.load_group_profile("group/A")

    assert loaded == profile
    assert (tmp_path / "memory" / "semantic" / "groups" / "group_A.json").exists()


def test_semantic_profile_store_when_user_profiles_are_listed_then_bad_files_are_skipped(tmp_path):
    store = SemanticProfileStore(tmp_path)
    profile = UserSemanticProfile(user_id="u/1", name="Alice", engagement_rate=0.5)
    profile.record_interaction("2026-01-01T00:00:00+00:00")

    store.save_user_profile("group/A", "u/1", profile)
    user_dir = tmp_path / "memory" / "semantic" / "users" / "group_A"
    (user_dir / "broken.json").write_text("{broken", encoding="utf-8")

    loaded = store.load_user_profile("group/A", "u/1")
    listed = store.list_group_user_profiles("group/A")

    assert loaded == profile
    assert listed == [profile]
    assert store.load_group_profile("missing") is None
    assert store.load_user_profile("missing", "u1") is None
    assert SemanticProfileStore._safe_name(" / ") == "default"
