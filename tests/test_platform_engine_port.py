"""平台层与引擎之间只走 ``platforms/engine_port.py`` 里登记过的接口。

两头各一道闸：真实引擎必须实现接口里的每个成员（引擎侧改名立刻失败），平台代码
也只能用接口里登记过的成员（平台侧不能再按名字摸引擎的私有状态）。以前两边靠
``getattr(engine, "_xxx", 默认值)`` 约定，任何一边改名都只会悄悄退化成默认值。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import pytest_asyncio

from sirius_pulse.core.emotional_engine import EmotionalGroupChatEngine
from sirius_pulse.models.persona import PersonaProfile
from sirius_pulse.platforms.engine_port import ManagedEngine, PlatformEngine, protocol_members
from sirius_pulse.providers.mock import MockProvider

PLATFORMS = Path(__file__).resolve().parent.parent / "sirius_pulse" / "platforms"
_ENGINE_MEMBER = re.compile(
    r"\b(?:self\._engine|engine)\.([A-Za-z_]\w*)"
    r"|\b(?:getattr|hasattr|setattr)\(\s*(?:self\._engine|engine)\s*,\s*\"(\w+)\""
)


@pytest_asyncio.fixture
async def engine(tmp_path):
    engine = EmotionalGroupChatEngine(
        work_path=tmp_path,
        provider_async=MockProvider(["好"]),
        persona=PersonaProfile(name="月白", full_system_prompt="你是月白。"),
        config={
            "enable_tools": False,
            "task_enabled": {"memory_extract": False, "event_extract": False},
        },
    )
    try:
        yield engine
    finally:
        engine.stop_background_tasks()
        await engine.event_bus.close()
        engine.cognition_store.close()
        engine.user_manager.close()
        engine._memory_storage.close()


@pytest.mark.asyncio
async def test_the_real_engine_provides_everything_the_platform_layer_relies_on(engine):
    missing = sorted(name for name in protocol_members(ManagedEngine) if not hasattr(engine, name))
    assert missing == [], f"引擎缺少平台层接口成员（改名了？）：{missing}"
    assert isinstance(engine, PlatformEngine)
    assert isinstance(engine, ManagedEngine)


@pytest.mark.asyncio
async def test_retiring_an_engine_is_visible_through_the_public_interface(engine):
    assert engine.is_ready() and not engine.runtime_retiring
    engine.runtime_retiring = True
    assert engine.runtime_retiring and not engine.is_ready()


def test_platform_code_only_uses_engine_members_declared_in_the_interface():
    undeclared: dict[str, list[str]] = {}
    for path in PLATFORMS.rglob("*.py"):
        if path.name == "engine_port.py":
            continue
        # 运行时负责构建与启停引擎，可以用生命周期成员；适配器只能用平台接口。
        allowed = protocol_members(ManagedEngine if path.name == "runtime.py" else PlatformEngine)
        used = {
            match.group(1) or match.group(2)
            for match in _ENGINE_MEMBER.finditer(path.read_text(encoding="utf-8"))
        }
        names = sorted(used - allowed)
        if names:
            undeclared[str(path.relative_to(PLATFORMS))] = names

    assert undeclared == {}, (
        "平台代码用到了 engine_port 里没登记的引擎成员；私有成员请改走公开方法，"
        f"新的能力请先登记进 PlatformEngine / ManagedEngine：{undeclared}"
    )
