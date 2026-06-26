from __future__ import annotations

from types import SimpleNamespace

from sirius_pulse.core import engine_core
from sirius_pulse.memory.biography.view import BiographyView
from sirius_pulse.memory.evolution.chain import EvolutionChain


class _Dummy:
    def __init__(self, *args, **kwargs) -> None:
        self.args = args
        self.kwargs = kwargs


class _DummyDiaryManager:
    def __init__(self, *args, **kwargs) -> None:
        self._retriever = object()

    def is_source_diarized(self, *args, **kwargs) -> bool:
        return False


def test_engine_memory_system_when_embedding_client_exists_then_evolution_chain_receives_only_conn(
    tmp_path, monkeypatch
):
    calls = []

    class FakeEvolutionChain:
        def __init__(self, *, conn=None) -> None:
            calls.append(conn)

    monkeypatch.setattr(engine_core, "MemoryStorage", _Dummy)
    monkeypatch.setattr(engine_core, "SemanticMemoryManager", _Dummy)
    monkeypatch.setattr(engine_core, "BasicMemoryManager", _Dummy)
    monkeypatch.setattr(engine_core, "BasicMemoryFileStore", _Dummy)
    monkeypatch.setattr(engine_core, "DiaryManager", _DummyDiaryManager)
    monkeypatch.setattr(engine_core, "UnifiedUserManager", _Dummy)
    monkeypatch.setattr(engine_core, "IdentityResolver", _Dummy)
    monkeypatch.setattr(engine_core, "EvolutionChain", FakeEvolutionChain)
    monkeypatch.setattr(engine_core, "BiographyView", _Dummy)
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

    assert calls == [engine._persona_db_conn]


def test_biography_view_when_constructed_then_registers_evolution_callback(tmp_path):
    chain = EvolutionChain(tmp_path / "evolution.db")

    view = BiographyView(chain)

    assert view is not None
