"""新版 WebUI「月白的小屋」依赖的后端行为。

- 每个标签页用 ``X-Sirius-Persona`` 声明自己在看哪个人格，互不串台；
- 首页一瞥能说出她的心情和最近 24 小时的动静；
- 每个人格可以有自己的立绘，没有时给出内置默认图；
- 前端在 ``/``，旧控制台已经拆掉；没构建前端时 ``/`` 说明怎么构建，而不是给一个空页面。
"""

from __future__ import annotations

import io
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import pytest
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image

from sirius_pulse.webui import server_core
from sirius_pulse.webui.server import WebUIServer


def _persona(data_dir: Path, name: str, display: str) -> Path:
    persona_dir = data_dir / "personas" / name
    persona_dir.mkdir(parents=True)
    (persona_dir / "persona.json").write_text(
        json.dumps({"name": display, "aliases": [], "full_system_prompt": ""}, ensure_ascii=False),
        encoding="utf-8",
    )
    return persona_dir


def _iso(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) - delta).isoformat()


async def _client(data_dir: Path) -> tuple[TestClient, dict[str, str]]:
    server = WebUIServer(data_dir, host="127.0.0.1", port=0)
    client = TestClient(TestServer(server.app))
    await client.start_server()
    token = server.auth_manager.create_token("admin", role="admin")
    return client, {"Authorization": f"Bearer {token}"}


def _png(width: int, height: int) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGBA", (width, height), (120, 150, 255, 200)).save(buffer, format="PNG")
    return buffer.getvalue()


@pytest.mark.asyncio
async def test_each_tab_reads_the_persona_it_names_without_switching_others(tmp_path):
    _persona(tmp_path, "alice", "爱丽丝")
    _persona(tmp_path, "sirius", "月白")
    _persona(tmp_path, "小雪", "小雪酱")
    (tmp_path / "global_config.json").write_text(
        json.dumps({"active_persona": "alice"}), encoding="utf-8"
    )
    client, auth = await _client(tmp_path)
    try:
        scoped = await client.get(
            "/api/persona/persona", headers={**auth, "X-Sirius-Persona": "sirius"}
        )
        assert scoped.status == 200
        assert (await scoped.json())["name"] == "月白"

        # 另一个没带请求头的标签页仍然看到进程级的活跃人格，没有被上面的请求改掉
        default = await client.get("/api/persona/persona", headers=auth)
        assert (await default.json())["name"] == "爱丽丝"

        # 请求头只能是 Latin-1：中文目录名由前端 URL 编码后传来
        chinese = await client.get(
            "/api/persona/persona", headers={**auth, "X-Sirius-Persona": quote("小雪")}
        )
        assert (await chinese.json())["name"] == "小雪酱"

        for bad in ("nobody", "../alice", "..", "alice/../sirius", quote("../alice")):
            response = await client.get(
                "/api/persona/persona", headers={**auth, "X-Sirius-Persona": bad}
            )
            assert response.status == 404, bad
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_glance_tells_her_mood_and_what_happened_recently(tmp_path):
    persona_dir = _persona(tmp_path, "sirius", "月白")
    (persona_dir / "engine_state").mkdir()
    (persona_dir / "engine_state" / "assistant_emotion.json").write_text(
        json.dumps({"valence": 0.85, "arousal": 0.72, "last_updated_at": _iso(timedelta())}),
        encoding="utf-8",
    )
    archive = persona_dir / "archive"
    archive.mkdir()
    lines = [
        {
            "role": "human",
            "speaker_name": "旧友",
            "content": "上周的话",
            "timestamp": _iso(timedelta(days=6)),
        },
        {
            "role": "human",
            "speaker_name": "临雀",
            "content": "月白在吗",
            "timestamp": _iso(timedelta(hours=3)),
        },
        {
            "role": "assistant",
            "speaker_name": "月白",
            "content": "在呀～",
            "timestamp": _iso(timedelta(hours=2)),
        },
        {
            "role": "human",
            "speaker_name": "临雀",
            "content": "晚安",
            "timestamp": _iso(timedelta(minutes=5)),
        },
    ]
    (archive / "10086.jsonl").write_text(
        "\n".join(json.dumps({**line, "group_id": "10086"}, ensure_ascii=False) for line in lines),
        encoding="utf-8",
    )
    units_dir = persona_dir / "memory_units"
    units_dir.mkdir()
    (units_dir / "10086.json").write_text(
        json.dumps(
            {
                "group_id": "10086",
                "units": [
                    {"unit_id": "u1", "summary": "临雀喜欢晚安", "created_at": _iso(timedelta(hours=1))},
                    {"unit_id": "u2", "summary": "很久以前的事", "created_at": _iso(timedelta(days=30))},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    autonomy = persona_dir / "memory" / "autonomy"
    autonomy.mkdir(parents=True)
    (autonomy / "episodes.json").write_text(
        json.dumps(
            {
                "episodes": [
                    {"kind": "reading", "seed": "读完那篇文章", "ended_at": _iso(timedelta(hours=4))}
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    client, auth = await _client(tmp_path)
    try:
        response = await client.get(
            "/api/persona/glance", headers={**auth, "X-Sirius-Persona": "sirius"}
        )
        glance = await response.json()
    finally:
        await client.close()

    assert glance["mood"]["emotion"] in {"JOY", "EXCITEMENT"}
    convo = glance["conversation"]
    assert (convo["heard"], convo["replied"]) == (2, 1)
    assert convo["groups"] == [{"group_id": "10086", "messages": 3}]
    # 最近说过的话从新到旧，窗口外的旧话也留着，安静的时候首页不至于空白
    assert [line["content"] for line in convo["recent"]] == ["晚安", "在呀～", "月白在吗", "上周的话"]
    assert glance["memory"] == {"total": 2, "new": 1}
    assert [episode["seed"] for episode in glance["episodes"]] == ["读完那篇文章"]


@pytest.mark.asyncio
async def test_glance_for_a_brand_new_persona_is_empty_not_an_error(tmp_path):
    _persona(tmp_path, "newbie", "新伙伴")
    client, auth = await _client(tmp_path)
    try:
        response = await client.get(
            "/api/persona/glance", headers={**auth, "X-Sirius-Persona": "newbie"}
        )
        glance = await response.json()
    finally:
        await client.close()
    assert response.status == 200
    assert glance["mood"] is None
    assert glance["conversation"]["recent"] == []
    assert glance["memory"] == {"total": 0, "new": 0}


@pytest.mark.asyncio
async def test_persona_avatar_can_be_uploaded_capped_and_reset(tmp_path):
    persona_dir = _persona(tmp_path, "sirius", "月白")
    client, auth = await _client(tmp_path)
    try:
        default = await client.get("/api/personas/sirius/avatar", headers=auth)
        assert default.status == 200
        assert default.headers["X-Avatar-Custom"] == "0"
        # 内置立绘是 webp：Windows 自带的 mimetypes 不认这个后缀，得由服务端补上
        assert default.headers["Content-Type"] == "image/webp"

        rejected = await client.post(
            "/api/personas/sirius/avatar", data=b"not an image", headers=auth
        )
        assert rejected.status == 400
        assert not (persona_dir / "avatar.png").exists()

        uploaded = await client.post(
            "/api/personas/sirius/avatar",
            data=_png(3000, 1500),
            headers={**auth, "Content-Type": "image/png"},
        )
        assert uploaded.status == 200
        assert (await uploaded.json())["width"] == 2048

        custom = await client.get("/api/personas/sirius/avatar", headers=auth)
        assert custom.headers["X-Avatar-Custom"] == "1"
        assert Image.open(io.BytesIO(await custom.read())).size == (2048, 1024)

        missing = await client.post("/api/personas/ghost/avatar", data=_png(10, 10), headers=auth)
        assert missing.status == 404

        reset = await client.delete("/api/personas/sirius/avatar", headers=auth)
        assert reset.status == 200
        again = await client.get("/api/personas/sirius/avatar", headers=auth)
        assert again.headers["X-Avatar-Custom"] == "0"
    finally:
        await client.close()


@pytest.mark.asyncio
async def test_home_at_root_and_old_console_is_gone(tmp_path, monkeypatch):
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    (app_dir / "index.html").write_text("<title>月白的小屋</title>", encoding="utf-8")
    monkeypatch.setattr(server_core, "APP_DIR", app_dir)
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    client, _auth = await _client(data_dir)
    try:
        for path in ("/", "/app", "/app/"):
            home = await client.get(path)
            assert home.status == 200, path
            assert "月白的小屋" in await home.text(), path
        for gone in ("/legacy/", "/legacy", "/static/index.html"):
            assert (await client.get(gone, allow_redirects=False)).status in (401, 404), gone
    finally:
        await client.close()

    # 源码用户还没构建前端时，根路径说明怎么构建
    monkeypatch.setattr(server_core, "APP_DIR", tmp_path / "missing")
    client, _auth = await _client(data_dir)
    try:
        missing = await client.get("/")
        assert missing.status == 503
        assert "pnpm build" in await missing.text()
    finally:
        await client.close()
