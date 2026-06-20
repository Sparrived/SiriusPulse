from __future__ import annotations

import json
from types import SimpleNamespace

from sirius_pulse.webui.memory_api import (
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
                                "content": "<cacheable_conversation_history>x</cacheable_conversation_history>",
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
