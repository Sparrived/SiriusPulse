"""业务视角：她开工时就知道自己家里有什么、最近在弄什么，不用每次先 ls 一遍。

线上她自己的回合里，一万七千多次 bash 以 ``cd $HOME`` / ``ls -la ~`` 开头。
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from sirius_pulse.core import life_bridge
from sirius_pulse.life import home as home_module
from sirius_pulse.life.home import summarize_home
from tests.test_scheduled_tool_context import _Brain, _context, _final_round

LOCAL = timezone(timedelta(hours=8))
NOW = datetime(2026, 11, 14, 20, 0, tzinfo=LOCAL)


def _touch(path: Path, when: datetime, content: str = "x") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    stamp = when.timestamp()
    os.utime(path, (stamp, stamp))


def _her_home(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    _touch(home / "notes" / "gen_jingsu_hunting.py", NOW - timedelta(minutes=17))
    _touch(home / "notes" / "stars.md", NOW - timedelta(days=3))
    _touch(home / "diary" / "2026-11-07.md", NOW - timedelta(days=7))
    _touch(home / "couple_avatars" / "left.png", NOW - timedelta(hours=1))
    _touch(home / "test_render_snow.py", NOW - timedelta(days=1, hours=2))
    # 不是她的东西：缓存、依赖、隐藏目录。
    for i in range(30):
        _touch(home / ".cache" / "pip" / f"blob{i}", NOW)
    _touch(home / "tools" / "node_modules" / "pkg" / "index.js", NOW)
    _touch(home / "tools" / "chart.py", NOW - timedelta(days=20))
    return home


def test_the_glance_leads_with_what_she_touched_last(tmp_path: Path) -> None:
    overview = summarize_home(_her_home(tmp_path), now=NOW)

    lines = overview.splitlines()
    recent = lines[lines.index("最近改过的文件：") + 1 :]
    assert recent[0] == "- ~/notes/gen_jingsu_hunting.py（今天 19:43）"
    assert recent[1] == "- ~/couple_avatars/left.png（今天 19:00）"
    assert "共 6 个文件" in overview
    folders = lines[lines.index("文件夹（最近有改动的在前）：") + 1 :]
    assert folders[:2] == ["- notes/（2 个文件，今天 19:43）", "- couple_avatars/（1 个文件，今天 19:00）"]
    assert "~ 根目录下还有 1 个零散文件（最近 昨天 18:00），比如 test_render_snow.py" in overview
    # 缓存、依赖目录、隐藏目录都不算她的东西。
    assert ".cache" not in overview and "node_modules" not in overview and "blob" not in overview
    assert "file_ops" in overview


def test_long_untouched_folders_are_only_named(tmp_path: Path) -> None:
    home = tmp_path / "home"
    for i in range(15):
        _touch(home / f"project{i:02d}" / "main.py", NOW - timedelta(days=i))

    overview = summarize_home(home, now=NOW)

    assert "- project00/（1 个文件，今天 20:00）" in overview
    assert "project11/" in overview and "project12/" not in overview
    assert "很久没动的：project12、project13、project14" in overview


def test_a_huge_home_is_only_partly_counted(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(home_module, "MAX_SCANNED_FILES", 5)
    home = tmp_path / "home"
    for i in range(12):
        _touch(home / "probe" / f"run{i}.log", NOW)

    overview = summarize_home(home, now=NOW)

    assert "5 个文件以上" in overview


def test_no_home_yet_means_no_glance(tmp_path: Path) -> None:
    assert summarize_home(tmp_path / "home", now=NOW) == ""


@pytest.mark.asyncio
async def test_her_own_turn_starts_with_the_glance_and_it_is_reused_for_a_while(
    tmp_path: Path, monkeypatch
) -> None:
    _her_home(tmp_path)
    brain = _Brain([_final_round("补完了军市的标注。"), _final_round("又看了一眼。")])
    context = _context(tmp_path, brain)
    clock = [1000.0]
    monkeypatch.setattr(life_bridge.time, "monotonic", lambda: clock[0])

    await context.run_autonomous_turn(kind="project", seed="继续画星图", group_id="g1")
    _touch(tmp_path / "home" / "notes" / "new_idea.md", datetime.now(timezone.utc))
    await context.run_autonomous_turn(kind="project", seed="继续画星图", group_id="g1")
    clock[0] += life_bridge.HOME_OVERVIEW_TTL_SECONDS + 1
    brain._rounds.append(_final_round("第三次。"))
    await context.run_autonomous_turn(kind="project", seed="继续画星图", group_id="g1")

    first, second, third = (
        "\n".join(str(m.get("content")) for m in request.messages) for request in brain.requests
    )
    assert "【你的家目录 ~】" in first and "~/notes/gen_jingsu_hunting.py" in first
    assert "new_idea.md" not in second, "几分钟内复用同一份概览，不必每次都遍历"
    assert "new_idea.md" in third
