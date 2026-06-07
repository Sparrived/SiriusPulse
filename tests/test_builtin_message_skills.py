"""Business tests for built-in sticker and pinned-message skills."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from sirius_pulse.core.bg_tasks_delayed import DelayedQueueTasks
from sirius_pulse.skills import SkillDefinition
from sirius_pulse.skills.builtin import (
    list_pinned_messages,
    pin_message,
    send_sticker,
    unpin_message,
)


class FakeEngineContext:
    def __init__(self) -> None:
        self.sent_stickers: list[tuple[str, list[str]]] = []
        self.pinned: dict[str, dict[str, Any]] = {}
        self.recent: dict[str, dict[str, Any]] = {
            "m1": {
                "content": "以后这个群里默认用中文回复",
                "speaker": "Alice",
                "user_id": "u1",
            }
        }

    def list_sticker_names(self) -> list[str]:
        return ["开心", "收到", "疑惑"]

    async def send_sticker_by_names(self, group_id: str, names: list[str]) -> dict[str, Any]:
        self.sent_stickers.append((group_id, names))
        return {
            "success": True,
            "sticker_name": names[0],
            "file_path": f"/fake/{names[0]}.png",
        }

    def pin_recent_message_by_id(
        self, group_id: str, msg_id: str, reason: str = ""
    ) -> dict[str, Any]:
        msg = self.recent.get(msg_id)
        if msg is None:
            return {"success": False, "error": "missing"}
        pinned = {
            "message_id": "pin_1",
            "content": msg["content"],
            "speaker": msg["speaker"],
            "group_id": group_id,
            "reason": reason,
            "metadata": {"user_id": msg["user_id"], "platform_message_id": msg_id},
        }
        self.pinned[pinned["message_id"]] = pinned
        return {"success": True, "summary": "ok", "pinned_message": pinned}

    def unpin_message(self, message_id: str) -> dict[str, Any]:
        if message_id not in self.pinned:
            return {"success": False, "error": "missing"}
        del self.pinned[message_id]
        return {"success": True, "summary": "ok"}

    def get_pinned_messages(self, group_id: str) -> list[dict[str, Any]]:
        return [item for item in self.pinned.values() if item.get("group_id") == group_id]


@pytest.mark.asyncio
async def test_send_sticker_skill_when_name_matches_then_sends_to_current_chat():
    ctx = FakeEngineContext()

    result = await send_sticker.run(
        names=["开心", "不存在"],
        chat_context={"group_id": "group_a"},
        engine_context=ctx,
    )

    assert result["success"] is True
    assert ctx.sent_stickers == [("group_a", ["开心"])]


@pytest.mark.asyncio
async def test_send_sticker_skill_when_no_name_matches_then_fails_without_send():
    ctx = FakeEngineContext()

    result = await send_sticker.run(
        names=["不存在"],
        chat_context={"group_id": "group_a"},
        engine_context=ctx,
    )

    assert result["success"] is False
    assert ctx.sent_stickers == []


def test_pinned_message_skills_when_pin_list_unpin_then_context_is_updated():
    ctx = FakeEngineContext()

    pin_result = pin_message.run(
        msg_id="m1",
        reason="长期规则",
        chat_context={"group_id": "group_a"},
        engine_context=ctx,
    )
    listed = list_pinned_messages.run(
        chat_context={"group_id": "group_a"},
        engine_context=ctx,
    )
    unpin_result = unpin_message.run(
        message_id="pin_1",
        engine_context=ctx,
    )

    assert pin_result["success"] is True
    assert "pin_1" in listed["text_blocks"][0]
    assert "以后这个群里默认用中文回复" in listed["text_blocks"][0]
    assert unpin_result["success"] is True
    assert ctx.pinned == {}


def test_autonomous_message_skill_check_only_trusts_package_builtins(tmp_path):
    builtin_path = (
        Path(__file__).resolve().parents[1]
        / "sirius_pulse"
        / "skills"
        / "builtin"
        / "send_sticker.py"
    )
    trusted = SkillDefinition(
        name="send_sticker",
        description="built-in sticker sender",
        source_path=builtin_path,
    )
    spoofed = SkillDefinition(
        name="send_sticker",
        description="workspace spoof",
        source_path=tmp_path / "skills" / "send_sticker.py",
    )
    unrelated = SkillDefinition(
        name="send_image",
        description="ordinary messaging skill",
        source_path=builtin_path,
    )

    assert DelayedQueueTasks._is_autonomous_message_skill(trusted) is True
    assert DelayedQueueTasks._is_autonomous_message_skill(spoofed) is False
    assert DelayedQueueTasks._is_autonomous_message_skill(unrelated) is False
