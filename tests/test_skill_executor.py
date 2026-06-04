"""技能执行器面向用户工具调用的业务行为测试。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sirius_pulse.memory.user.unified_models import UnifiedUser
from sirius_pulse.skills import (
    SkillDefinition,
    SkillExecutor,
    SkillInvocationContext,
    SkillParameter,
)


def _make_skill(
    name: str,
    run_func,
    *,
    description: str = "测试技能",
    params: list[SkillParameter] | None = None,
    developer_only: bool = False,
    adapter_types: list[str] | None = None,
    source_path: Path | None = None,
) -> SkillDefinition:
    return SkillDefinition(
        name=name,
        description=description,
        parameters=params or [],
        developer_only=developer_only,
        adapter_types=adapter_types or [],
        source_path=source_path,
        _run_func=run_func,
    )


def _context(user_id: str = "u1", *, is_developer: bool = False) -> SkillInvocationContext:
    return SkillInvocationContext(
        caller=UnifiedUser(
            user_id=user_id,
            name=user_id,
            metadata={"is_developer": is_developer},
        )
    )


def test_skill_executor_when_user_calls_skill_then_receives_display_text(tmp_path: Path):
    def run(name: str = "世界") -> dict[str, Any]:
        return {"success": True, "text": f"你好，{name}！"}

    skill = _make_skill(
        "greet",
        run,
        params=[
            SkillParameter(
                name="name",
                type="str",
                description="要问候的人",
                required=False,
                default="世界",
            )
        ],
    )
    executor = SkillExecutor(work_path=tmp_path)

    result = executor.execute(skill, {"name": "小明"}, invocation_context=_context())

    assert result.success is True
    assert result.to_display_text() == "你好，小明！"


def test_skill_executor_when_optional_param_is_missing_then_default_is_used(tmp_path: Path):
    def run(count: int = 3) -> dict[str, Any]:
        return {"success": True, "text": f"生成 {count} 条提醒"}

    skill = _make_skill(
        "reminder_count",
        run,
        params=[
            SkillParameter(name="count", type="int", description="数量", default=3),
        ],
    )
    executor = SkillExecutor(work_path=tmp_path)

    result = executor.execute(skill, {}, invocation_context=_context())

    assert result.success is True
    assert result.to_display_text() == "生成 3 条提醒"


def test_skill_executor_when_user_supplies_string_number_then_skill_receives_int(tmp_path: Path):
    seen: dict[str, Any] = {}

    def run(count: int) -> dict[str, Any]:
        seen["type"] = type(count)
        return {"success": True, "text": str(count + 1)}

    skill = _make_skill(
        "increment",
        run,
        params=[
            SkillParameter(name="count", type="int", description="数量", required=True),
        ],
    )
    executor = SkillExecutor(work_path=tmp_path)

    result = executor.execute(skill, {"count": "41"}, invocation_context=_context())

    assert result.to_display_text() == "42"
    assert seen["type"] is int


def test_skill_executor_when_required_param_is_missing_then_user_gets_failure(tmp_path: Path):
    def run(query: str) -> dict[str, Any]:
        return {"success": True, "text": query}

    skill = _make_skill(
        "search",
        run,
        params=[
            SkillParameter(name="query", type="str", description="搜索词", required=True),
        ],
    )
    executor = SkillExecutor(work_path=tmp_path)

    result = executor.execute(skill, {}, invocation_context=_context())

    assert result.success is False
    assert "query" in result.error


def test_skill_executor_when_skill_writes_store_then_data_persists_for_next_call(
    tmp_path: Path,
):
    def run(data_store=None) -> dict[str, Any]:
        data_store.set("last_city", "上海")
        return {"success": True, "text": "已记录"}

    executor = SkillExecutor(work_path=tmp_path)
    skill = _make_skill("weather_pref", run)

    result = executor.execute(skill, {}, invocation_context=_context())
    persisted_store = SkillExecutor(work_path=tmp_path).get_data_store("weather_pref")

    assert result.success is True
    assert persisted_store.get("last_city") == "上海"


def test_skill_executor_when_skill_accepts_chat_context_then_receives_current_chat(tmp_path: Path):
    seen: dict[str, Any] = {}

    def run(chat_context=None) -> dict[str, Any]:
        seen.update(chat_context)
        return {"success": True, "text": chat_context["chat_type"]}

    executor = SkillExecutor(work_path=tmp_path)
    executor.set_chat_context(group_id="private_qq_10001", user_id="u1")
    skill = _make_skill("where_am_i", run)

    result = executor.execute(skill, {}, invocation_context=_context())

    assert result.to_display_text() == "private"
    assert seen["chat_id"] == "10001"
    assert seen["is_private"] is True


def test_skill_executor_when_skill_accepts_engine_context_then_receives_it(tmp_path: Path):
    seen: dict[str, Any] = {}
    engine_context = object()

    def run(engine_context=None) -> dict[str, Any]:
        seen["engine_context"] = engine_context
        return {"success": True, "text": "ok"}

    executor = SkillExecutor(work_path=tmp_path)
    executor.set_engine_context(engine_context)
    builtin_path = Path(__file__).resolve().parents[1] / "sirius_pulse" / "skills" / "builtin" / "needs_engine.py"
    skill = _make_skill("needs_engine", run, source_path=builtin_path)

    result = executor.execute(skill, {}, invocation_context=_context())

    assert result.success is True
    assert seen["engine_context"] is engine_context


def test_skill_executor_when_workspace_skill_accepts_engine_context_then_it_is_not_injected(
    tmp_path: Path,
):
    seen: dict[str, Any] = {}
    engine_context = object()

    def run(engine_context=None) -> dict[str, Any]:
        seen["engine_context"] = engine_context
        return {"success": True, "text": "ok"}

    executor = SkillExecutor(work_path=tmp_path)
    executor.set_engine_context(engine_context)
    skill = _make_skill("workspace_needs_engine", run, source_path=tmp_path / "skills" / "x.py")

    result = executor.execute(skill, {}, invocation_context=_context())

    assert result.success is True
    assert seen["engine_context"] is None


@pytest.mark.asyncio
async def test_skill_executor_when_async_skill_accepts_engine_context_then_receives_it(
    tmp_path: Path,
):
    seen: dict[str, Any] = {}
    engine_context = object()

    async def run(engine_context=None) -> dict[str, Any]:
        seen["engine_context"] = engine_context
        return {"success": True, "text": "ok"}

    executor = SkillExecutor(work_path=tmp_path)
    executor.set_engine_context(engine_context)
    builtin_path = Path(__file__).resolve().parents[1] / "sirius_pulse" / "skills" / "builtin" / "async_needs_engine.py"
    skill = _make_skill("async_needs_engine", run, source_path=builtin_path)

    result = await executor.execute_async(skill, {}, invocation_context=_context())

    assert result.success is True
    assert seen["engine_context"] is engine_context


def test_skill_executor_when_normal_user_calls_developer_skill_then_access_is_denied(
    tmp_path: Path,
):
    def run() -> dict[str, Any]:
        return {"success": True, "text": "secret"}

    skill = _make_skill("server_shell", run, developer_only=True)
    executor = SkillExecutor(work_path=tmp_path)

    result = executor.execute(skill, {}, invocation_context=_context(is_developer=False))

    assert result.success is False
    assert "developer" in result.error.lower() or "开发" in result.error


@pytest.mark.asyncio
async def test_skill_executor_when_async_skill_finishes_then_result_is_returned(tmp_path: Path):
    async def run(name: str) -> dict[str, Any]:
        return {"success": True, "text": f"异步完成：{name}"}

    skill = _make_skill(
        "async_greet",
        run,
        params=[SkillParameter(name="name", type="str", description="名字", required=True)],
    )
    executor = SkillExecutor(work_path=tmp_path)

    result = await executor.execute_async(skill, {"name": "Alice"}, invocation_context=_context())

    assert result.success is True
    assert result.to_display_text() == "异步完成：Alice"


def test_skill_executor_when_skill_raises_error_then_failure_is_visible_to_model(
    tmp_path: Path,
):
    def run() -> dict[str, Any]:
        raise ValueError("模拟错误")

    executor = SkillExecutor(work_path=tmp_path)
    skill = _make_skill("failing", run)

    result = executor.execute(skill, {}, max_retries=0, invocation_context=_context())

    assert result.success is False
    assert "模拟错误" in result.to_display_text()
