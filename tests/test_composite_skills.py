from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sirius_pulse.skills.builtin import group_management, interaction
from sirius_pulse.skills.models import SkillResult


class _FakeContext:
    def __init__(self, target_names: list[str]) -> None:
        self.skill_registry = SimpleNamespace(
            get=lambda name: SimpleNamespace(name=name) if name in target_names else None
        )
        self.skill_executor = SimpleNamespace(
            execute_async=AsyncMock(return_value=SkillResult(success=True, data={"ok": True}))
        )


@pytest.mark.asyncio
async def test_interaction_routes_action_to_internal_skill_and_records_action():
    context = _FakeContext(["send_image"])

    result = await interaction.run(
        action="image",
        image_path="C:/tmp/a.png",
        engine_context=context,
    )

    context.skill_executor.execute_async.assert_awaited_once()
    target, params = context.skill_executor.execute_async.await_args.args[:2]
    assert target.name == "send_image"
    assert params == {"image_path": "C:/tmp/a.png"}
    assert result.internal_metadata["interaction_action"] == "image"


@pytest.mark.asyncio
async def test_group_management_routes_action_to_internal_skill():
    context = _FakeContext(["mute_member"])

    result = await group_management.run(
        action="mute_member",
        user_id=1001,
        duration=60,
        engine_context=context,
    )

    context.skill_executor.execute_async.assert_awaited_once()
    target, params = context.skill_executor.execute_async.await_args.args[:2]
    assert target.name == "mute_member"
    assert params == {"user_id": 1001, "duration": 60}
    assert result.internal_metadata["management_action"] == "mute_member"


@pytest.mark.asyncio
async def test_composite_skill_when_action_is_unknown_then_does_not_execute():
    context = _FakeContext([])

    result = await interaction.run(action="unknown", engine_context=context)

    assert result["success"] is False
    context.skill_executor.execute_async.assert_not_awaited()
