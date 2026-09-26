"""AMKR 任务注册的行为测试。

关注业务结果：在某人格的空间里，缺失的任务名被创建、已存在的任务不被改动、
工作空间按人格隔离、revision 冲突时能重读再试。
"""

from __future__ import annotations

import json

import httpx
import pytest

from sirius_pulse.providers.amkr import (
    AmkrSettings,
    load_amkr_settings,
    load_inference_keys,
    load_panel_keys,
    save_inference_key,
    save_panel_key,
)
from sirius_pulse.providers.amkr_sync import (
    AmkrAdminClient,
    AmkrError,
    amkr_ui_url,
    collect_amkr_status,
    ensure_persona_workspace_key,
    inspect_persona_workspace,
    known_task_names,
    persona_panel_url,
    register_persona_tasks,
    register_persona_tasks_async,
    register_tasks,
    rotate_persona_inference_key,
    workspace_for,
)


class _FakeAmkr:
    """一个够用的 AMKR 管理面替身：记录收到的请求，维护任务与 revision。

    ``tasks`` 可给一串任务名（默认视为已绑定模型），或给 ``{任务名: 模型}`` 的
    映射——模型传 ``None`` 或空串即表示「登记了但没绑模型」。
    """

    def __init__(self, tasks=None, *, revision="rev-1", reject_once=False, workspaces=None):
        if isinstance(tasks, dict):
            self.tasks = {name: {"name": name, "model": model} for name, model in tasks.items()}
        else:
            self.tasks = {
                name: {"name": name, "model": "wb-deepseek-v4.1-flash"} for name in (tasks or [])
            }
        self.workspaces = set(workspaces or [])
        self.revision = revision
        self.requests: list[tuple[str, str, dict, dict]] = []
        self._reject_once = reject_once
        self._key_seq = 0
        # 记录每个空间最近一次轮换出来的推理 key，供断言使用。
        self.rotated: dict[str, str] = {}
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
        if request.url.path == "/api/workspaces" and request.method == "POST":
            name = str(body.get("name", ""))
            if name in self.workspaces:
                return httpx.Response(409, json={"error": f"工作空间已存在: {name}"})
            self.workspaces.add(name)
            self._key_seq += 1
            return httpx.Response(
                201,
                json={
                    "name": name,
                    "task_count": 0,
                    "api_key": f"amkr_ws_key{self._key_seq}",
                    "inference_key": f"amkr_ik_key{self._key_seq}",
                    "config_revision": self.revision,
                },
            )
        if request.url.path.endswith("/inference-key") and request.method == "POST":
            prefix = "/api/workspaces/"
            workspace = request.url.path[len(prefix) :].removesuffix("/inference-key")
            if workspace not in self.workspaces:
                return httpx.Response(404, json={"error": f"工作空间不存在: {workspace}"})
            self._key_seq += 1
            rotated = f"amkr_ik_rotated{self._key_seq}"
            self.rotated[workspace] = rotated
            return httpx.Response(
                200,
                json={
                    "name": workspace,
                    "inference_key": rotated,
                    "config_revision": self.revision,
                },
            )
        if request.url.path == "/api/tasks" and request.method == "GET":
            return httpx.Response(
                200,
                json={
                    # 真 AMKR 会回任务绑定的模型；未绑定时该字段为空。巡检要能分辨
                    # 「登记了但没绑模型」这种看着正常、一调用就 404 的状态。
                    "tasks": [
                        {"name": name, "model": str(body.get("model") or "")}
                        for name, body in self.tasks.items()
                    ],
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


def _install(monkeypatch, fake: _FakeAmkr, *, handler=None) -> None:
    """把 httpx.Client 换成打向 ``fake`` 的 MockTransport。

    ``handler`` 可换成包装过的实现，用于模拟特定 AMKR 版本的响应差异。
    """
    transport = httpx.MockTransport(handler or fake.handler)
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

    result = register_persona_tasks(_settings(), "sirius", task_names=["autonomy_generate"])

    assert result.ok, result.failed
    assert result.created == ["autonomy_generate"]
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

    assert [item["name"] for item in tasks] == ["a"]
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
        _settings(), "sirius", task_names=["response_generate", "autonomy_generate"]
    )

    assert state.ok
    assert state.registered == ["response_generate"]
    assert state.missing == ["autonomy_generate"]
    assert state.workspace == "sirius-pulse/sirius"


def test_inspect_persona_workspace_when_task_has_no_model_then_reports_it_as_unbound(
    monkeypatch,
):
    """「登记了」不等于「能用」：没绑模型的任务要单独报出来。

    这是自主行为停摆两天的那个状态：``autonomy_generate`` 曾在任务表里，运维页
    于是显示一切正常，而每个回合调用它都 404。只比对任务名的巡检看不出这件事。
    """
    fake = _FakeAmkr(tasks={"response_generate": "wb-deepseek-v4.1-flash", "autonomy_generate": ""})
    _install(monkeypatch, fake)

    state = inspect_persona_workspace(
        _settings(), "sirius", task_names=["response_generate", "autonomy_generate"]
    )

    assert state.ok
    assert state.registered == ["response_generate", "autonomy_generate"]
    assert state.missing == []
    assert state.unbound == ["autonomy_generate"]


def test_inspect_persona_workspace_when_all_tasks_are_bound_then_unbound_is_empty(monkeypatch):
    """全部绑好模型时不该报出假警，否则运维会开始忽略这一栏。"""
    fake = _FakeAmkr(tasks=["response_generate", "autonomy_generate"])
    _install(monkeypatch, fake)

    state = inspect_persona_workspace(
        _settings(), "sirius", task_names=["response_generate", "autonomy_generate"]
    )

    assert state.registered == ["response_generate", "autonomy_generate"]
    assert state.unbound == []


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
    assert "autonomy_generate" in workspace["missing"]


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


# ── 后端地址与浏览器地址的分工 ─────────────────────────────


def test_browser_base_url_when_unset_then_falls_back_to_backend_url():
    """单机部署下两者本就是同一个地址，不该强迫运维填两遍。"""
    settings = _settings(base_url="http://amkr.test/")

    assert settings.browser_base_url == "http://amkr.test"


def test_browser_base_url_when_backend_is_loopback_then_falls_back_to_same_origin_proxy():
    """回环地址在**用户浏览器**里指向用户自己的机器，不能当作面板地址下发。

    面板必须与取数接口同源（AMKR 不发 CORS 头），而浏览器又是先打开本框架的
    WebUI，因此唯一处处可用的地址就是本框架源上的反代路径。
    """
    for base in (
        "http://127.0.0.1:28881",
        "http://127.0.0.1:8000",
        "http://localhost:28881",
        "http://[::1]:28881",
    ):
        assert _settings(base_url=base).browser_base_url == "/amkr"


def test_persona_panel_url_when_backend_is_loopback_then_embeds_same_origin_path(
    tmp_path,
):
    """同机部署是默认形态：面板地址必须落在本框架自己的源上。

    这是本次修复的核心——此前这里会拼出一个远程 AMKR 域名（或回环地址），
    iframe 于是加载到别的实例、或干脆加载不出来。
    """
    save_panel_key(tmp_path, "sirius-pulse/sirius", "amkr_ws_secret")

    url = persona_panel_url(_settings(base_url="http://127.0.0.1:28881"), "sirius", tmp_path)

    assert url == "/amkr/ui/panel.html#k=amkr_ws_secret"
    assert "127.0.0.1" not in url
    # 同源相对路径：浏览器按当前页面的源解析，因而与取数接口天然同源。
    assert "sparrived.xyz" not in url


def test_browser_base_url_when_non_loopback_backend_then_used_as_is():
    """跨机部署时后端地址本身就是浏览器可达的，不该被改写成反代路径。"""
    assert (
        _settings(base_url="http://192.168.2.181:28881").browser_base_url
        == "http://192.168.2.181:28881"
    )
    assert (
        _settings(base_url="https://amkr.example.com").browser_base_url
        == "https://amkr.example.com"
    )


def test_load_amkr_settings_when_public_url_stored_then_reads_it(tmp_path):
    """「AMKR 浏览器地址」与「AMKR 地址」都必须能从全局配置读出来。"""
    (tmp_path / "global_config.json").write_text(
        json.dumps(
            {
                "amkr_base_url": "http://127.0.0.1:28881",
                "amkr_local_api_key": "sk-local",
                "amkr_public_url": "https://amkr.example.com/",
            }
        ),
        encoding="utf-8",
    )

    settings = load_amkr_settings(tmp_path)

    assert settings.base_url == "http://127.0.0.1:28881"
    assert settings.browser_base_url == "https://amkr.example.com"


def test_amkr_ui_url_when_public_url_set_then_uses_it_for_browser_link():
    """同机部署时后端走回环最省事，但那个地址在用户浏览器里指向用户自己的机器。

    运维页外链与面板 iframe 都是**浏览器**去访问，因此必须用 amkr_public_url。
    """
    settings = _settings(base_url="http://127.0.0.1:28881", public_url="https://amkr.example.com")

    assert amkr_ui_url(settings) == "https://amkr.example.com/ui"
    # 后端自己仍然连回环——面板数据由浏览器直连获取，不由后端代取。
    assert settings.base_url == "http://127.0.0.1:28881"


def test_persona_panel_url_when_public_url_set_then_uses_browser_origin(monkeypatch, tmp_path):
    """iframe 的 src 必须是浏览器能解析的源，否则面板永远加载不出来。"""
    fake = _FakeAmkr()
    _install(monkeypatch, fake)
    settings = _settings(base_url="http://127.0.0.1:28881", public_url="https://amkr.example.com")
    save_panel_key(tmp_path, "sirius-pulse/sirius", "amkr_ws_secret")

    url = persona_panel_url(settings, "sirius", tmp_path)

    assert url == "https://amkr.example.com/ui/panel.html#k=amkr_ws_secret"
    assert "127.0.0.1" not in url


# ── 工作空间与面板 key ─────────────────────────────────────


def test_ensure_persona_workspace_key_when_new_then_creates_workspace_and_saves_key(
    monkeypatch, tmp_path
):
    """首次接入：建空间拿 key 并存到本地——这是唯一的获取时机。"""
    fake = _FakeAmkr()
    _install(monkeypatch, fake)

    key = ensure_persona_workspace_key(_settings(), "sirius", tmp_path)

    assert key == "amkr_ws_key1"
    created = next(b for m, p, _, b in fake.requests if m == "POST" and p == "/api/workspaces")
    assert created["name"] == "sirius-pulse/sirius"
    assert "api_key" not in created  # 让 AMKR 生成，本框架不自造格式
    assert load_panel_keys(tmp_path) == {"sirius-pulse/sirius": "amkr_ws_key1"}


def test_ensure_persona_workspace_key_when_already_stored_then_does_not_touch_amkr(
    monkeypatch, tmp_path
):
    """已存过 key 就不该再建空间：AMKR 对已存在的空间返回 409，徒增噪音。"""
    fake = _FakeAmkr()
    _install(monkeypatch, fake)
    save_panel_key(tmp_path, "sirius-pulse/sirius", "amkr_ws_saved")

    key = ensure_persona_workspace_key(_settings(), "sirius", tmp_path)

    assert key == "amkr_ws_saved"
    assert fake.requests == []


def test_ensure_persona_workspace_key_when_workspace_exists_without_key_then_explains_recovery(
    monkeypatch, tmp_path
):
    """空间已在 AMKR 侧存在但没有本地 key 时，必须明确告知无法自动恢复。"""
    fake = _FakeAmkr(workspaces=["sirius-pulse/sirius"])
    _install(monkeypatch, fake)

    with pytest.raises(AmkrError, match="工作空间已存在"):
        ensure_persona_workspace_key(_settings(), "sirius", tmp_path)

    assert load_panel_keys(tmp_path) == {}


def test_register_persona_tasks_when_provisioning_then_creates_workspace_before_tasks(
    monkeypatch, tmp_path
):
    """建空间必须发生在注册任务之前，否则拿不到面板 key。"""
    fake = _FakeAmkr()
    _install(monkeypatch, fake)

    result = register_persona_tasks(
        _settings(), "sirius", task_names=["plugin_raw"], global_data_path=tmp_path
    )

    assert result.created == ["plugin_raw"]
    posts = [p for m, p, _, _ in fake.requests if m == "POST"]
    assert posts[0] == "/api/workspaces"
    assert posts.count("/api/tasks") == 1
    assert load_panel_keys(tmp_path) == {"sirius-pulse/sirius": "amkr_ws_key1"}


def test_persona_panel_url_when_key_stored_then_puts_credential_in_fragment(monkeypatch, tmp_path):
    """凭据必须在 fragment 里：查询串会进 Referer 与服务端日志。"""
    save_panel_key(tmp_path, "sirius-pulse/sirius", "amkr_ws_secret")

    url = persona_panel_url(_settings(), "sirius", tmp_path)

    assert url == "http://amkr.test/ui/panel.html#k=amkr_ws_secret"
    assert "?" not in url


def test_persona_panel_url_when_no_key_stored_then_returns_empty(tmp_path):
    """没有 key 时返回空串，由调用方决定如何提示，而不是拼一个 401 的地址。"""
    assert persona_panel_url(_settings(), "sirius", tmp_path) == ""


def test_collect_amkr_status_when_key_stored_then_marks_panel_ready(monkeypatch, tmp_path):
    """巡检要能告诉运维「这个空间的面板能不能用」。"""
    fake = _FakeAmkr(tasks=["response_generate"])
    _install(monkeypatch, fake)
    save_panel_key(tmp_path, "sirius-pulse/sirius", "amkr_ws_secret")

    status = collect_amkr_status(_settings(), ["sirius"], global_data_path=tmp_path)

    assert status["reachable"] is True
    assert status["workspaces"][0]["panel_ready"] is True
    # 状态里绝不出现 key 本身：它是管理员接口的事。
    assert "amkr_ws_secret" not in json.dumps(status, ensure_ascii=False)


def test_collect_amkr_status_when_no_key_stored_then_panel_not_ready(monkeypatch, tmp_path):
    """没有面板 key 的空间要显式标出来，页面才能提示运维。"""
    fake = _FakeAmkr()
    _install(monkeypatch, fake)

    status = collect_amkr_status(_settings(), ["sirius"], global_data_path=tmp_path)

    assert status["workspaces"][0]["panel_ready"] is False


# ── 工作空间推理 key ───────────────────────────────────────


def test_ensure_persona_workspace_key_when_new_then_saves_both_credentials(monkeypatch, tmp_path):
    """建空间是拿两把 key 的唯一时机，必须一起存下来，否则推理 key 就丢了。

    丢了只能靠轮换补，而轮换会作废已有的那把——所以这里漏存是有真实代价的。
    """
    fake = _FakeAmkr()
    _install(monkeypatch, fake)

    key = ensure_persona_workspace_key(_settings(), "sirius", tmp_path)

    assert key == "amkr_ws_key1"
    assert load_panel_keys(tmp_path) == {"sirius-pulse/sirius": "amkr_ws_key1"}
    assert load_inference_keys(tmp_path) == {"sirius-pulse/sirius": "amkr_ik_key1"}


def test_create_workspace_when_inference_key_missing_then_errors_instead_of_degrading(
    monkeypatch, tmp_path
):
    """老版本 AMKR 不返回 inference_key，必须明确报错而不是静默退化成全权凭据。

    退化的后果是模型调用继续动用管理员 key，而运维会以为已经收窄了。
    """
    fake = _FakeAmkr()

    def legacy_handler(request: httpx.Request) -> httpx.Response:
        response = fake.handler(request)
        if request.url.path == "/api/workspaces" and request.method == "POST":
            payload = response.json()
            payload.pop("inference_key", None)  # 模拟不支持该字段的 AMKR
            return httpx.Response(response.status_code, json=payload)
        return response

    _install(monkeypatch, fake, handler=legacy_handler)

    with pytest.raises(AmkrError, match="推理 key"):
        ensure_persona_workspace_key(_settings(), "sirius", tmp_path)

    assert load_inference_keys(tmp_path) == {}


def test_rotate_persona_inference_key_when_called_then_stores_new_key(monkeypatch, tmp_path):
    """轮换要落到本地：否则引擎下次构建时又拿到旧 key（已失效）。"""
    fake = _FakeAmkr(workspaces=["sirius-pulse/sirius"])
    _install(monkeypatch, fake)

    key = rotate_persona_inference_key(_settings(), "sirius", global_data_path=tmp_path)

    assert key == "amkr_ik_rotated1"
    assert load_inference_keys(tmp_path) == {"sirius-pulse/sirius": "amkr_ik_rotated1"}
    posted = [p for m, p, _, _ in fake.requests if m == "POST"]
    assert posted == ["/api/workspaces/sirius-pulse/sirius/inference-key"]


def test_rotate_persona_inference_key_when_amkr_rejects_then_keeps_existing_key(
    monkeypatch, tmp_path
):
    """轮换失败不能损坏已有凭据：本地仍要留着原来那把可用的 key。"""
    fake = _FakeAmkr(workspaces=[])  # 空间不存在 → 404
    _install(monkeypatch, fake)
    save_inference_key(tmp_path, "sirius-pulse/sirius", "amkr_ik_existing")

    with pytest.raises(AmkrError):
        rotate_persona_inference_key(_settings(), "sirius", global_data_path=tmp_path)

    assert load_inference_keys(tmp_path) == {"sirius-pulse/sirius": "amkr_ik_existing"}


def test_collect_amkr_status_when_inference_key_absent_then_not_ready(monkeypatch, tmp_path):
    """巡检要能标出「这个空间还用不了」——它缺的是模型调用的那把 key。"""
    fake = _FakeAmkr(tasks=["response_generate"])
    _install(monkeypatch, fake)
    save_panel_key(tmp_path, "sirius-pulse/sirius", "amkr_ws_secret")

    status = collect_amkr_status(_settings(), ["sirius"], global_data_path=tmp_path)

    assert status["workspaces"][0]["panel_ready"] is True
    assert status["workspaces"][0]["inference_ready"] is False
    # 状态里绝不出现任何 key，包括推理 key。
    assert "amkr_ik" not in json.dumps(status, ensure_ascii=False)


def test_collect_amkr_status_when_inference_key_stored_then_ready(monkeypatch, tmp_path):
    """两把 key 齐备时才报告就绪。"""
    fake = _FakeAmkr(tasks=["response_generate"])
    _install(monkeypatch, fake)
    save_panel_key(tmp_path, "sirius-pulse/sirius", "amkr_ws_secret")
    save_inference_key(tmp_path, "sirius-pulse/sirius", "amkr_ik_secret")

    status = collect_amkr_status(_settings(), ["sirius"], global_data_path=tmp_path)

    assert status["workspaces"][0]["inference_ready"] is True
