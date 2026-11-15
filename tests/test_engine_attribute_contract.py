"""引擎上被别处按名字读取的私有属性，必须真的在引擎上。

适配器、工具上下文、后台任务大量用 ``getattr(engine, "_xxx", 默认值)`` 读引擎状态。
改名不会报错，只会让读取方悄悄拿到默认值——例如 ``_runtime_retiring`` 改名后，
正在退役的引擎会继续往群里发消息。这里把源码里所有这类读取收集起来，逐个确认
一台新建的引擎确实有这个属性。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import pytest_asyncio

from sirius_pulse.core.emotional_engine import EmotionalGroupChatEngine
from sirius_pulse.models.persona import PersonaProfile
from sirius_pulse.providers.mock import MockProvider

PACKAGE = Path(__file__).resolve().parent.parent / "sirius_pulse"
#: 指向引擎对象的常见写法：engine / _engine / self._engine / runtime.engine。
_ENGINE_ACCESS = re.compile(
    r"\b(?:getattr|hasattr|setattr)\(\s*(?:self\._engine|_engine|engine|self\.engine|"
    r"runtime\.engine|self\._runtime\.engine)\s*,\s*\"(_[A-Za-z0-9_]+)\""
)


def _private_names_read_by_name() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for path in PACKAGE.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for match in _ENGINE_ACCESS.finditer(text):
            found.setdefault(match.group(1), []).append(str(path.relative_to(PACKAGE)))
    return found


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
async def test_every_private_attribute_read_by_name_exists_on_the_engine(engine):
    names = _private_names_read_by_name()
    assert names, "没收集到任何按名字读取的属性，正则可能失效了"

    missing = {
        name: sorted(set(files)) for name, files in names.items() if not hasattr(engine, name)
    }

    hint = "这些属性被按名字读取，但新建的引擎上没有（改名了？还是只在别处临时挂上去？）"
    assert missing == {}, f"{hint}请在引擎 __init__ 里显式声明：{missing!r}"
