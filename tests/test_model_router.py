"""模型路由在业务任务调度中的行为测试。

路由的职责已收窄为「把任务名原样交给 AMKR，并带上本地超时/重试」：模型与采样
参数由 AMKR 的任务定义决定。
"""

from __future__ import annotations

import pytest

from sirius_pulse.core.model_router import _DEFAULT_TASK_REGISTRY, ModelRouter, TaskConfig


def test_model_router_when_generating_final_reply_then_uses_that_task_name():
    """发往 AMKR 的 model 就是任务名，AMKR 据此查表换成真实模型。"""
    config = ModelRouter().resolve("response_generate")

    assert config.model_name == "response_generate"
    assert config.timeout == 30.0


@pytest.mark.parametrize("task_name", sorted(_DEFAULT_TASK_REGISTRY))
def test_model_router_when_any_known_task_then_model_is_the_task_name(task_name):
    """每个认知任务都原样发出任务名，不掺入任何本地模型选择。"""
    config = ModelRouter().resolve(task_name)

    assert config.model_name == task_name
    assert config.timeout > 0
    assert config.retries >= 0


def test_model_router_when_task_is_unknown_then_name_is_still_passed_through():
    """未注册的任务名照原样传出：AMKR 侧可能正是按这个名字建的任务。"""
    config = ModelRouter().resolve("unknown_business_task")

    assert config.model_name == "unknown_business_task"
    # 兜底任务的本地超时被借用，避免调用方拿到 0。
    assert config.timeout == ModelRouter().resolve("response_generate").timeout


def test_model_router_when_operator_sets_task_retries_then_value_is_respected():
    """重试是本地传输层关注点，运维可以按任务覆盖。"""
    router = ModelRouter(overrides={"memory_extract": {"retries": 3}})

    assert router.resolve("memory_extract").retries == 3


def test_model_router_when_operator_sets_task_timeout_then_value_is_respected():
    router = ModelRouter(overrides={"response_generate": {"timeout": 60.0}})

    assert router.resolve("response_generate").timeout == 60.0


def test_model_router_when_override_tries_to_pick_model_then_it_is_ignored():
    """模型不属于本地配置：即便传进来也不该改变发出的任务名。"""
    router = ModelRouter(overrides={"response_generate": {"model_name": "custom-model"}})

    assert router.resolve("response_generate").model_name == "response_generate"


def test_model_router_when_memory_extraction_runs_then_json_budget_is_reserved():
    config = ModelRouter().resolve("memory_extract")

    assert config.max_tokens == 4096
    assert config.retries == 0


def test_model_router_when_custom_business_task_is_registered_then_it_resolves():
    router = ModelRouter(
        task_registry={
            "support_triage": TaskConfig(
                model_name="support_triage",
                temperature=0.1,
                max_tokens=800,
                timeout=12.0,
            )
        }
    )

    config = router.resolve("support_triage")

    assert config.model_name == "support_triage"
    assert config.timeout == 12.0
    assert router.list_tasks() == ["support_triage"]


def test_model_router_when_urgency_is_high_then_model_is_not_locally_escalated():
    """紧急度不再本地换模型：换哪个模型是 AMKR 的事，本地换了反而查不到任务。"""
    router = ModelRouter()

    normal = router.resolve("cognition_analyze", urgency=50)
    urgent = router.resolve("cognition_analyze", urgency=85)
    critical = router.resolve("cognition_analyze", urgency=96)

    assert normal.model_name == urgent.model_name == critical.model_name == "cognition_analyze"


def test_known_task_registry_when_inspected_then_holds_only_task_names():
    """注册表里不留任何具体模型名，避免又出现一套「本地真相」。"""
    model_names = {cfg.model_name for cfg in _DEFAULT_TASK_REGISTRY.values()}

    assert model_names == set(_DEFAULT_TASK_REGISTRY)


def _task_names_sent_by_the_code() -> dict[str, set[str]]:
    """源码里作为 ``task_name`` 默认值出现的任务名 → 用到它的文件。

    连源码一起扫，是因为这里的失效模式不是写错字，而是**新增了任务名却忘了登记**：
    注册表同时就是发往 AMKR 的名字清单，漏一个，那个任务在 AMKR 侧就不存在。
    """
    import ast
    from pathlib import Path

    package = Path(__file__).resolve().parent.parent / "sirius_pulse"
    found: dict[str, set[str]] = {}
    for path in package.rglob("*.py"):
        # utf-8-sig：仓库里有个别文件带 BOM，裸 utf-8 读进来会让 ast.parse 抛错。
        tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            args = node.args
            positional = args.posonlyargs + args.args
            # defaults 只对齐最后 len(defaults) 个位置参数。
            offset = len(positional) - len(args.defaults)
            paired = list(zip(positional[offset:], args.defaults))
            for arg, default in paired + list(zip(args.kwonlyargs, args.kw_defaults)):
                if arg.arg != "task_name":
                    continue
                if isinstance(default, ast.Constant) and isinstance(default.value, str):
                    if default.value.strip():
                        found.setdefault(default.value, set()).add(path.name)
    return found


def test_every_task_name_sent_by_the_code_is_registered():
    """凡是被当 ``task_name`` 发出去的名字，都必须出现在注册表里。

    不变量：注册表 == 发往 AMKR 的名字清单。曾经 ``autonomy_generate`` 只在前者
    缺席、后者出现，于是自主回合每一次都 HTTP 404（AMKR 把它当真实模型名去查），
    自主行为在线上整整两天一次都没成功过——而所有单测都是绿的。
    """
    sent = _task_names_sent_by_the_code()
    # 守卫自身要有效：至少得扫到几个已知任务名，否则规则被改坏了也不会有人发现。
    assert {"response_generate", "passive_tool", "autonomy_generate"} <= set(sent)

    unregistered = {
        name: sorted(files) for name, files in sent.items() if name not in _DEFAULT_TASK_REGISTRY
    }

    assert not unregistered, (
        f"这些任务名会被发往 AMKR 但没有登记：{unregistered}；"
        f"请在 _DEFAULT_TASK_REGISTRY 中补上，否则 AMKR 会当成模型名而 404。"
    )


def test_autonomy_turn_uses_a_registered_task_name():
    """自主回合的任务名必须已登记——它只靠背景心跳驱动，失败时没有对话可暴露。"""
    sent = _task_names_sent_by_the_code()

    # 引擎实际发出的名字（扫源码取得，不在这里复制一份常量）。
    assert "autonomy_generate" in sent
    assert "autonomy_generate" in _DEFAULT_TASK_REGISTRY
    assert ModelRouter().resolve("autonomy_generate").model_name == "autonomy_generate"
