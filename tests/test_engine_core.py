from __future__ import annotations

from types import SimpleNamespace

from sirius_pulse.core import engine_core
from sirius_pulse.core.engine_core import _EmotionalGroupChatEngineBase
from sirius_pulse.memory.basic import BasicMemoryManager


def test_engine_when_pending_message_is_low_information_then_detects_filler():
    assert _EmotionalGroupChatEngineBase._is_low_information_pending_message("哈哈") is True
    assert _EmotionalGroupChatEngineBase._is_low_information_pending_message("ok") is True
    assert _EmotionalGroupChatEngineBase._is_low_information_pending_message("怎么了？") is False


def test_engine_orchestration_defaults_when_no_config_then_no_task_overrides(tmp_path):
    """没有人格编排配置时不预设任何模型相关的覆盖项。"""
    engine = engine_core._EmotionalGroupChatEngineBase.__new__(
        engine_core._EmotionalGroupChatEngineBase
    )
    engine.work_path = tmp_path
    engine.config = {}

    engine._init_orchestration_and_task_models()
    engine._init_model_router()

    # 每个任务都原样发出任务名，不因缺失配置而换成某个本地模型。
    assert engine.model_router.resolve("cognition_analyze").model_name == "cognition_analyze"
    assert engine.model_router.resolve("memory_extract").model_name == "memory_extract"


def test_engine_orchestration_custom_config_then_only_local_fields_are_applied(tmp_path):
    """编排配置里只有超时与重试属于本地；模型字段一律被忽略。"""
    from sirius_pulse.core.orchestration_store import OrchestrationStore

    OrchestrationStore.save(
        tmp_path,
        {
            "analysis_model": "vision-model",
            "chat_model": "chat-model",
            "task_models": {"cognition_analyze": "memory-model"},
            "task_timeout": {"response_generate": 45.0},
            "task_retries": {"memory_extract": 3},
        },
    )
    engine = engine_core._EmotionalGroupChatEngineBase.__new__(
        engine_core._EmotionalGroupChatEngineBase
    )
    engine.work_path = tmp_path
    engine.config = {}

    engine._init_orchestration_and_task_models()
    engine._init_model_router()

    assert engine.model_router.resolve("response_generate").timeout == 45.0
    assert engine.model_router.resolve("memory_extract").retries == 3
    # 配置里写了模型名也不生效：模型归 AMKR 的任务定义。
    assert engine.model_router.resolve("cognition_analyze").model_name == "cognition_analyze"


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
