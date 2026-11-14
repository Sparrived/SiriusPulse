"""Memory recall: old/retired units and current-chat archive lines are searchable."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from sirius_pulse.memory.basic.models import BasicMemoryEntry
from sirius_pulse.memory.basic.store import BasicMemoryFileStore
from sirius_pulse.memory.search import MemorySearchService
from sirius_pulse.memory.units.models import MemoryUnit
from sirius_pulse.memory.units.store import MemoryUnitFileStore
from sirius_pulse.tools.builtin import memory_search
from sirius_pulse.tools.models import ToolInvocationContext


def _unit(
    unit_id: str,
    group_id: str,
    summary: str,
    *,
    scope: str = "group",
    scope_id: str = "",
    participants: list[str] | None = None,
    event_time: str = "2026-11-01T10:00:00+00:00",
    should_prompt: bool = True,
    retired: str = "",
) -> MemoryUnit:
    return MemoryUnit(
        unit_id=unit_id,
        group_id=group_id,
        created_at=event_time,
        summary=summary,
        scope=scope,
        scope_id=scope_id,
        participants=list(participants or []),
        event_time=event_time,
        should_prompt=should_prompt,
        metadata={"retired": retired} if retired else {},
    )


def _entry(
    entry_id: str,
    group_id: str,
    content: str,
    *,
    user_id: str = "qq_1",
    speaker_name: str = "临雀",
    timestamp: str = "2026-11-01T10:00:00+00:00",
) -> BasicMemoryEntry:
    return BasicMemoryEntry(
        entry_id=entry_id,
        group_id=group_id,
        user_id=user_id,
        role="human",
        sender_type="human",
        speaker_name=speaker_name,
        content=content,
        timestamp=timestamp,
    )


def test_retired_current_group_unit_is_searchable(tmp_path: Path) -> None:
    units = MemoryUnitFileStore(tmp_path)
    units.save(
        "g1",
        [
            _unit(
                "u-old",
                "g1",
                "前前前世是成都的测试工程师，和池宇健是两个人。",
                should_prompt=False,
                retired="overflow",
            ),
            _unit("u-other", "g1", "今天讨论的是烧烤和月亮。"),
        ],
    )

    result = MemorySearchService(tmp_path).search(group_id="g1", query="前前前世", source="units")

    assert result["success"] is True
    assert [hit["unit_id"] for hit in result["units"]] == ["u-old"]
    assert result["units"][0]["active"] is False
    assert result["units"][0]["retired_reason"] == "overflow"
    assert "测试工程师" in result["units"][0]["summary"]


def test_global_units_are_opt_in_and_never_expose_other_group_units(tmp_path: Path) -> None:
    units = MemoryUnitFileStore(tmp_path)
    units.save("g1", [_unit("u-current", "g1", "当前群的普通事实。")])
    units.save(
        "g2",
        [
            _unit("u-persona", "g2", "人格知道自己是月白。", scope="persona"),
            _unit("u-private", "g2", "另一个群的私密事实。", scope="group"),
        ],
    )
    service = MemorySearchService(tmp_path)

    local = service.search(group_id="g1", query="", source="units")
    global_hits = service.search(group_id="g1", query="", source="units", include_global=True)

    assert "u-persona" not in [hit["unit_id"] for hit in local["units"]]
    assert "u-persona" in [hit["unit_id"] for hit in global_hits["units"]]
    assert "u-private" not in [hit["unit_id"] for hit in global_hits["units"]]


def test_message_search_returns_current_chat_newest_first_and_filters(tmp_path: Path) -> None:
    archive = BasicMemoryFileStore(tmp_path)
    archive.append(
        _entry(
            "e1",
            "g1",
            "旧约定：周六一起看月亮。",
            timestamp="2026-11-01T10:00:00+00:00",
        )
    )
    archive.append(
        _entry(
            "e2",
            "g1",
            "新的约定：周日一起看月亮。",
            user_id="qq_2",
            speaker_name="卡咔",
            timestamp="2026-11-02T10:00:00+00:00",
        )
    )
    archive.append(_entry("x1", "g2", "别的群的约定，不能读到。", timestamp="2026-11-03T10:00:00+00:00"))

    service = MemorySearchService(tmp_path)
    result = service.search(group_id="g1", query="约定", source="messages")

    assert [hit["entry_id"] for hit in result["messages"]] == ["e2", "e1"]
    assert all(hit["entry_id"] != "x1" for hit in result["messages"])

    filtered = service.search(
        group_id="g1",
        query="约定",
        source="messages",
        since="2026-11-02",
        until="2026-11-02",
        user_id="qq_2",
    )
    assert [hit["entry_id"] for hit in filtered["messages"]] == ["e2"]

    recent = service.search(group_id="g1", query="", source="messages")
    assert [hit["entry_id"] for hit in recent["messages"]] == ["e2", "e1"]


def test_message_search_does_not_match_hidden_prompt_fields(tmp_path: Path) -> None:
    entry = _entry("e1", "g1", "只是一句普通聊天。")
    entry.conversation_chain = [{"role": "system", "content": "这里藏着前前前世，但不是聊天原文。"}]
    BasicMemoryFileStore(tmp_path).append(entry)

    result = MemorySearchService(tmp_path).search(group_id="g1", query="前前前世", source="messages")

    assert result["messages"] == []


def test_empty_query_and_missing_archive_are_not_errors(tmp_path: Path) -> None:
    result = MemorySearchService(tmp_path).search(group_id="g1", query="", source="messages")
    assert result["success"] is True
    assert result["messages"] == []


class _EngineContext:
    def __init__(self, work_path: Path) -> None:
        self._work_path = work_path

    def get_work_path(self) -> str:
        return str(self._work_path)


@pytest.mark.asyncio
async def test_memory_search_tool_uses_trusted_group_and_returns_hits(tmp_path: Path) -> None:
    units = MemoryUnitFileStore(tmp_path)
    units.save("g1", [_unit("u1", "g1", "月白记得旧约定。")])
    archive = BasicMemoryFileStore(tmp_path)
    archive.append(_entry("e1", "g1", "约定要一起看月亮。"))

    result = await memory_search.run(
        query="约定",
        source="both",
        engine_context=_EngineContext(tmp_path),
        invocation_context=ToolInvocationContext(group_id="g1"),
    )

    assert result["success"] is True
    assert result["units"][0]["unit_id"] == "u1"
    assert result["messages"][0]["entry_id"] == "e1"


@pytest.mark.asyncio
async def test_memory_search_tool_requires_current_chat(tmp_path: Path) -> None:
    result = await memory_search.run(
        query="约定",
        engine_context=_EngineContext(tmp_path),
        invocation_context=ToolInvocationContext(group_id=""),
    )
    assert result["success"] is False
    assert "当前会话" in result["error"]
