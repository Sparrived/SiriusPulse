"""EmotionalGroupChatEngine: backward-compatible shim.

All implementation has been split into:
  - engine_core   : class definition, __init__, public API, persistence, _generate
  - pipeline      : Perception → Cognition → Decision → Execution → BackgroundUpdate
  - bg_tasks      : background tasks, proactive checks, reminders, delayed queue, prompt builders
  - helpers       : utility methods, token recording, exception classification

This module re-exports the combined class so existing imports continue to work.
"""

from __future__ import annotations

from typing import Any

from sirius_chat.core.engine_core import (
    _EmotionalGroupChatEngineBase,
)
from sirius_chat.core.pipeline import PipelineMixin
from sirius_chat.core.bg_tasks import BackgroundTasksMixin
from sirius_chat.skills.builtin.reminder import _is_reminder_due
from sirius_chat.core.helpers import HelpersMixin


class EmotionalGroupChatEngine(
    _EmotionalGroupChatEngineBase,
    PipelineMixin,
    BackgroundTasksMixin,
    HelpersMixin,
):
    """Combined EmotionalGroupChatEngine with all mixins."""

    pass


def create_emotional_engine(
    work_path: Any,
    *,
    provider: Any | None = None,
    persona: Any | None = None,
    config: dict[str, Any] | None = None,
    vector_store: Any | None = None,
    embedding_client: Any | None = None,
) -> "EmotionalGroupChatEngine":
    """Factory for EmotionalGroupChatEngine (v0.28+).

    Args:
        work_path: Workspace path for persistence.
        provider: Optional LLM provider for async generation tasks.
        persona: Optional PersonaProfile or string archetype name.
        config: Optional engine configuration dict.
        vector_store: Optional DiaryVectorStore for persistent embeddings.
        embedding_client: Optional EmbeddingClient for shared embedding service.

    Returns:
        Configured EmotionalGroupChatEngine instance.
    """
    provider_async = provider if provider is None or hasattr(provider, "generate_async") else None
    return EmotionalGroupChatEngine(
        work_path=work_path,
        provider_async=provider_async,
        persona=persona,
        config=config,
        vector_store=vector_store,
        embedding_client=embedding_client,
    )


# Re-export internal helpers for backward compatibility
__all__ = ["EmotionalGroupChatEngine", "create_emotional_engine", "_is_reminder_due"]
