"""Test configuration and fixtures."""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Any

import pytest

# 为了能够导入项目根目录的 main.py，需要添加项目根目录到 Python 路径
project_root = Path(__file__).parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))


# ---------------------------------------------------------------------------
# RAM-based tmp_path for faster I/O
# ---------------------------------------------------------------------------


@pytest.fixture
def ram_tmp_path(tmp_path: Path) -> Path:
    """RAM-based temp directory for I/O-heavy tests.

    Uses pytest's tmp_path (respecting basetemp) so that all temp files
    land in the project-local ``.pytest_tmp`` directory rather than
    ``%TEMP%`` where Windows Defender real-time scanning adds ~20s per call.

    Falls back to RAM disk if available; otherwise returns tmp_path directly.
    """
    import os

    ram_paths = ["R:\\", "T:\\"]
    for ram in ram_paths:
        if os.path.exists(ram):
            import tempfile
            with tempfile.TemporaryDirectory(dir=ram) as td:
                yield Path(td)
            return

    yield tmp_path


# ---------------------------------------------------------------------------
# Session-scoped engine factory for speed
# ---------------------------------------------------------------------------


class _EnginePool:
    """Pool of pre-created EmotionalGroupChatEngine instances for reuse.

    Engines are created once per session and reset between tests to avoid
    state leakage while skipping expensive __init__ overhead.
    """

    _instances: dict[str, Any] = {}

    @classmethod
    def get_or_create(cls, key: str, factory) -> Any:
        if key not in cls._instances:
            cls._instances[key] = factory()
        return cls._instances[key]

    @classmethod
    def reset_engine(cls, engine: Any) -> None:
        """Reset mutable state to make engine safe for next test."""
        engine._group_last_message_at.clear()
        engine._transcripts.clear()
        engine._last_reply_at.clear()
        engine._last_reply_depth.clear()
        engine._proactive_enabled_groups.clear()
        engine._proactive_disabled_groups.clear()
        engine._last_proactive_at.clear()
        engine.token_usage_records.clear()
        engine.basic_memory._windows.clear()
        engine.basic_memory._heat_state.clear()
        engine.user_manager.entries.clear()
        engine.user_manager._global_users.clear()
        engine.user_manager._speaker_index.clear()
        engine.user_manager._identity_index.clear()
        engine.delayed_queue._queues.clear()
        if hasattr(engine.event_bus, "_subscribers"):
            engine.event_bus._subscribers.clear()
        if hasattr(engine.event_bus, "_closed"):
            engine.event_bus._closed = False


@pytest.fixture(scope="session")
def _session_engine_pool():
    """Internal session-scoped pool holder."""
    yield _EnginePool


@pytest.fixture
def engine_factory(tmp_path, _session_engine_pool):
    """Return a factory that yields resettable engines.

    Usage::

        async def test_something(engine_factory):
            engine = engine_factory()
            ...
    """

    def _make(
        *,
        provider_async=None,
        persona=None,
        config=None,
    ):
        from sirius_chat.core.emotional_engine import EmotionalGroupChatEngine
        from sirius_chat.models.persona import PersonaProfile
        from sirius_chat.providers.mock import MockProvider

        if persona is None:
            persona = PersonaProfile(name="TestBot")
        if config is None:
            config = {"sensitivity": 0.0}

        key = f"{persona.name}:{id(provider_async)}"

        def _create():
            return EmotionalGroupChatEngine(
                work_path=tmp_path,
                persona=persona,
                provider_async=provider_async,
                config=config,
            )

        engine = _session_engine_pool.get_or_create(key, _create)
        _session_engine_pool.reset_engine(engine)
        return engine

    return _make
