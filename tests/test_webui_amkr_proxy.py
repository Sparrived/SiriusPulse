"""AMKR 同源反代的行为测试。

这些测试站在这条路径的**使用者**角度：运维页要嵌的那个面板能不能加载、能不能取数，
以及这条免 JWT 的路径有没有被收窄到只剩面板需要的那几条。因此断言的是「浏览器打
``/amkr/...`` 会得到什么」，而不是内部函数怎么拆分路径。
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from sirius_pulse.utils.json_io import atomic_write_json
from sirius_pulse.webui.amkr_proxy import AmkrProxy, setup_amkr_proxy_routes
from sirius_pulse.webui.middleware import auth_middleware

# 上游收到的请求：(method, path_with_query, headers, body)
UpstreamRequest = tuple[str, str, dict[str, str], bytes]


def _install_upstream(monkeypatch, handler) -> list[UpstreamRequest]:
    """把 httpx.AsyncClient 接到一个内存里的假 AMKR 上，并记录收到的请求。

    ``handler`` 可以是普通函数或协程函数（httpx 两种都支持）。
    """
    seen: list[UpstreamRequest] = []

    async def routed(request: httpx.Request) -> httpx.Response:
        seen.append(
            (request.method, request.url.raw_path.decode(), dict(request.headers), request.content)
        )
        result = handler(request)
        if not isinstance(result, httpx.Response):
            result = await result
        return result

    real_client = httpx.AsyncClient

    def _client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(routed)
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", _client)
    return seen


def _write_config(tmp_path: Path, base_url: str = "http://amkr.upstream") -> None:
    atomic_write_json(
        tmp_path / "global_config.json",
        {"amkr_base_url": base_url, "amkr_local_api_key": "sk-x", "amkr_workspace": "sp"},
    )


async def _proxy_client(tmp_path: Path) -> TestClient:
    """起一个只挂了反代与认证中间件的 app——面板走的正是这条免 JWT 的路径。"""
    app = web.Application(middlewares=[auth_middleware])
    proxy = AmkrProxy(tmp_path)
    setup_amkr_proxy_routes(app, proxy)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


def _ok(body: bytes = b"panel", **headers) -> httpx.Response:
    return httpx.Response(200, content=body, headers=headers)


# ── 面板加载所需的转发行为 ─────────────────────────────────


@pytest.mark.asyncio
async def test_panel_page_is_served_without_sirius_jwt(tmp_path, monkeypatch):
    """面板是 iframe 里的独立文档，带的是 AMKR 的面板 key 而非 Sirius 的 JWT。

    因此这条路径必须免 JWT，否则运维页里那个 iframe 永远是登录页。
    """
    _write_config(tmp_path)
    _install_upstream(monkeypatch, lambda request: _ok(b"<html>panel</html>"))
    client = await _proxy_client(tmp_path)

    response = await client.get("/amkr/ui/panel.html")

    assert response.status == 200
    assert await response.text() == "<html>panel</html>"


@pytest.mark.asyncio
async def test_prefix_is_stripped_and_query_preserved(tmp_path, monkeypatch):
    """AMKR 挂在根路径：转发前必须剥掉 /amkr，同时保留查询串与面板 key 头。"""
    _write_config(tmp_path)
    seen = _install_upstream(
        monkeypatch,
        lambda request: httpx.Response(401, json={"detail": "本地 API key 验证失败"}),
    )
    client = await _proxy_client(tmp_path)

    response = await client.get(
        "/amkr/ui/workspace-panel.json?hours=24&all_history=true",
        headers={"Authorization": "Bearer amkr_ws_panel"},
    )

    assert response.status == 401
    method, path, headers, _body = seen[0]
    assert (method, path) == ("GET", "/ui/workspace-panel.json?hours=24&all_history=true")
    # 面板自己的凭据要原样透传：反代不注入任何密钥。
    assert headers["authorization"] == "Bearer amkr_ws_panel"
    assert json.loads(await response.text()) == {"detail": "本地 API key 验证失败"}


@pytest.mark.asyncio
async def test_panel_task_writes_reach_upstream_unchanged(tmp_path, monkeypatch):
    """面板改任务配置走 PUT/DELETE /api/tasks/...，方法与正文都要原样送达。"""
    _write_config(tmp_path)
    seen = _install_upstream(monkeypatch, lambda request: _ok(b'{"ok":true}'))
    client = await _proxy_client(tmp_path)

    body = {"config_revision": 7, "name": "response_generate"}
    put = await client.put("/amkr/api/tasks/response_generate", json=body)
    delete = await client.delete("/amkr/api/tasks/response_generate", json=body)

    assert put.status == 200 and delete.status == 200
    assert [(m, p) for m, p, _, _ in seen] == [
        ("PUT", "/api/tasks/response_generate"),
        ("DELETE", "/api/tasks/response_generate"),
    ]
    # 正文确实被转发（httpx 会重新编码，因此按 JSON 内容比对而不是字节相等）。
    assert json.loads(seen[0][3]) == body
    assert json.loads(seen[1][3]) == body


@pytest.mark.asyncio
async def test_upstream_redirect_is_moved_under_the_proxy_prefix(tmp_path, monkeypatch):
    """AMKR 的 /ui 会 301 到 /ui/：不改写的话浏览器会去本框架根下找，必然 404。"""
    _write_config(tmp_path)
    _install_upstream(
        monkeypatch,
        lambda request: httpx.Response(301, headers={"Location": "/ui/"}),
    )
    client = await _proxy_client(tmp_path)

    response = await client.get("/amkr/ui", allow_redirects=False)

    assert response.status == 301
    assert response.headers["Location"] == "/amkr/ui/"


@pytest.mark.asyncio
async def test_unreachable_amkr_reports_502_instead_of_hanging(tmp_path, monkeypatch):
    """AMKR 没起来时要给一个明确的错误，而不是让面板白屏。"""
    _write_config(tmp_path, base_url="http://amkr.upstream")

    async def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    _install_upstream(monkeypatch, boom)
    client = await _proxy_client(tmp_path)

    response = await client.get("/amkr/ui/panel.html")

    assert response.status == 502
    assert "无法连接 AMKR" in json.loads(await response.text())["error"]


# ── 路径闸门：这条免 JWT 的路径不能被用来管理整个 AMKR ──────


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        "/amkr/api/providers",
        "/amkr/api/settings",
        "/amkr/api/logs",
        "/amkr/api/workspaces",
        "/amkr/docs",
        "/amkr/openapi.json",
        "/amkr/redoc",
    ],
)
async def test_management_and_introspection_paths_are_refused(tmp_path, monkeypatch, path):
    """管理面与自描述文档不在这条免 JWT 路径的放行范围内。

    它们虽然各自还要 AMKR 的本地 key，但那把 key 的权限是「增删供应商与 Key 池」，
    远大于「看一个空间的用量」。面板需要什么就放行什么。
    """
    _write_config(tmp_path)
    seen = _install_upstream(monkeypatch, lambda request: _ok())
    client = await _proxy_client(tmp_path)

    response = await client.get(path)

    assert response.status == 403
    assert "白名单" in json.loads(await response.text())["error"]
    # 关键：被拒的请求根本没出网。
    assert seen == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "path",
    [
        # 编码过的 .. —— 解码后以 /ui/ 开头，看似放行，但 httpx 会还原 %2e 并把
        # .. 走掉，实际会打到 /api/providers。
        "/amkr/ui/%2e%2e/api/providers",
        "/amkr/ui/..%2fapi%2fproviders",
    ],
)
async def test_encoded_traversal_cannot_reach_management_api(tmp_path, monkeypatch, path):
    """白名单看的是解码后的路径，必须在转发前挡住路径穿越。

    否则任何人只要打开运维页的源，就能不带 Sirius 凭据摸到 AMKR 的管理接口。
    """
    _write_config(tmp_path)
    seen = _install_upstream(monkeypatch, lambda request: _ok())
    client = await _proxy_client(tmp_path)

    response = await client.get(path)

    assert response.status == 403
    # 关键：请求根本没出网，上游看不到它。
    assert seen == []


@pytest.mark.asyncio
async def test_literal_traversal_never_reaches_upstream(tmp_path, monkeypatch):
    """字面 ``..`` 会被客户端在发出前归一化掉，因而根本进不了 /amkr。

    这一层由归一化 + 认证中间件兜住（归一化后是 Sirius 自己的路径，要 JWT），
    编码形式则由反代的闸门兜住。两种写法的共同底线是：**上游看不到这个请求**。
    """
    _write_config(tmp_path)
    seen = _install_upstream(monkeypatch, lambda request: _ok())
    client = await _proxy_client(tmp_path)

    response = await client.get("/amkr/ui/../../api/providers")

    assert response.status in (401, 403, 404)
    assert seen == []


@pytest.mark.asyncio
async def test_health_can_be_probed_through_the_proxy(tmp_path, monkeypatch):
    """巡检要打 /health（AMKR 侧免鉴权），同源路径下也得能通。"""
    _write_config(tmp_path)
    _install_upstream(monkeypatch, lambda request: _ok(b'{"version":"6.0.0"}'))
    client = await _proxy_client(tmp_path)

    response = await client.get("/amkr/health")

    assert response.status == 200
    assert json.loads(await response.text())["version"] == "6.0.0"


@pytest.mark.asyncio
async def test_panel_base_path_redirect_lands_inside_the_proxy(tmp_path, monkeypatch):
    """把 /amkr/ui 的跳转跟到底，最终要落在同源的面板页上。

    这是运维点面板的真实路径：AMKR 的 /ui 会 301 到 /ui/，改写后必须仍在 /amkr/
    之下，否则浏览器会跑到本框架根路径去找。
    """
    _write_config(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/ui":
            return httpx.Response(301, headers={"Location": "/ui/"})
        return _ok(b"<html>panel</html>")

    _install_upstream(monkeypatch, handler)
    client = await _proxy_client(tmp_path)

    response = await client.get("/amkr/ui")

    assert response.status == 200
    assert await response.text() == "<html>panel</html>"
    assert str(response.url).endswith("/amkr/ui/")


@pytest.mark.asyncio
async def test_panel_assets_are_not_cached(tmp_path, monkeypatch):
    """AMKR 对 /ui/* 不发缓存头；不禁掉的话浏览器会启发式缓存旧脚本。

    症状是 AMKR 升级后面板行为与新版本对不上，却完全看不出原因。
    """
    _write_config(tmp_path)
    _install_upstream(monkeypatch, lambda request: _ok(b"// panel"))
    client = await _proxy_client(tmp_path)

    response = await client.get("/amkr/ui/panel.js")

    assert response.headers["Cache-Control"] == "no-store"


@pytest.mark.asyncio
async def test_compression_is_not_passed_through_inconsistently(tmp_path, monkeypatch):
    """上游被要求发未压缩正文：否则 content-encoding 与正文对不上，浏览器解错。"""
    _write_config(tmp_path)
    seen = _install_upstream(monkeypatch, lambda request: _ok())
    client = await _proxy_client(tmp_path)

    await client.get("/amkr/ui/panel.js", headers={"Accept-Encoding": "gzip, br"})

    assert seen[0][2]["accept-encoding"] == "identity"
