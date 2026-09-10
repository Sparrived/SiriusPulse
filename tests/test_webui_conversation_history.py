from __future__ import annotations

import json
from types import SimpleNamespace

from sirius_pulse.webui.memory_api import (
    _annotate_memory_compression,
    _compact_history_entry,
    _load_compressed_memory_source_index,
    _load_runtime_basic_memory_messages,
    _merge_conversation_messages,
    _rewrite_jsonl_without_conversation_key,
)


def test_runtime_basic_memory_messages_are_loaded_for_conversation_history(tmp_path):
    engine_state = tmp_path / "engine_state"
    engine_state.mkdir()
    (engine_state / "basic_memory.json").write_text(
        json.dumps(
            {
                "group_a": [
                    {
                        "entry_id": "assistant_1",
                        "group_id": "group_a",
                        "role": "assistant",
                        "content": "reply",
                        "conversation_chain": [
                            {
                                "role": "system",
                                "content": "【历史聊天信息】x【历史聊天信息结束】",
                            }
                        ],
                    }
                ],
                "group_b": [
                    {
                        "entry_id": "human_1",
                        "role": "human",
                        "content": "not selected",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    paths = SimpleNamespace(engine_state=engine_state)

    messages = _load_runtime_basic_memory_messages(paths, "group_a")

    assert len(messages) == 1
    assert messages[0]["entry_id"] == "assistant_1"
    assert messages[0]["group_id"] == "group_a"
    assert messages[0]["tags"] == []


def test_rewrite_jsonl_when_deleting_one_message_then_keeps_other_and_invalid_lines(tmp_path):
    archive = tmp_path / "group_a.jsonl"
    archive.write_text(
        "{not-json}\n"
        + json.dumps({"entry_id": "remove", "content": "gone"})
        + "\n"
        + json.dumps({"entry_id": "keep", "content": "stays"})
        + "\n",
        encoding="utf-8",
    )

    deleted = _rewrite_jsonl_without_conversation_key(archive, "group_a", "id:remove")

    assert deleted == 1
    assert (
        archive.read_text(encoding="utf-8")
        == "{not-json}\n" + json.dumps({"entry_id": "keep", "content": "stays"}) + "\n"
    )
    assert not list(tmp_path.glob("group_a.jsonl.*.tmp"))


def test_history_entry_compaction_drops_duplicate_request_and_bounds_chain():
    entry = _compact_history_entry(
        {
            "content": "x" * 9_000,
            "injected_request": {"messages": [{"content": "duplicate"}]},
            "conversation_chain": [
                {"role": "system", "content": "s" * 9_000},
                *[{"role": "user", "content": "m" * 5_000} for _ in range(20)],
            ],
        }
    )

    assert entry["injected_request"] == {}
    assert len(entry["conversation_chain"]) == 12
    assert len(entry["conversation_chain"][0]["content"]) < 8_100
    assert len(entry["content"]) < 8_100


def test_conversation_merge_prefers_runtime_chain_for_same_entry_id():
    archive = [
        {
            "entry_id": "assistant_1",
            "role": "assistant",
            "content": "reply",
            "conversation_chain": [],
        }
    ]
    runtime = [
        {
            "entry_id": "assistant_1",
            "role": "assistant",
            "content": "reply",
            "conversation_chain": [{"role": "system", "content": "full prompt"}],
        }
    ]

    merged = _merge_conversation_messages(archive, runtime)

    assert len(merged) == 1
    assert merged[0]["conversation_chain"] == [{"role": "system", "content": "full prompt"}]


def test_conversation_merge_keeps_archive_intent_scores_when_runtime_lacks_them():
    archive = [
        {
            "entry_id": "human_1",
            "role": "human",
            "content": "hello",
            "intent_scores": {"social_intent": "social", "directed_score": 0.75},
        }
    ]
    runtime = [
        {
            "entry_id": "human_1",
            "role": "human",
            "content": "hello",
            "conversation_chain": [],
        }
    ]

    merged = _merge_conversation_messages(archive, runtime)

    assert len(merged) == 1
    assert merged[0]["intent_scores"] == {"social_intent": "social", "directed_score": 0.75}


def test_conversation_history_marks_memory_compressed_sources(tmp_path):
    memory_units = tmp_path / "memory_units"
    memory_units.mkdir()
    (memory_units / "group_a.json").write_text(
        json.dumps(
            {
                "group_id": "group_a",
                "units": [
                    {
                        "unit_id": "mem_1",
                        "created_at": "2026-06-28T00:00:00+00:00",
                        "unit_type": "event",
                        "summary": "Alice agreed to redeploy.",
                        "source_ids": ["human_1"],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    paths = SimpleNamespace(dir=tmp_path)
    messages = [
        {"entry_id": "human_1", "content": "run deploy"},
        {"entry_id": "human_2", "content": "still active"},
    ]

    source_index = _load_compressed_memory_source_index(paths, "group_a")
    _annotate_memory_compression(messages, source_index)

    assert messages[0]["memory_compressed"] is True
    assert messages[0]["memory_refs"] == [
        {
            "kind": "memory_unit",
            "id": "mem_1",
            "summary": "Alice agreed to redeploy.",
            "created_at": "2026-06-28T00:00:00+00:00",
            "unit_type": "event",
        }
    ]
    assert messages[1]["memory_compressed"] is False
    assert messages[1]["memory_refs"] == []
