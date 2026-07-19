from __future__ import annotations

from sirius_pulse.core import engine_core
from sirius_pulse.core.engine_core import _EmotionalGroupChatEngineBase


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
    assert "diary_generate" not in engine._task_models
    assert "diary_consolidate" not in engine._task_models
