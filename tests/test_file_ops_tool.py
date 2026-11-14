"""业务视角：她做自己的项目时能看、能改、能找自己的文件，而且只动得了自己家目录。

以前这些都靠 bash：``python3 -c`` 读进来 ``s.replace`` 写回去，内容一长就撞上命令长度上限。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from sirius_pulse.memory.user.unified_models import UnifiedUser
from sirius_pulse.tools.builtin import file_ops
from sirius_pulse.tools.data_store import ToolDataStore
from sirius_pulse.tools.executor import ToolExecutor
from sirius_pulse.tools.models import ToolInvocationContext
from sirius_pulse.tools.registry import ToolRegistry


@pytest.fixture
def persona(tmp_path: Path) -> tuple[ToolDataStore, Path]:
    """人格目录：工具数据在 tool_data/，她的家在 home/。"""
    store = ToolDataStore(tmp_path / "tool_data" / "file_ops.json")
    home = tmp_path / "home"
    (home / "notes").mkdir(parents=True)
    return store, home


def _call(store: ToolDataStore, action: str, *, developer: bool = False, **params) -> dict:
    who = ToolInvocationContext(
        caller=UnifiedUser(user_id="u1", name="临雀", metadata={"is_developer": developer}),
        group_id="g1",
    )
    return file_ops.run(action, data_store=store, invocation_context=who, **params)


def _text(result: dict) -> str:
    assert result["success"] is True, result.get("error")
    return result["text_blocks"][0]


def test_a_long_note_is_read_with_line_numbers_until_the_end(persona) -> None:
    store, home = persona
    (home / "notes" / "stars.md").write_text(
        "".join(f"第{i}颗星：{'很亮' * 20}\n" for i in range(1, 301)), encoding="utf-8"
    )

    pieces, offset = [], 1
    while offset is not None:
        result = _call(store, "read", path="~/notes/stars.md", offset=offset)
        pieces.append(_text(result))
        offset = result["data"]["next_offset"]

    assert len(pieces) > 1, "超过一次结果上限的文件要分段给"
    assert all(len(piece) <= 6000 for piece in pieces)
    assert "     1\t第1颗星" in pieces[0]
    assert "   300\t第300颗星" in pieces[-1] and "已读完" in pieces[-1]


def test_she_changes_exactly_the_part_she_meant(persona) -> None:
    store, home = persona
    script = home / "notes" / "gen_chart.py"
    script.write_text(
        "offsets = {\n    '天狼星': (0.0, 0.6),\n    '军市一': (0.4, 0.2),\n}\n", encoding="utf-8"
    )

    result = _call(
        store,
        "edit",
        path="~/notes/gen_chart.py",
        old_text="    '天狼星': (0.0, 0.6),",
        new_text="    '天狼星': (-0.35, 0.6),",
    )

    assert "-0.35" in _text(result) and "第 2 行" in _text(result)
    assert script.read_text(encoding="utf-8") == (
        "offsets = {\n    '天狼星': (-0.35, 0.6),\n    '军市一': (0.4, 0.2),\n}\n"
    )


@pytest.mark.parametrize(
    ("old_text", "replace_all", "error", "expected"),
    [
        ("TODO", False, "出现了 2 次", "TODO 收尾\nTODO 配图\n"),
        ("TODO", True, None, "DONE 收尾\nDONE 配图\n"),
        ("todo", False, "没找到", "TODO 收尾\nTODO 配图\n"),
    ],
    ids=["ambiguous-refused", "replace-all", "not-found-hint"],
)
def test_an_ambiguous_or_missing_edit_changes_nothing_and_says_why(
    persona, old_text: str, replace_all: bool, error: str | None, expected: str
) -> None:
    store, home = persona
    plan = home / "plan.txt"
    plan.write_text("TODO 收尾\nTODO 配图\n", encoding="utf-8")

    result = _call(
        store,
        "edit",
        path="~/plan.txt",
        old_text=old_text,
        new_text="DONE",
        replace_all=replace_all,
    )

    if error:
        assert result["success"] is False and error in result["error"]
    else:
        assert result["success"] is True
    assert plan.read_text(encoding="utf-8") == expected


def test_a_windows_style_file_keeps_its_line_endings_after_an_edit(persona) -> None:
    store, home = persona
    notes = home / "win.txt"
    notes.write_bytes("第一行\r\n第二行\r\n第三行\r\n".encode("utf-8"))

    # 她从 read 的结果里复制的是 LF 换行。
    result = _call(store, "edit", path="~/win.txt", old_text="第一行\n第二行", new_text="改过的两行")

    assert result["success"] is True, result.get("error")
    assert notes.read_bytes() == "改过的两行\r\n第三行\r\n".encode("utf-8")


def test_long_content_that_bash_could_not_carry_is_written_in_one_go(persona) -> None:
    store, home = persona
    chapter = "".join(f"第{i}段：她在窗边数星星，一颗、两颗、三颗。\n" for i in range(2000))

    result = _call(store, "write", path="~/projects/novel/chapter1.md", content=chapter)

    assert "新建" in _text(result)
    assert (home / "projects" / "novel" / "chapter1.md").read_text(encoding="utf-8") == chapter


@pytest.mark.parametrize("developer", [False, True], ids=["group-member", "developer"])
def test_writes_never_leave_her_home(persona, tmp_path: Path, developer: bool) -> None:
    store, home = persona
    config = tmp_path / "persona.json"
    config.write_text('{"name": "月白"}', encoding="utf-8")
    sneaky = home / "persona-link.json"
    try:
        sneaky.symlink_to(config)
    except OSError:
        sneaky = None

    direct = _call(store, "write", path=str(config), content="{}", developer=developer)
    edited = _call(
        store, "edit", path=str(config), old_text="月白", new_text="X", developer=developer
    )

    assert direct["success"] is False and "家目录" in direct["error"]
    assert edited["success"] is False
    if sneaky is not None:
        via_link = _call(store, "write", path="~/persona-link.json", content="{}")
        assert via_link["success"] is False
    assert config.read_text(encoding="utf-8") == '{"name": "月白"}'


@pytest.mark.parametrize(("developer", "readable"), [(False, False), (True, True)])
def test_reading_config_outside_her_places_needs_a_developer(
    persona, tmp_path: Path, developer: bool, readable: bool
) -> None:
    store, _ = persona
    secret = tmp_path / "accounts.json"
    secret.write_text('{"token": "ghp_secret"}', encoding="utf-8")

    result = _call(store, "read", path=str(secret), developer=developer)

    assert result["success"] is readable
    if not readable:
        assert "ghp_secret" not in str(result)


def test_she_can_find_her_way_around_her_home(persona) -> None:
    store, home = persona
    (home / "notes" / "todo.md").write_text("- 给星图补上军市\n- 整理梦\n", encoding="utf-8")
    (home / "tools").mkdir()
    (home / "tools" / "chart.py").write_text("# 军市 的标注偏移\nX = 1\n", encoding="utf-8")
    (home / "tools" / "node_modules" / "pkg").mkdir(parents=True)
    (home / "tools" / "node_modules" / "pkg" / "index.js").write_text("// 军市", encoding="utf-8")

    listing = _text(_call(store, "list"))
    everywhere = _text(_call(store, "grep", pattern="军市"))
    only_python = _text(_call(store, "grep", pattern="军市", glob="*.py"))

    assert "notes/" in listing and "todo.md" in listing and "node_modules/（已略过）" in listing
    assert "~/notes/todo.md:1:" in everywhere and "~/tools/chart.py:1:" in everywhere
    assert "index.js" not in everywhere
    assert "chart.py" in only_python and "todo.md" not in only_python


@pytest.mark.parametrize(
    ("name", "content", "error"),
    [
        ("report.pdf", b"%PDF-1.4", "read_document"),
        ("photo.png", b"\x89PNG\r\n\x1a\n", "read_image"),
        ("blob.bin", b"\x00\x01binary", "不是文本文件"),
    ],
)
def test_files_she_should_open_another_way_point_to_the_right_tool(
    persona, name: str, content: bytes, error: str
) -> None:
    store, home = persona
    (home / name).write_bytes(content)

    result = _call(store, "read", path=f"~/{name}")

    assert result["success"] is False and error in result["error"]


@pytest.mark.asyncio
async def test_her_own_project_turn_may_write_to_her_home(tmp_path: Path) -> None:
    """自主回合拒绝一切对外发送；写自己家目录里的文件不算，必须放行。"""
    registry = ToolRegistry()
    registry._load_builtin_tools(auto_install_deps=False)
    tool = registry.get("file_ops")
    assert tool is not None
    executor = ToolExecutor(tmp_path)
    own_turn = ToolInvocationContext(self_initiated=True, group_id="g1")

    result = await executor.execute_async(
        tool,
        {"action": "write", "path": "~/projects/stars/log.md", "content": "今天补完了军市。\n"},
        invocation_context=own_turn,
    )

    assert result.success, result.error
    written = executor.get_data_store("file_ops").store_path.parent.parent / "home"
    assert (written / "projects" / "stars" / "log.md").read_text(encoding="utf-8") == ("今天补完了军市。\n")
