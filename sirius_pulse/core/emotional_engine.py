"""EmotionalGroupChatEngine: backward-compatible shim.

All implementation has been split into:
  - engine_core   : class definition, __init__, public API, persistence, _generate
  - pipeline      : pipeline stages (组合模式)
  - bg_tasks      : background tasks (组合模式)
  - helpers       : utility methods (组合模式)

This module re-exports the combined class so existing imports continue to work.
"""

from __future__ import annotations

from typing import Any


class EmotionalGroupChatEngine:
    """Combined EmotionalGroupChatEngine with all components.

    所有组件已通过组合模式集成到基类中：
    - engine._helpers: Helpers 组件
    - engine._bg_tasks_mgr: BackgroundTasks 组件
    - engine._pipeline: Pipeline 组件
    不再需要通过继承 Mixin 方式集成。
    """

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


__all__ = ["EmotionalGroupChatEngine", "create_emotional_engine"]
