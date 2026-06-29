from __future__ import annotations

from types import SimpleNamespace

from sirius_pulse.core import engine_core
from sirius_pulse.core.engine_core import _EmotionalGroupChatEngineBase


def test_engine_when_pending_message_is_low_information_then_detects_filler():
    assert _EmotionalGroupChatEngineBase._is_low_information_pending_message("哈哈") is True
    assert _EmotionalGroupChatEngineBase._is_low_information_pending_message("ok") is True
    assert _EmotionalGroupChatEngineBase._is_low_information_pending_message("怎么了？") is False


class _Dummy:
    def __init__(self, *args, **kwargs) -> None:
        self.args = args
        self.kwargs = kwargs


class _DummyDiaryManager:
    def __init__(self, *args, **kwargs) -> None:
        self._retriever = object()

    def is_source_diarized(self, *args, **kwargs) -> bool:
        return False


def test_engine_memory_system_initializes_profile_manager_with_shared_conn(tmp_path, monkeypatch):
    store_calls = []
    manager_calls = []

    class FakeProfileStore:
        def __init__(self, *, conn=None) -> None:
            store_calls.append(conn)

    class FakeProfileManager:
        def __init__(self, store, **kwargs) -> None:
            manager_calls.append((store, kwargs))

    monkeypatch.setattr(engine_core, "MemoryStorage", _Dummy)
    monkeypatch.setattr(engine_core, "SemanticMemoryManager", _Dummy)
    monkeypatch.setattr(engine_core, "BasicMemoryManager", _Dummy)
    monkeypatch.setattr(engine_core, "BasicMemoryFileStore", _Dummy)
    monkeypatch.setattr(engine_core, "DiaryManager", _DummyDiaryManager)
    monkeypatch.setattr(engine_core, "UnifiedUserManager", _Dummy)
    monkeypatch.setattr(engine_core, "IdentityResolver", _Dummy)
    monkeypatch.setattr(engine_core, "UserPersonaProfileStore", FakeProfileStore)
    monkeypatch.setattr(engine_core, "UserPersonaProfileManager", FakeProfileManager)
    monkeypatch.setattr(engine_core, "ColdDetector", _Dummy)
    monkeypatch.setattr(engine_core, "ContextAssembler", _Dummy)
    monkeypatch.setattr(engine_core, "GlossaryManager", _Dummy)

    engine = engine_core._EmotionalGroupChatEngineBase.__new__(
        engine_core._EmotionalGroupChatEngineBase
    )
    engine.work_path = tmp_path
    engine.persona = SimpleNamespace(name="sirius", aliases=[])
    engine._persona_db_conn = object()
    engine._remote_bridge = None
    engine._vector_store = object()
    engine._embedding_client = object()

    engine._init_memory_system()

    assert store_calls == [engine._persona_db_conn]
    assert manager_calls[0][1]["persona_name"] == "sirius"
    assert getattr(engine, "biography_view") is None


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
