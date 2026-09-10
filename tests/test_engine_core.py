from __future__ import annotations

from types import SimpleNamespace

from sirius_pulse.core import engine_core
from sirius_pulse.core.engine_core import _EmotionalGroupChatEngineBase
from sirius_pulse.memory.basic import BasicMemoryManager


def test_engine_when_pending_message_is_low_information_then_detects_filler():
    assert _EmotionalGroupChatEngineBase._is_low_information_pending_message("哈哈") is True
    assert _EmotionalGroupChatEngineBase._is_low_information_pending_message("ok") is True
    assert _EmotionalGroupChatEngineBase._is_low_information_pending_message("怎么了？") is False


def test_engine_orchestration_defaults_route_memory_extract_to_memory_model(tmp_path):
    engine = engine_core._EmotionalGroupChatEngineBase.__new__(
        engine_core._EmotionalGroupChatEngineBase
    )
    engine.work_path = tmp_path
    engine.config = {}

    engine._init_orchestration_and_task_models()

    assert engine._task_models["cognition_analyze"] == "gpt-4o-mini"
    assert engine._task_models["memory_extract"] == "gpt-4o-mini"


def test_engine_orchestration_custom_models_route_memory_extract_to_memory_model(tmp_path):
    from sirius_pulse.core.orchestration_store import OrchestrationStore

    OrchestrationStore.save(
        tmp_path,
        {
            "analysis_model": "vision-model",
            "chat_model": "chat-model",
            "memory_model": "memory-model",
            "plugin_model": "plugin-model",
        },
    )
    engine = engine_core._EmotionalGroupChatEngineBase.__new__(
        engine_core._EmotionalGroupChatEngineBase
    )
    engine.work_path = tmp_path
    engine.config = {}

    engine._init_orchestration_and_task_models()

    assert engine._task_models["cognition_analyze"] == "vision-model"
    assert engine._task_models["memory_extract"] == "memory-model"
    assert engine._task_models["proactive_generate"] == "chat-model"
    assert "diary_generate" not in engine._task_models
    assert "diary_consolidate" not in engine._task_models


def test_engine_adapter_routes_when_registered_then_resolve_groups_and_private_users():
    engine = _EmotionalGroupChatEngineBase.__new__(_EmotionalGroupChatEngineBase)
    engine._adapter_routes = []
    first = SimpleNamespace(adapter_type="napcat")
    second = SimpleNamespace(adapter_type="discord")

    third = SimpleNamespace(adapter_type="napcat")
    engine.register_adapter(first, group_ids=["g1", "g2"], private_user_ids=[])
    engine.register_adapter(second, group_ids=["g2"], private_user_ids=["u2"])
    engine.register_adapter(third, group_ids=["g1"], private_user_ids=[])

    assert engine.resolve_adapter_types("g1") == ["napcat"]
    assert engine.resolve_adapter_types("g2") == ["napcat", "discord"]
    assert engine.resolve_adapter_types("g3") == []
    assert engine.resolve_adapter_types("private_u2") == ["napcat", "discord"]
    assert engine.resolve_adapter_types("private_u1") == ["napcat"]
    assert engine.resolve_adapter_route_counts("g1") == {"napcat": 2}
    assert engine.resolve_adapter_route_counts("g2") == {"napcat": 1, "discord": 1}

    engine.unregister_adapter(first)
    assert engine.resolve_adapter_types("g1") == ["napcat"]
    assert engine.resolve_adapter_route_counts("g1") == {"napcat": 1}


def test_engine_records_delivered_markdown_card_in_basic_history():
    engine = _EmotionalGroupChatEngineBase.__new__(_EmotionalGroupChatEngineBase)
    stored = []
    semantic = []
    persisted = []
    engine.persona = SimpleNamespace(name="月白")
    engine.basic_memory = BasicMemoryManager()
    engine.basic_store = SimpleNamespace(append=stored.append)
    engine.semantic_memory = SimpleNamespace(
        record_ai_sent=lambda **kwargs: semantic.append(kwargs)
    )
    engine._persist_group_state = persisted.append

    engine._record_assistant_message(
        group_id="9001",
        target_user_id="1001",
        content="部署结论\n\n- 服务已恢复",
        tags=[{"type": "image", "label": "富文本卡片"}],
        platform_message_id="42",
        injected_request={
            "system_prompt": "完整 system",
            "messages": [{"role": "user", "content": "完整 user"}],
            "tools": [],
            "tool_choice": None,
        },
    )

    entry = engine.basic_memory.get_context("9001", n=1)[0]
    assert entry.content == "部署结论\n\n- 服务已恢复"
    assert entry.tags == [{"type": "image", "label": "富文本卡片"}]
    assert entry.platform_message_id == "42"
    assert entry.system_prompt == "完整 system"
    assert entry.injected_request == {"tool_choice": None}
    assert stored == [entry]
    assert semantic[0]["target_user_id"] == "1001"
    assert persisted == ["9001"]
