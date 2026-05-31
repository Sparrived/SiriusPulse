"""技能执行器测试。"""
from __future__ import annotations

from pathlib import Path

from sirius_pulse.skills import (
    SkillDefinition,
    SkillExecutor,
    SkillInvocationContext,
    SkillParameter,
)


def _make_skill(name: str, desc: str, run_func, params=None):
    return SkillDefinition(
        name=name,
        description=desc,
        parameters=params or [],
        source_path=None,
        _run_func=run_func,
    )


def test_execute_sync_skill(tmp_path: Path):
    """同步技能执行——参数正确传递。"""

    def my_run(name: str = "世界", **kwargs):
        return {"success": True, "text": f"你好，{name}！"}

    param_def = SkillParameter(
        name="name",
        type="str",
        description="要打招呼的人",
        required=False,
        default="世界",
    )
    skill = _make_skill("greet", "打招呼", my_run, params=[param_def])
    executor = SkillExecutor(work_path=tmp_path)
    executor.set_chat_context(group_id="g1", user_id="u1")

    result = executor.execute(
        skill=skill,
        params={"name": "小明"},
        invocation_context=SkillInvocationContext(caller=None),
    )
    assert result.success
    assert "小明" in result.to_display_text()


def test_execute_with_default_params(tmp_path: Path):
    """参数有默认值时正确执行。"""

    def my_run(count: int = 3, **kwargs):
        return {"success": True, "text": f"共{count}条"}

    param_def = SkillParameter(
        name="count",
        type="int",
        description="数量",
        required=False,
        default=3,
    )
    skill = _make_skill("count", "计数", my_run, params=[param_def])
    executor = SkillExecutor(work_path=tmp_path)
    executor.set_chat_context(group_id="g1", user_id="u1")

    result = executor.execute(
        skill=skill,
        params={},
        invocation_context=SkillInvocationContext(caller=None),
    )
    assert result.success
    assert "共3条" in result.to_display_text()


def test_execute_with_data_store(tmp_path: Path):
    """技能执行时 data_store 注入。"""

    def my_run(data_store=None, **kwargs):
        data_store.set("called", True)
        return {"success": True, "text": "ok"}

    skill = _make_skill("store_test", "存储测试", my_run)
    executor = SkillExecutor(work_path=tmp_path)
    executor.set_chat_context(group_id="g1", user_id="u1")

    result = executor.execute(
        skill=skill,
        params={},
        invocation_context=SkillInvocationContext(caller=None),
    )
    assert result.success

    store = executor.get_data_store("store_test")
    assert store.get("called") is True


def test_execute_failure(tmp_path: Path):
    """技能执行失败场景。"""

    def my_run(**kwargs):
        raise ValueError("模拟错误")

    skill = _make_skill("failing", "会失败的技能", my_run)
    executor = SkillExecutor(work_path=tmp_path)
    executor.set_chat_context(group_id="g1", user_id="u1")

    result = executor.execute(
        skill=skill,
        params={},
        max_retries=0,
        invocation_context=SkillInvocationContext(caller=None),
    )
    assert not result.success
    assert result.error
