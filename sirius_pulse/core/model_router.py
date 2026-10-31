"""Model router: task-aware LLM routing for v0.28+.

把认知任务映射到它们的 **AMKR 任务名**。模型选择、采样参数、故障回退都由 AMKR
的任务定义决定，本框架只负责说明「这是哪个任务」。

因此 ``resolve()`` 返回的 ``model_name`` 就是任务名本身：它作为 ``model`` 字段发往
AMKR，由 AMKR 查表换成真实模型。这里保留的其余字段（``timeout`` / ``retries`` /
``fallback_model_name``）是**本地传输层**的关注点，与模型无关。

唯一的例外是 ``fallback_model_name``：AMKR 的故障回退按 HTTP 状态触发，而 Gemini
的内容策略拦截走的是「HTTP 200 + 拒绝文案当正文」，AMKR 看不到这种失败。因此由
Brain 识别后改用备选**任务名**重发——备选名对应的仍是 AMKR 的任务定义，选哪个
模型依旧由 AMKR 决定，本框架不碰具体模型。

采样参数（``temperature`` / ``max_tokens``）**不属于本框架**。AMKR 的任务定义里
已经固定了它们；调用方再显式传一份会与任务定义冲突，AMKR 对此直接回 400（而不是
静默覆盖），这是正确行为。需要调整取值时应当改 AMKR 侧的固定值，而不是在这里
重开一条本地真相或做回退。
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any


@dataclass(frozen=True, slots=True)
class TaskConfig:
    """Configuration for a specific cognitive task.

    只承载本地传输层参数：``model_name`` 是发往 AMKR 的任务名，``timeout`` /
    ``retries`` 是本地超时与重试，``fallback_model_name`` 是内容拦截时改用的
    备选任务名。采样参数由 AMKR 的任务定义负责，这里不保存，也就不可能被回传
    到请求里。
    """

    model_name: str
    timeout: float = 30.0
    retries: int = 1
    #: 主任务命中「上游内容策略拦截」时改用的 AMKR 任务名；空串表示没有备选。
    fallback_model_name: str = ""


# ---------------------------------------------------------------------------
# Default task registry
# ---------------------------------------------------------------------------

# 认知任务名清单。这些名字同时是：
#   1. 发往 AMKR 的 ``model`` 字段（AMKR 用它查任务定义）；
#   2. 本框架向 AMKR 注册的任务名。
# 每条记录只带**本地**关注点（超时、重试）；模型与采样参数不由这里决定。
_DEFAULT_TASK_REGISTRY: dict[str, TaskConfig] = {
    "cognition_analyze": TaskConfig(
        model_name="cognition_analyze",
        timeout=15.0,
    ),
    "memory_extract": TaskConfig(
        model_name="memory_extract",
        timeout=30.0,
        retries=0,
    ),
    "response_generate": TaskConfig(
        model_name="response_generate",
        timeout=30.0,
        fallback_model_name="fallback_generate",
    ),
    # 内容策略拦截时的兜底任务名。AMKR 侧把它指向一个不受 Gemini 内容过滤影响的
    # 模型（生产是 deepseek-v4.1-flash）。它只在 Brain 识别到「200 + 拒绝文案」时
    # 被使用，正常聊天不会走到；不绑定模型时命中拦截只会失败，不会发错内容。
    "fallback_generate": TaskConfig(
        model_name="fallback_generate",
        timeout=30.0,
    ),
    # 工作模式专用任务名：多步工具协作一轮要读的上下文更多、也想用更强的模型，
    # 因此在 AMKR 面板里可以单独把它指向另一个模型，不影响普通聊天。
    "work_mode_generate": TaskConfig(
        model_name="work_mode_generate",
        timeout=60.0,
        fallback_model_name="fallback_generate",
    ),
    # 插件侧的一切生成（分析、人格化渲染、绕过管线的原生调用）共用一个任务名：
    # 它们原先各留一个名字只是为了分开调模型，实际运维里从来绑的是同一个。
    "plugin_generate": TaskConfig(
        model_name="plugin_generate",
        timeout=30.0,
    ),
    "passive_tool": TaskConfig(
        model_name="passive_tool",
        timeout=20.0,
    ),
    # 自主时间（autonomy tick）的回合。单列一个任务名，是因为它和聊天回合的性格
    # 不同：她要在这里自己找事做、写点东西，可能想用更便宜或更强的模型，也可能
    # 想关掉它。**必须留在本表里**——本表同时就是向 AMKR 注册的任务名清单，
    # 漏掉它这个任务在 AMKR 侧不存在，每次自主回合都会 404（见下方守护测试）。
    "autonomy_generate": TaskConfig(
        model_name="autonomy_generate",
        timeout=30.0,
        fallback_model_name="fallback_generate",
    ),
    # 生图任务：发往 AMKR 的 /v1/images/* 端点，必须由运维在 AMKR 面板里绑定
    # 一个真正的生图模型（不绑就是 404）。超时给足，生图 30–300 秒都算正常。
    "image_generate": TaskConfig(
        model_name="image_generate",
        timeout=300.0,
        retries=0,
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
                （``timeout`` / ``retries``）；其余键一律忽略。
        """
        self._registry: dict[str, TaskConfig] = dict(task_registry or _DEFAULT_TASK_REGISTRY)
        if overrides:
            for task_name, patch in overrides.items():
                base = self._registry.get(task_name)
                if base is None:
                    continue
                self._registry[task_name] = replace(
                    base,
                    timeout=patch.get("timeout", base.timeout),
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

        ``model_name`` 始终用调用方给的任务名：AMKR 侧可能正是按这个名字建的任务，
        换成兜底名会让它查不到任务定义。未登记的任务名只借用兜底任务的本地
        超时/重试，不借用任何采样参数（本框架已不再持有它们）。
        ``fallback_model_name`` 原样保留：它是命中上游内容拦截时的备选任务名。
        """
        base = self._registry.get(task_name) or self._registry.get(_FALLBACK_TASK)
        if base is None:
            return TaskConfig(model_name=task_name, timeout=30.0)
        return replace(base, model_name=task_name)

    def list_tasks(self) -> list[str]:
        """Return all registered task names."""
        return list(self._registry.keys())
