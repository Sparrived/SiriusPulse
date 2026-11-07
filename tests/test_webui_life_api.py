"""WebUI 里看她的生活：某天的日程、她的项目、读她的作品、首页一瞥里的此刻。

业务视角：
- 能看到她这天给自己排了什么、做了没有、为什么没做，以及这天夜里的梦；
- 能翻开她写下的作品；不合法的项目 id 被拒绝，读不到别的文件；
- 新人格没有任何生活数据时得到空结构，而且查看不会凭空创建文件。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from sirius_pulse.life import life_dir
from sirius_pulse.life.agenda import AgendaStore, Block, DayAgenda
from sirius_pulse.life.dreams import Dream, DreamStore
from sirius_pulse.life.projects import ProjectStore
from sirius_pulse.life.reflection import SelfNote, SelfNoteStore
from sirius_pulse.webui.companion_api import api_persona_glance_get
from sirius_pulse.webui.life_api import api_persona_life_get, api_persona_life_work_get


class _FakeRequest:
    def __init__(self, query: dict | None = None, match_info: dict | None = None) -> None:
        self.query = query or {}
        self.match_info = match_info or {}


def _work(tmp_path):
    work = tmp_path / "sirius"
    (work / "memory").mkdir(parents=True)
    return work


@pytest.mark.asyncio
async def test_she_can_be_seen_planning_and_working_on_her_day(tmp_path):
    work = _work(tmp_path)
    life = life_dir(work)
    projects = ProjectStore(life)
    project = projects.start(
        title="读《雪国》并写读书笔记", kind="read", now=datetime(2026, 3, 1, 1, tzinfo=timezone.utc)
    )
    projects.append_work(
        project.project_id, "## 第一章\n岛村来到雪国。", now=datetime(2026, 3, 1, 2, tzinfo=timezone.utc)
    )
    AgendaStore(life).save(
        DayAgenda(
            date="2026-03-01",
            note="慢慢读书",
            blocks=[
                Block(
                    start="10:00",
                    end="11:00",
                    activity="读第一章",
                    project_id=project.project_id,
                    status="done",
                    outcome="读完了",
                ),
                Block(start="15:00", end="16:00", activity="散步", status="skipped", reason="下雨了"),
            ],
        )
    )
    SelfNoteStore(life).save(SelfNote(recent="最近在读川端。", interests=["雪"]))
    DreamStore(life).save(Dream(date="2026-03-01", text="梦见雪落进了书页里", feeling="很轻"))

    day = json.loads((await api_persona_life_get(_FakeRequest({"date": "2026-03-01"}), work)).text)
    opened = json.loads(
        (
            await api_persona_life_work_get(
                _FakeRequest(match_info={"project_id": project.project_id}), work
            )
        ).text
    )

    assert day["agenda"]["note"] == "慢慢读书"
    assert [b["status"] for b in day["agenda"]["blocks"]] == ["done", "skipped"]
    assert day["agenda"]["blocks"][1]["reason"] == "下雨了"
    assert day["projects"][0]["kind_label"] == "阅读"
    assert day["works"][0]["project_id"] == project.project_id
    assert day["self_note"]["recent"] == "最近在读川端。"
    assert day["dream"]["text"] == "梦见雪落进了书页里"
    assert "岛村来到雪国" in opened["markdown"]


@pytest.mark.asyncio
@pytest.mark.parametrize("project_id", ["../persona", "p-zzzz", "p-12345678/../../x"])
async def test_reading_a_work_rejects_ids_that_are_not_projects(tmp_path, project_id):
    response = await api_persona_life_work_get(
        _FakeRequest(match_info={"project_id": project_id}), _work(tmp_path)
    )
    assert response.status == 400


@pytest.mark.asyncio
async def test_a_bad_date_is_rejected(tmp_path):
    response = await api_persona_life_get(_FakeRequest({"date": "../../x"}), _work(tmp_path))
    assert response.status == 400


@pytest.mark.asyncio
async def test_a_fresh_persona_has_an_empty_life_and_viewing_creates_nothing(tmp_path):
    work = _work(tmp_path)

    data = json.loads((await api_persona_life_get(_FakeRequest(), work)).text)
    glance = json.loads((await api_persona_glance_get(_FakeRequest(), work)).text)

    assert data["agenda"] is None
    assert data["projects"] == [] and data["works"] == []
    assert data["now"]["wake_time"] == "07:30"
    assert glance["life"]["phase"] in {"asleep", "morning", "day", "evening"}
    assert not life_dir(work).exists()
