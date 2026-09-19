"""AMKR 任务注册的行为测试。

关注业务结果：在某人格的空间里，缺失的任务名被创建、已存在的任务不被改动、
工作空间按人格隔离、revision 冲突时能重读再试。
"""

from __future__ import annotations

import json

import httpx
import pytest

from sirius_pulse.providers.amkr import AmkrSettings
from sirius_pulse.providers.amkr_sync import (
    AmkrAdminClient,
    AmkrError,
    amkr_ui_url,
    collect_amkr_status,
    inspect_persona_workspace,
    known_task_names,
    register_persona_tasks,
    register_persona_tasks_async,
    register_tasks,
    workspace_for,
)


class _FakeAmkr:
    """一个够用的 AMKR 管理面替身：记录收到的请求，维护任务与 revision。"""

    def __init__(self, tasks=None, *, revision="rev-1", reject_once=False):
        self.tasks = {name: {} for name in (tasks or [])}
        self.revision = revision
        self.requests: list[tuple[str, str, dict, dict]] = []
        self._reject_once = reject_once
        self.health = {
            "status": "ok",
            "version": "1.2.3",
            "ops_enabled": True,
            "webui_mounted": True,
            "webui_path": "/ui",
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.requests.append(
            (
                request.method,
                request.url.path,
                dict(request.headers),
                body,
            )
        )
        if request.url.path == "/health":
            return httpx.Response(200, json=self.health)
        if request.url.path == "/api/tasks" and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    "tasks": [{"name": name} for name in self.tasks],
                    "config_revision": self.revision,
                },
            )
        if request.url.path == "/api/tasks" and request.method == "POST":
            if self._reject_once:
                self._reject_once = False
                return httpx.Response(409, json={"error": "配置版本已变更，请刷新后重试"})
            name = str(body.get("name", ""))
            self.tasks[name] = body
            self.revision = f"rev-{len(self.tasks) + 1}"
            return httpx.Response(200, json={"config_revision": self.revision})
        return httpx.Response(404, json={"error": "未预期的请求"})


def _install(monkeypatch, fake: _FakeAmkr) -> None:
    transport = httpx.MockTransport(fake.handler)
    real_client = httpx.Client

    def _client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr(httpx, "Client", _client)


def _settings(**overrides) -> AmkrSettings:
    base = {
        "base_url": "http://amkr.test",
        "api_key": "sk-local",
        "workspace": "sirius-pulse",
    }
    base.update(overrides)
    return AmkrSettings(**base)


def test_register_tasks_when_amkr_is_empty_then_creates_every_known_task(monkeypatch):
    """全新 AMKR 上，本框架的认知任务名应被逐个注册。"""
    fake = _FakeAmkr()
    _install(monkeypatch, fake)

    result = register_persona_tasks(_settings(), "sirius")

    assert result.ok
    assert sorted(result.created) == sorted(known_task_names())
    assert result.existing == []
    posted = [b for m, p, _, b in fake.requests if m == "POST"]
    assert {b["name"] for b in posted} == set(known_task_names())


def test_register_tasks_when_task_already_exists_then_leaves_it_untouched(monkeypatch):
    """已存在的任务不得改动：模型与参数归 AMKR 侧维护。"""
    fake = _FakeAmkr(tasks=["response_generate"])
    _install(monkeypatch, fake)

    result = register_persona_tasks(_settings(), "sirius")

    assert result.existing == ["response_generate"]
    assert "response_generate" not in result.created
    # 没有任何 PUT/PATCH，也没有针对它的 POST。
    assert all(m == "GET" or m == "POST" for m, _, _, _ in fake.requests)
    assert all(
        b["name"] != "response_generate" for m, _, _, b in fake.requests if m == "POST" and b
    )


def test_register_tasks_when_creating_then_sends_no_model(monkeypatch):
    """注册只提交任务名：模型由 AMKR 侧决定，本框架不预设。"""
    fake = _FakeAmkr()
    _install(monkeypatch, fake)

    register_persona_tasks(_settings(), "sirius", task_names=["memory_extract"])

    body = next(b for m, _, _, b in fake.requests if m == "POST")
    assert body["name"] == "memory_extract"
    assert "model" not in body


def test_register_tasks_when_revision_is_stale_then_rereads_and_retries(monkeypatch):
    """首次提交撞上配置版本变更时应重读 revision 后重试，而不是直接失败。"""
    fake = _FakeAmkr(reject_once=True)
    _install(monkeypatch, fake)

    result = register_persona_tasks(_settings(), "sirius", task_names=["topic_cluster"])

    assert result.ok, result.failed
    assert result.created == ["topic_cluster"]
    assert sum(1 for m, _, _, _ in fake.requests if m == "GET") == 2


def test_register_tasks_when_key_is_rejected_then_reports_amkr_error(monkeypatch):
    """凭据不对时给出可操作的提示，而不是把 401 原样抛出。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"error": "unauthorized"})

    real_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda *a, **k: real_client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(AmkrError, match="amkr_local_api_key"):
        register_persona_tasks(_settings(), "sirius")


def test_register_persona_tasks_when_not_configured_then_raises():
    """没有本地授权 Key 时不该发起请求。"""
    with pytest.raises(AmkrError, match="amkr_local_api_key"):
        register_persona_tasks(_settings(api_key=""), "sirius")


def test_workspace_for_when_persona_given_then_nests_under_base_workspace():
    """每个人格一个子空间，保证同名任务在不同人格下可以指向不同模型。"""
    assert workspace_for(_settings(), "sirius") == "sirius-pulse/sirius"
    assert workspace_for(_settings(workspace="pulse"), "alice") == "pulse/alice"


def test_workspace_for_when_persona_missing_then_uses_base_workspace():
    """没有人格名时退回基础工作空间，不产生以斜杠结尾的空间名。"""
    assert workspace_for(_settings(), "  ") == "sirius-pulse"


def test_register_persona_tasks_when_called_then_uses_persona_workspace(monkeypatch):
    """请求必须带上人格对应的 X-AMKR-Workspace，否则会写进别人的空间。"""
    fake = _FakeAmkr()
    _install(monkeypatch, fake)

    register_persona_tasks(_settings(), "alice", task_names=["plugin_raw"])

    assert all(h.get("x-amkr-workspace") == "sirius-pulse/alice" for _, _, h, _ in fake.requests)


def test_register_tasks_when_task_list_requested_then_returns_revision(monkeypatch):
    """revision 从任务列表读出，供后续创建请求串行携带。"""
    fake = _FakeAmkr(tasks=["a"], revision="rev-42")
    _install(monkeypatch, fake)

    with AmkrAdminClient(_settings()) as client:
        tasks, revision = client.list_tasks()

    assert tasks == [{"name": "a"}]
    assert revision == "rev-42"


@pytest.mark.asyncio
async def test_register_persona_tasks_async_when_called_then_registers(monkeypatch):
    """异步入口与同步入口行为一致，便于在引擎构建的事件循环里直接调用。"""
    fake = _FakeAmkr()
    _install(monkeypatch, fake)

    result = await register_persona_tasks_async(_settings(), "sirius", task_names=["plugin_raw"])

    assert result.created == ["plugin_raw"]
    assert fake.requests


def test_inspect_persona_workspace_when_tasks_partly_exist_then_splits_registered_and_missing(
    monkeypatch,
):
    """巡检要能把「已登记」与「还缺的」分开，运维页据此显示缺口。"""
    fake = _FakeAmkr(tasks=["response_generate", "memory_extract"])
    _install(monkeypatch, fake)

    state = inspect_persona_workspace(
        _settings(), "sirius", task_names=["response_generate", "topic_cluster"]
    )

    assert state.ok
    assert state.registered == ["response_generate"]
    assert state.missing == ["topic_cluster"]
    assert state.workspace == "sirius-pulse/sirius"


def test_inspect_persona_workspace_when_amkr_unreachable_then_reports_error_and_all_missing(
    monkeypatch,
):
    """AMKR 不可达时不应抛异常打断巡检，而是把错误与缺口一并报给页面。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    real_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda *a, **k: real_client(transport=httpx.MockTransport(handler)),
    )

    state = inspect_persona_workspace(_settings(), "sirius", task_names=["response_generate"])

    assert not state.ok
    assert state.missing == ["response_generate"]
    assert "无法连接 AMKR" in state.error


def test_collect_amkr_status_when_unconfigured_then_asks_for_key_without_requests():
    """没配 Key 时直接给出可操作提示，不发起网络请求。"""
    status = collect_amkr_status(_settings(api_key=""), ["sirius"])

    assert status["configured"] is False
    assert status["reachable"] is False
    assert "amkr_local_api_key" in status["error"]
    assert status["workspaces"] == []


def test_collect_amkr_status_when_reachable_then_reports_health_and_ui_url(monkeypatch):
    """页面需要的信息：版本、运维开关、WebUI 外链地址、各人格的任务缺口。"""
    fake = _FakeAmkr(tasks=["response_generate"])
    _install(monkeypatch, fake)

    status = collect_amkr_status(_settings(), ["sirius"])

    assert status["reachable"] is True
    assert status["version"] == "1.2.3"
    assert status["ops_enabled"] is True
    assert status["ui_url"] == "http://amkr.test/ui"
    assert status["known_tasks"] == known_task_names()
    workspace = status["workspaces"][0]
    assert workspace["workspace"] == "sirius-pulse/sirius"
    assert workspace["registered"] == ["response_generate"]
    assert "topic_cluster" in workspace["missing"]


def test_collect_amkr_status_when_not_reachable_then_reports_error(monkeypatch):
    """AMKR 不可达时页面仍要能渲染，因此这里返回带 error 的结构而不是抛异常。"""

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    real_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda *a, **k: real_client(transport=httpx.MockTransport(handler)),
    )

    status = collect_amkr_status(_settings(), ["sirius"])

    assert status["reachable"] is False
    assert "无法连接 AMKR" in status["error"]


def test_collect_amkr_status_when_health_has_no_webui_path_then_falls_back_to_slash_ui():
    """未挂载 WebUI 时 webui_path 为 null，外链应退回 /ui 而不是拼出坏地址。"""
    settings = _settings(base_url="http://amkr.test/")

    assert amkr_ui_url(settings) == "http://amkr.test/ui"
    assert amkr_ui_url(settings, {"webui_path": None}) == "http://amkr.test/ui"
    assert amkr_ui_url(settings, {"webui_path": "/amkr/ui"}) == "http://amkr.test/amkr/ui"
