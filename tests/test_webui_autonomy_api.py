"""自主行为只读 API：页面要能读到"她惦记什么、做过什么"。

自主留下的痕迹在磁盘上而不是聊天记录里，没有这个接口就只能去翻文件。
这里验证接口返回什么，以及它确实是只读的。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sirius_pulse.core.autonomy import Episode
from sirius_pulse.core.intent import IntentFileStore, Intention, IntentStore
from sirius_pulse.utils.json_io import atomic_write_json
from sirius_pulse.webui.autonomy_api import api_persona_autonomy_get


class _FakeRequest:
    def __init__(self, query: dict[str, str] | None = None) -> None:
        self.query = query or {}


def _work_path(tmp_path: Path) -> Path:
    work = tmp_path / "sirius"
    (work / "memory").mkdir(parents=True)
    return work


def _write_episodes(work: Path, episodes: list[Episode]) -> None:
    path = work / "memory" / "autonomy" / "episodes.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, {"episodes": [item.to_dict() for item in episodes]})


@pytest.mark.asyncio
async def test_autonomy_api_shows_what_she_carries_and_what_she_did(tmp_path):
    """页面要能回答两个问题：她惦记着什么、她做过什么。"""
    work = _work_path(tmp_path)
    IntentFileStore(work).save(
        IntentStore(
            [
                Intention.create(what="把那篇文章读完", kind="reading", urgency=0.9),
                Intention.create(
                    what="想告诉他今天看到的笑话",
                    resolution="tell",
                    audience="private_10001",
                ),
            ]
        )
    )
    _write_episodes(
        work,
        [
            Episode(
                episode_id="e1",
                started_at="2026-01-01T10:00:00+00:00",
                ended_at="2026-01-01T10:05:00+00:00",
                kind="reading",
                outcome="读了一半",
            ),
            Episode(
                episode_id="e2",
                started_at="2026-01-01T11:00:00+00:00",
                ended_at="2026-01-01T11:02:00+00:00",
                kind="share",
                outcome="把笑话讲出去了",
                audience="private_10001",
            ),
        ],
    )

    response = await api_persona_autonomy_get(_FakeRequest(), work)
    payload = json.loads(response.text)

    assert response.status == 200
    assert payload["summary"]["intentions_total"] == 2
    assert payload["summary"]["intentions_open"] == 2
    assert payload["summary"]["episodes_total"] == 2
    # 倒序：最新的在最前面，页面直接渲染。
    assert [item["episode_id"] for item in payload["episodes"]] == ["e2", "e1"]
    assert payload["summary"]["last_episode_at"] == "2026-01-01T11:02:00+00:00"
    wat = next(item for item in payload["intentions"] if item["resolution"] == "tell")
    assert wat["audience"] == "private_10001"
    assert payload["paths"]["intentions"].endswith("intentions.json")


@pytest.mark.asyncio
async def test_autonomy_api_is_empty_and_read_only_for_a_fresh_persona(tmp_path):
    """还没自主过的人格不该报错，也不该因为被看了一眼就产生记录。"""
    work = _work_path(tmp_path)

    response = await api_persona_autonomy_get(_FakeRequest(), work)
    payload = json.loads(response.text)

    assert response.status == 200
    assert payload["intentions"] == []
    assert payload["episodes"] == []
    assert payload["summary"]["episodes_total"] == 0
    # 只读：不写回任何文件。
    assert not (work / "memory" / "intentions.json").exists()
    assert not (work / "memory" / "autonomy" / "episodes.json").exists()


@pytest.mark.asyncio
async def test_resolved_intention_leaves_the_open_count_but_stays_visible(tmp_path):
    """她做过的事要留在时间线上，但不能还算作"还没了结"。"""
    work = _work_path(tmp_path)
    done = Intention.create(what="去查那个报错")
    done.status = "resolved"
    done.resolved_at = "2026-01-01T10:00:00+00:00"
    IntentFileStore(work).save(IntentStore([done, Intention.create(what="还想再看看")]))

    payload = json.loads((await api_persona_autonomy_get(_FakeRequest(), work)).text)

    assert payload["summary"]["intentions_total"] == 2
    assert payload["summary"]["intentions_open"] == 1
