"""工作模式只读 API：页面要能看到她进过哪些工作模式、每一轮做了什么。

工作模式内的正文不外发，聊天记录里看不到过程，所以轨迹文件是唯一的过程视图；
这里验证接口把它读出来，并且不会因为被看一眼就写文件。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sirius_pulse.core.work_mode import WorkModeRun, WorkModeStore
from sirius_pulse.webui.work_mode_api import api_persona_work_mode_get


class _FakeRequest:
    def __init__(self, query: dict[str, str] | None = None) -> None:
        self.query = query or {}


def _work_path(tmp_path: Path) -> Path:
    work = tmp_path / "sirius"
    (work / "memory").mkdir(parents=True)
    return work


def _finished_run(
    goal: str, result: str, *, session_id: str, started_at: str, ended_at: str
) -> WorkModeRun:
    run = WorkModeRun(group_id="group-1", goal=goal, session_id=session_id)
    run.started_at = started_at
    run.add_step(
        text="内部笔记：先列目录",
        tools=[{"name": "bash", "arguments": '{"command": "ls"}'}],
        results=[{"tool": "bash", "output": "[Tool result: success]\nok: True"}],
    )
    run.add_step(kind="midway", text="进展：列完目录了")
    run.finish(result=result)
    run.ended_at = ended_at
    return run


@pytest.mark.asyncio
async def test_work_mode_api_shows_goal_steps_and_result(tmp_path):
    """页面要能回答：她为什么进去、每一轮干了什么、最后交出了什么。"""
    work = _work_path(tmp_path)
    store = WorkModeStore(work)
    store.save_run(
        _finished_run(
            "整理群文件",
            "整理完了",
            session_id="s1",
            started_at="2026-01-01T10:00:00+00:00",
            ended_at="2026-01-01T11:00:00+00:00",
        )
    )
    store.save_run(
        _finished_run(
            "核对名单",
            "核对完成",
            session_id="s2",
            started_at="2026-01-01T11:30:00+00:00",
            ended_at="2026-01-01T12:00:00+00:00",
        )
    )

    response = await api_persona_work_mode_get(_FakeRequest(), work)
    payload = json.loads(response.text)

    assert response.status == 200
    assert payload["summary"]["sessions_total"] == 2
    assert payload["summary"]["sessions_completed"] == 2
    assert payload["summary"]["sessions_running"] == 0
    assert payload["summary"]["steps_total"] == 4
    # 倒序：最近一次在最前面，页面直接渲染。
    assert [item["session_id"] for item in payload["sessions"]] == ["s2", "s1"]
    assert payload["summary"]["last_goal"] == "核对名单"
    assert payload["summary"]["last_session_at"] == "2026-01-01T12:00:00+00:00"
    newest = payload["sessions"][0]
    assert newest["goal"] == "核对名单"
    assert newest["result"] == "核对完成"
    assert newest["steps"][0]["tools"][0]["name"] == "bash"
    assert "success" in newest["steps"][0]["results"][0]["output"]
    assert newest["steps"][1]["kind"] == "midway"
    assert payload["paths"]["sessions"].endswith("sessions.json")


@pytest.mark.asyncio
async def test_work_mode_api_is_empty_and_read_only_for_a_fresh_persona(tmp_path):
    """没进过工作模式的人格不该报错，也不该因为被看了一眼就产生记录。"""
    work = _work_path(tmp_path)

    response = await api_persona_work_mode_get(_FakeRequest(), work)
    payload = json.loads(response.text)

    assert response.status == 200
    assert payload["sessions"] == []
    assert payload["summary"]["sessions_total"] == 0
    assert payload["summary"]["last_session_at"] == ""
    assert not (work / "memory" / "work_mode" / "sessions.json").exists()
