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
