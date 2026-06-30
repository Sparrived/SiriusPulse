from __future__ import annotations

import json
from types import SimpleNamespace

from sirius_pulse.webui.memory_api import (
    _annotate_memory_compression,
    _load_compressed_memory_source_index,
    _load_runtime_basic_memory_messages,
    _merge_conversation_messages,
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
