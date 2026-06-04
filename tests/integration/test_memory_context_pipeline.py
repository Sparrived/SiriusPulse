"""Integration tests for memory context assembly across stores."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from sirius_pulse.memory.basic.manager import BasicMemoryManager
from sirius_pulse.memory.context_assembler import ContextAssembler
from sirius_pulse.memory.diary.models import DiaryEntry
from sirius_pulse.memory.situation.models import Situation
from sirius_pulse.memory.situation.store import SituationStore

pytestmark = pytest.mark.integration


class RecordingDiaryRetriever:
    def __init__(self, entries: list[DiaryEntry]) -> None:
        self.entries = entries
        self.calls: list[dict[str, object]] = []

    def retrieve(
        self,
        *,
        query: str,
        group_id: str,
        top_k: int,
        max_tokens_budget: int,
    ) -> list[DiaryEntry]:
        self.calls.append(
            {
                "query": query,
                "group_id": group_id,
                "top_k": top_k,
                "max_tokens_budget": max_tokens_budget,
            }
        )
        return self.entries


@dataclass(slots=True)
class FakeBio:
    name: str
    short_bio: str = ""
    identity_anchors: list[str] = field(default_factory=list)


class FakeBiographyView:
    def __init__(self, bios: dict[str, FakeBio]) -> None:
        self._bios = bios

    def get_biography(self, user_id: str) -> FakeBio | None:
        return self._bios.get(user_id)


def test_context_when_memory_sources_exist_then_prompt_contains_diary_situation_and_biography(
    tmp_path,
):
    basic = BasicMemoryManager()
    first = basic.add_entry(
        "group-a",
        "u1",
        "human",
        "Alice mentioned the release plan.",
        speaker_name="Alice",
        platform_message_id="m1",
    )
    second = basic.add_entry(
        "group-a",
        "u2",
        "human",
        "Bob asked about rollout risk.",
        speaker_name="Bob",
        platform_message_id="m2",
    )
    basic.add_entry(
        "group-a",
        "assistant",
        "assistant",
        "I will track that.",
        speaker_name="TestBot",
    )
    situation_store = SituationStore(tmp_path / "persona.db")
    situation_store.save(
        Situation(
            situation_id="sit-1",
            group_id="group-a",
            summary="Release planning is being discussed.",
            topics=["release"],
            participants=["u1", "u2"],
            source_entry_ids=[first.entry_id, second.entry_id],
        )
    )
    diary = RecordingDiaryRetriever(
        [
            DiaryEntry(
                entry_id="diary-1",
                group_id="group-a",
                created_at="2026-01-01T00:00:00+00:00",
                content="Earlier diary: Alice owned the release checklist.",
                summary="Alice owned release checklist",
                keywords=["release"],
            )
        ]
    )
    bio_view = FakeBiographyView(
        {
            "u1": FakeBio(
                name="Alice",
                short_bio="Release lead for the project.",
                identity_anchors=["release lead"],
            )
        }
    )
    assembler = ContextAssembler(
        basic,
        diary,
        situation_store=situation_store,
        biography_view=bio_view,
    )

    try:
        messages = assembler.build_messages(
            "group-a",
            "What is the next release step?",
            "base system",
            speaker_user_id="u1",
            speaker_name="Alice",
        )
    finally:
        situation_store.close()

    system_prompt = messages[0]["content"]
    assert messages[-1]["role"] == "user"
    assert "Earlier diary" in system_prompt
    assert "Release planning is being discussed." in system_prompt
    assert "Alice owned the release checklist" in system_prompt
    assert "Release lead for the project." in system_prompt
    assert "Bob asked about rollout risk." in system_prompt
    assert diary.calls[0]["group_id"] == "group-a"
    assert "release lead" in str(diary.calls[0]["query"])
