"""Model router: task-aware LLM model selection for v0.28+.

Maps cognitive tasks to optimal (model, temperature, max_tokens, timeout)
configurations. Supports dynamic escalation (high-urgency → stronger model)
and user-defined overrides via engine config.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class TaskConfig:
    """Configuration for a specific cognitive task."""

    model_name: str
    temperature: float
    max_tokens: int
    timeout: float = 30.0
    fallback_model: str | None = None


# ---------------------------------------------------------------------------
# Default task registry
# ---------------------------------------------------------------------------

_DEFAULT_TASK_REGISTRY: dict[str, TaskConfig] = {
    # Lightweight tasks → fast/cheap models
    "cognition_analyze": TaskConfig(
        model_name="gpt-4o-mini",
        temperature=0.3,
        max_tokens=512,
        timeout=15.0,
        fallback_model="deepseek-chat",
    ),
    "memory_extract": TaskConfig(
        model_name="gpt-4o-mini",
        temperature=0.3,
        max_tokens=512,
        timeout=20.0,
        fallback_model="deepseek-chat",
    ),
    # High-quality tasks → stronger models
    "response_generate": TaskConfig(
        model_name="gpt-4o",
        temperature=0.7,
        max_tokens=4096,
        timeout=30.0,
        fallback_model="deepseek-reasoner",
    ),
    "proactive_generate": TaskConfig(
        model_name="gpt-4o",
        temperature=0.8,
        max_tokens=1024,
        timeout=20.0,
        fallback_model="deepseek-chat",
    ),
    # Plugin 分析任务 → 小模型
    "plugin_analyze": TaskConfig(
        model_name="gpt-4o-mini",
        temperature=0.5,
        max_tokens=1024,
        timeout=30.0,
        fallback_model="deepseek-chat",
    ),
}
# Urgency thresholds for escalation
_URGENCY_ESCALATE = 80  # urgency > 80 → use stronger model
_URGENCY_CRITICAL = 95  # urgency > 95 → strongest model + more tokens


class ModelRouter:
    """Routes cognitive tasks to appropriate LLM configurations.

    Usage::

        router = ModelRouter()
        cfg = router.resolve("response_generate", urgency=85)
        # cfg.model_name == "gpt-4o" (escalated from default)
    """

    def __init__(
        self,
        task_registry: dict[str, TaskConfig] | None = None,
        overrides: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        """Initialize router.

        Args:
            task_registry: Full task→config mapping. If None, uses defaults.
            overrides: Partial overrides per task (e.g.
                {"response_generate": {"temperature": 0.5}}).
        """
        self._registry: dict[str, TaskConfig] = dict(task_registry or _DEFAULT_TASK_REGISTRY)
        if overrides:
            for task_name, patch in overrides.items():
                if task_name in self._registry:
                    base = self._registry[task_name]
                    self._registry[task_name] = TaskConfig(
                        model_name=patch.get("model_name", base.model_name),
                        temperature=patch.get("temperature", base.temperature),
                        max_tokens=patch.get("max_tokens", base.max_tokens),
                        timeout=patch.get("timeout", base.timeout),
                        fallback_model=patch.get("fallback_model", base.fallback_model),
                    )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def resolve(
        self,
        task_name: str,
        *,
        urgency: int = 0,
        heat_level: str = "warm",
    ) -> TaskConfig:
        """Resolve the best config for a task, considering urgency and context.

        Escalation rules:
            - urgency > 80: upgrade to stronger model, lower temperature
            - urgency > 95: strongest model, more tokens

        heat_level 不再影响 max_tokens：长度控制已移至 StyleAdapter 的 prompt 指令层，
        避免在 SKILL 调用场景下因 token 预算不足导致技能标记被截断。
        """
        base = self._registry.get(task_name)
        if base is None:
            base = self._registry.get("response_generate", TaskConfig(
                model_name="gpt-4o", temperature=0.7, max_tokens=512
            ))

        model = base.model_name
        temperature = base.temperature
        max_tokens = base.max_tokens
        timeout = base.timeout
        fallback = base.fallback_model

        # Urgency escalation
        if urgency > _URGENCY_CRITICAL:
            model = self._stronger_model(model)
            temperature = max(0.1, temperature - 0.3)
            max_tokens = max(max_tokens, min(8192, int(max_tokens * 1.3)))
        elif urgency > _URGENCY_ESCALATE:
            model = self._stronger_model(model)
            temperature = max(0.2, temperature - 0.2)
            max_tokens = max(max_tokens, min(4096, int(max_tokens * 1.1)))

        return TaskConfig(
            model_name=model,
            temperature=round(temperature, 2),
            max_tokens=max_tokens,
            timeout=timeout,
            fallback_model=fallback,
        )

    def get_fallback(self, task_name: str) -> TaskConfig | None:
        """Get fallback config for a task."""
        base = self._registry.get(task_name)
        if base and base.fallback_model:
            return TaskConfig(
                model_name=base.fallback_model,
                temperature=base.temperature,
                max_tokens=base.max_tokens,
                timeout=base.timeout,
            )
        return None

    def list_tasks(self) -> list[str]:
        """Return all registered task names."""
        return list(self._registry.keys())

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _stronger_model(current: str) -> str:
        """Return a stronger model for escalation.

        Simple tier mapping; real deployment should use provider registry.
        """
        tiers: dict[str, str] = {
            "gpt-4o-mini": "gpt-4o",
            "gpt-4o": "gpt-4o-2024-08-06",  # latest snapshot
            "deepseek-chat": "deepseek-reasoner",
            "deepseek-reasoner": "deepseek-chat",  # no stronger known
            "qwen-turbo": "qwen-max",
            "qwen-max": "qwen-max-longcontext",
            "claude-3-haiku": "claude-3-sonnet",
            "claude-3-sonnet": "claude-3-opus",
        }
        return tiers.get(current, current)
