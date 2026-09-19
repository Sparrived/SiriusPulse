"""Model router: task-aware LLM routing for v0.28+.

把认知任务映射到它们的 **AMKR 任务名**。模型选择、采样参数、故障回退都由 AMKR
的任务定义决定，本框架只负责说明「这是哪个任务」。

因此 ``resolve()`` 返回的 ``model_name`` 就是任务名本身：它作为 ``model`` 字段发往
AMKR，由 AMKR 查表换成真实模型。这里保留的其余字段（``timeout`` / ``retries``）
是**本地传输层**的关注点，与模型无关。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class TaskConfig:
    """Configuration for a specific cognitive task."""

    model_name: str
    temperature: float
    max_tokens: int
    timeout: float = 30.0
    fallback_model: str | None = None
    retries: int = 1


# ---------------------------------------------------------------------------
# Default task registry
# ---------------------------------------------------------------------------

# 认知任务名清单。这些名字同时是：
#   1. 发往 AMKR 的 ``model`` 字段（AMKR 用它查任务定义）；
#   2. 本框架向 AMKR 注册的任务名。
# 每条记录只带**本地**关注点（超时、重试）与预算估算用的默认值；模型与采样参数
# 不由这里决定。
_DEFAULT_TASK_REGISTRY: dict[str, TaskConfig] = {
    "cognition_analyze": TaskConfig(
        model_name="cognition_analyze",
        temperature=0.3,
        max_tokens=1024,
        timeout=15.0,
    ),
    "memory_extract": TaskConfig(
        model_name="memory_extract",
        temperature=0.3,
        # 结构化记忆抽取要留够空间写完 JSON；1024 很容易被大会话批次吃满。
        max_tokens=4096,
        timeout=30.0,
        retries=0,
    ),
    "response_generate": TaskConfig(
        model_name="response_generate",
        temperature=0.7,
        max_tokens=4096,
        timeout=30.0,
    ),
    "proactive_generate": TaskConfig(
        model_name="proactive_generate",
        temperature=0.8,
        max_tokens=1024,
        timeout=20.0,
    ),
    "plugin_analyze": TaskConfig(
        model_name="plugin_analyze",
        temperature=0.5,
        max_tokens=1024,
        timeout=30.0,
    ),
    "plugin_generate": TaskConfig(
        model_name="plugin_generate",
        temperature=0.7,
        max_tokens=4096,
        timeout=30.0,
    ),
    "plugin_render": TaskConfig(
        model_name="plugin_render",
        temperature=0.7,
        max_tokens=2048,
        timeout=30.0,
    ),
    "plugin_raw": TaskConfig(
        model_name="plugin_raw",
        temperature=0.5,
        max_tokens=2048,
        timeout=30.0,
    ),
    "passive_tool": TaskConfig(
        model_name="passive_tool",
        temperature=0.8,
        max_tokens=1024,
        timeout=20.0,
    ),
    "diary_generate": TaskConfig(
        model_name="diary_generate",
        temperature=0.5,
        max_tokens=512,
        timeout=20.0,
    ),
    "topic_cluster": TaskConfig(
        model_name="topic_cluster",
        temperature=0.3,
        max_tokens=1024,
        timeout=20.0,
    ),
    "diary_consolidate": TaskConfig(
        model_name="diary_consolidate",
        temperature=0.4,
        max_tokens=2048,
        timeout=30.0,
    ),
}

# 兜底任务：未注册的任务名按它的超时/重试处理。
_FALLBACK_TASK = "response_generate"


class ModelRouter:
    """Routes cognitive tasks to their AMKR task definitions.

    Usage::

        router = ModelRouter()
        cfg = router.resolve("response_generate")
        # cfg.model_name == "response_generate"（即 AMKR 的任务名）
    """

    def __init__(
        self,
        task_registry: dict[str, TaskConfig] | None = None,
        overrides: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        """Initialize router.

        Args:
            task_registry: Full task→config mapping. If None, uses defaults.
            overrides: Partial overrides per task, 只支持本地字段
                （``timeout`` / ``retries``）。
        """
        self._registry: dict[str, TaskConfig] = dict(task_registry or _DEFAULT_TASK_REGISTRY)
        if overrides:
            for task_name, patch in overrides.items():
                if task_name in self._registry:
                    base = self._registry[task_name]
                    self._registry[task_name] = TaskConfig(
                        model_name=base.model_name,
                        temperature=base.temperature,
                        max_tokens=base.max_tokens,
                        timeout=patch.get("timeout", base.timeout),
                        fallback_model=base.fallback_model,
                        retries=patch.get("retries", base.retries),
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
        """Resolve the config for a task.

        ``urgency`` 与 ``heat_level`` 保留在签名里以兼容既有调用方，但不再影响
        结果：模型与采样参数的调整权在 AMKR，本框架不按本地启发式换模型。
        """
        base = self._registry.get(task_name) or self._registry.get(_FALLBACK_TASK)
        if base is None:
            return TaskConfig(model_name=task_name, temperature=0.7, max_tokens=512, timeout=30.0)
        # model_name 始终用调用方给的任务名：AMKR 侧可能正是按这个名字建的任务，
        # 换成兜底名会让它查不到任务定义。
        return TaskConfig(
            model_name=task_name,
            temperature=base.temperature,
            max_tokens=base.max_tokens,
            timeout=base.timeout,
            retries=base.retries,
        )

    def list_tasks(self) -> list[str]:
        """Return all registered task names."""
        return list(self._registry.keys())
