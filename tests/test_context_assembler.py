"""Tests for ContextAssembler message construction."""

from __future__ import annotations

from dataclasses import dataclass, field

from sirius_pulse.memory.basic.manager import BasicMemoryManager
from sirius_pulse.memory.context_assembler import ContextAssembler


class EmptyDiaryRetriever:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def retrieve(
        self,
        *,
        query: str,
        group_id: str,
        top_k: int,
        max_tokens_budget: int,
    ) -> list[object]:
        self.calls.append(
            {
                "query": query,
                "group_id": group_id,
                "top_k": top_k,
                "max_tokens_budget": max_tokens_budget,
            }
        )
        return []


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


def _assembler(
    basic: BasicMemoryManager,
    diary: EmptyDiaryRetriever | None = None,
    bio_view: FakeBiographyView | None = None,
) -> tuple[ContextAssembler, EmptyDiaryRetriever]:
    retriever = diary or EmptyDiaryRetriever()
    return ContextAssembler(basic, retriever, biography_view=bio_view), retriever


def test_messages_when_history_has_assistant_turns_then_splits_user_blocks():
    basic = BasicMemoryManager()
    basic.add_entry("g1", "u1", "human", "first user", speaker_name="Alice")
    basic.add_entry("g1", "assistant", "assistant", "first reply", speaker_name="Bot")
    basic.add_entry("g1", "u2", "human", "second user", speaker_name="Bob")
    basic.add_entry("g1", "assistant", "assistant", "second reply", speaker_name="Bot")
    assembler, _ = _assembler(basic)

    messages = assembler.build_messages(
        "g1",
        "current turn",
        "base system",
        speaker_user_id="u3",
        speaker_name="Cara",
    )

    assert [m["role"] for m in messages] == [
        "system",
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
    ]
    assert "first user" in messages[1]["content"]
    assert messages[2]["content"] == "first reply"
    assert "second user" in messages[3]["content"]
    assert messages[4]["content"] == "second reply"
    assert "current turn" in messages[5]["content"]


def test_messages_when_pending_exists_then_merges_other_users_into_current_turn():
    basic = BasicMemoryManager()
    basic.add_entry("g1", "u1", "human", "answered user", speaker_name="Alice")
    basic.add_entry("g1", "assistant", "assistant", "previous reply", speaker_name="Bot")
    basic.add_entry("g1", "u2", "human", "pending other", speaker_name="Bob")
    basic.add_entry("g1", "u3", "human", "pending same user", speaker_name="Cara")
    assembler, _ = _assembler(basic)

    messages = assembler.build_messages(
        "g1",
        "current turn",
        "base system",
        speaker_user_id="u3",
        speaker_name="Cara",
    )

    current = messages[-1]["content"]
    assert "pending other" in current
    assert "current turn" in current
    assert "pending same user" not in current
    assert "<pending_messages>" not in current


def test_messages_when_content_is_already_tagged_then_keeps_current_payload_verbatim():
    basic = BasicMemoryManager()
    assembler, _ = _assembler(basic)
    tagged = '<message speaker="Alice" user_id="u1">raw <tag></message>'

    messages = assembler.build_messages(
        "g1",
        tagged,
        "base system",
        content_is_tagged=True,
    )

    assert messages[-1] == {"role": "user", "content": tagged}


def test_search_query_when_biography_exists_then_diary_retrieval_is_enriched():
    basic = BasicMemoryManager()
    bio_view = FakeBiographyView(
        {
            "u1": FakeBio(
                name="Alice",
                short_bio="builds storage systems",
                identity_anchors=["backend engineer"],
            ),
            "u2": FakeBio(name="Bob", short_bio=""),
        }
    )
    assembler, diary = _assembler(basic, bio_view=bio_view)

    messages = assembler.build_messages(
        "g1",
        "how is the migration going?",
        "base system",
        speaker_user_id="u1",
        mentioned_user_ids=["u2"],
    )

    assert messages[0]["role"] == "system"
    query = str(diary.calls[0]["query"])
    assert "how is the migration going?" in query
    assert "Alice" in query
    assert "backend engineer" in query
    assert "Bob" in query
