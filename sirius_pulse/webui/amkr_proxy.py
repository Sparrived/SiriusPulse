"""把 AMKR 自带的 WebUI 反代到本框架的**同源**路径 ``/amkr/`` 下。

## 为什么要反代

AMKR 把面板挂在自己的根路径（``/ui/``、``/api/*``）。生产部署里 AMKR 通常只发布到
宿主回环（``127.0.0.1:28881``），供本框架的容器从服务端调用——但这个地址在**用户
浏览器**里指向用户自己的机器，面板 iframe 必然加载失败。

更关键的是 AMKR **不发任何 CORS 头**，所以面板页面与它调用的接口必须**同源**。把
AMKR 挂到本框架域名下的一条路径上，是同时满足「浏览器可达」与「同源」的唯一做法：
运维用哪个地址打开 WebUI，面板就在哪个源上，不必再为 AMKR 单独申请域名或证书。

面板自身能适配子路径——它的 ``apiBase()`` 从 ``location.pathname`` 里找 ``/ui/``
并截取前缀（见 AMKR ``webui/api.js``），因此在 ``/amkr/ui/panel.html`` 上它会正确地
把请求发到 ``/amkr/api/...``。前提是**页面地址里必须真的含 ``/ui/``**，所以本模块
只做「剥掉 ``/amkr`` 前缀」，不改写其余路径。

## 凭据从哪来

本模块**不注入任何密钥**，透传浏览器带来的 ``Authorization``：

- 工作空间面板（本框架运维页要嵌的那个）从 URL fragment 取自己的**面板 key**
  （``#k=amkr_ws_…``）并逐条请求带上。fragment 不发给服务端，也不进日志——这正是
  AMKR 的硬要求。因此面板天然「免输入 key」，且拿到的是一把只对自己空间有效的受限
  凭据。
- 管理台自带 WebUI 用 localStorage 里的管理员 key，同样由它自己带上。

反过来，若在这里替浏览器注入 ``amkr_local_api_key``，就等于开出一条**无需 Sirius
认证即可管理整个 AMKR** 的同源路径（那条路径要为 iframe 免 JWT，见中间件白名单）。
AMKR 的 key 一旦能这样被代理转发，增删供应商与 Key 池就只隔一个 URL，这与
``docs/modules/provider-config`` 里「管理凭据只留在服务端」的边界直接冲突。

## 只放行面板需要的那几条

因此这里同时是一道**路径闸门**：只透传面板用得到的 ``/ui/*``（静态资源与
``workspace-panel.json``）与 ``/api/tasks``。AMKR 的自描述文档（``/docs``、
``/openapi.json``、``/redoc``）与运维接口（``/api/logs``、``/api/tool``、
``/api/service/*``、``/api/integrations/*``）一律 403——它们在 AMKR 侧虽只要本地
key，但那把 key 的权限远大于「看一个空间的用量」。
"""

from __future__ import annotations

import logging
from pathlib import Path

import httpx
from aiohttp import web

from sirius_pulse.providers.amkr import AMKR_PROXY_PREFIX, AmkrSettings, load_amkr_settings

LOG = logging.getLogger("sirius.webui.amkr_proxy")

#: 逐跳首部（RFC 9110 §7.6.1）：只对单条连接有意义，不能透传给下游/上游。
_HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "transfer-encoding",
        "upgrade",
    }
)

#: 转发时丢弃的请求首部：``host`` 会钉死上游虚拟主机，``content-length`` 由 httpx
#: 按重新编码后的正文计算，``accept-encoding`` 见 ``_forward_headers``。
_DROP_REQUEST_HEADERS = frozenset({"host", "content-length", "accept-encoding"})

#: 允许透传的精确上游路径：``/health``（巡检）与 ``/ui``（面板基址，会 301 到
#: ``/ui/``，改写后正好落在下面的前缀上）。
_ALLOWED_EXACT = frozenset({"/health", "/ui"})

#: 允许透传的上游路径前缀：面板静态资源与用量接口，以及面板唯一的写接口。
_ALLOWED_PREFIXES = ("/ui/", "/api/tasks/")

#: 面板的接口路径恰好等于 ``/api/tasks``（无尾斜杠），需要单独放行。
_ALLOWED_TASKS_PATH = "/api/tasks"


def _is_allowed(upstream_path: str) -> bool:
    """判断一条上游路径是否属于「面板用得到」的集合。"""
    if upstream_path in _ALLOWED_EXACT or upstream_path == _ALLOWED_TASKS_PATH:
        return True
    return any(upstream_path.startswith(prefix) for prefix in _ALLOWED_PREFIXES)


def _upstream_path(request: web.Request) -> str | None:
    """把请求路径里的 ``/amkr`` 前缀剥掉，得到上游路径；路径可疑时返回 ``None``。

    ``/amkr/ui/x`` → ``/ui/x``。转发用 ``raw_path``（保留百分号编码，避免二次解码
    改变语义），而**闸门检查用解码后的路径**——两者都看，才能挡住编码绕行。

    单看任一种都会被绕过：``/amkr/ui/%2e%2e/api/providers`` 的解码形式以 ``/ui/``
    开头、看似放行，但 httpx 会把 ``%2e`` 还原并把 ``..`` 走掉，最终打到
    ``/api/providers``——即借着 Sirius 的源、在无 Sirius 认证的情况下摸到 AMKR 的
    管理接口。含 ``.``/``..`` 段或 ``%2e`` 的路径一律拒绝。（字面 ``..`` 在 aiohttp
    的路由之前就被归一化掉了，因此这一层主要防的是编码形式。）
    """
    segment = AMKR_PROXY_PREFIX + "/"
    raw_path = request.rel_url.raw_path
    decoded_path = request.path
    raw_upstream = raw_path[len(AMKR_PROXY_PREFIX) :] if raw_path.startswith(segment) else raw_path
    decoded_upstream = (
        decoded_path[len(AMKR_PROXY_PREFIX) :] if decoded_path.startswith(segment) else decoded_path
    )

    if any(part in (".", "..") for part in decoded_upstream.split("/")):
        return None
    if "%2e" in raw_path.lower():
        return None
    if not _is_allowed(decoded_upstream):
        return None
    return raw_upstream


def _forward_headers(headers: "web.CIMultiDictProxy[str]") -> dict[str, str]:
    """挑出可以安全转发的请求首部。

    ``accept-encoding`` 被固定成 ``identity``：上游若压缩，httpx 解压后
    ``content-encoding`` 与实际正文就不再一致，透传下去浏览器会解错。面板资源都很
    小，不做压缩最省心。
    """
    forwarded = {
        name: value
        for name, value in headers.items()
        if name.lower() not in _HOP_BY_HOP and name.lower() not in _DROP_REQUEST_HEADERS
    }
    forwarded["accept-encoding"] = "identity"
    return forwarded


def _response_headers(headers: "httpx.Headers", *, upstream_path: str) -> dict[str, str]:
    """挑出可以安全回给浏览器的响应首部。"""
    forwarded = {
        name: value
        for name, value in headers.items()
        if name.lower() not in _HOP_BY_HOP
        # 上游的正文长度在流式转发里不再可靠，交给 aiohttp 决定。
        and name.lower() not in ("content-length", "content-encoding")
    }
    if upstream_path.startswith("/ui/") or upstream_path == "/ui":
        # AMKR 对 /ui/* 不发任何缓存头。没有验证器时浏览器会**启发式**缓存，AMKR
        # 升级后面板可能还跑着旧脚本，症状是面板行为与新版本对不上却看不出原因。
        # 这些资源很小，直接禁掉缓存最省心（与本框架 /static/ 的处理一致）。
        forwarded["cache-control"] = "no-store"
    return forwarded


def _rewrite_location(location: str) -> str:
    """把上游的根路径跳转挪到反代前缀下。

    AMKR 的 ``/ui`` 会 301 到 ``/ui/``。原样透传的话浏览器会去本框架根的 ``/ui/``
    找，那里什么都没有。只改以 ``/`` 开头的绝对路径；相对跳转（如 ``./``）在
    ``/amkr/ui/`` 这个层级上本来就是对的。
    """
    if location.startswith("/") and not location.startswith(AMKR_PROXY_PREFIX + "/"):
        return AMKR_PROXY_PREFIX + location
    return location


class AmkrProxy:
    """AMKR 同源反向代理。

    连接复用在一个 ``httpx.AsyncClient`` 上，随 WebUI 生命周期创建与关闭。
    每次请求都重新读一遍配置：运维在「全局设置」里改了 AMKR 地址后无需重启。
    """

    def __init__(self, data_dir: Path) -> None:
        self._data_dir = Path(data_dir)
        self._client: httpx.AsyncClient | None = None

    def settings(self) -> AmkrSettings:
        """当前 AMKR 连接配置（每次现读，支持热改地址）。"""
        return load_amkr_settings(self._data_dir)

    async def _upstream_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(30.0, read=120.0),
                # 跳转由本模块改写成带前缀的地址，不该由 httpx 悄悄跟过去。
                follow_redirects=False,
            )
        return self._client

    async def close(self) -> None:
        """关闭连接池（WebUI 停止时调用）。"""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    async def handle(self, request: web.Request) -> web.StreamResponse:
        """转发一条 ``/amkr/*`` 请求到配置里的 AMKR。"""
        upstream_path = _upstream_path(request)
        if upstream_path is None:
            LOG.warning("拒绝转发未放行的 AMKR 路径: %s", request.raw_path)
            return web.json_response(
                {
                    "error": (
                        "该路径不在 AMKR 反代白名单内。同源反代只服务工作空间面板"
                        "（/amkr/ui/* 与 /amkr/api/tasks）。AMKR 的管理台与运维接口"
                        "请直接访问 AMKR 自身地址并携带管理员 key。"
                    )
                },
                status=403,
            )

        settings = self.settings()
        if not settings.base_url.strip():
            return web.json_response({"error": "尚未配置 AMKR 地址（amkr_base_url）"}, status=503)

        # 闸门用解码后的路径判定，转发用剥掉前缀的原始路径（保留编码语义）。
        upstream_request_path = upstream_path
        if request.rel_url.raw_query_string:
            upstream_request_path = f"{upstream_path}?{request.rel_url.raw_query_string}"
        url = f"{settings.base_url.rstrip('/')}{upstream_request_path}"

        body = await request.read() if request.can_read_body else None
        client = await self._upstream_client()
        try:
            upstream_response = await client.send(
                client.build_request(
                    request.method,
                    url,
                    headers=_forward_headers(request.headers),
                    content=body,
                ),
                stream=True,
            )
        except httpx.HTTPError as exc:
            LOG.warning("反代 AMKR 失败: %s %s -> %s", request.method, url, exc)
            return web.json_response({"error": f"无法连接 AMKR: {exc}"}, status=502)

        headers = _response_headers(upstream_response.headers, upstream_path=upstream_path)
        if "location" in upstream_response.headers:
            headers["location"] = _rewrite_location(upstream_response.headers["location"])

        response = web.StreamResponse(status=upstream_response.status_code, headers=headers)
        try:
            await response.prepare(request)
            # 204/304 不允许有正文，写了会被 aiohttp 拒绝。
            if upstream_response.status_code not in (204, 304):
                async for chunk in upstream_response.aiter_bytes():
                    await response.write(chunk)
            await response.write_eof()
        except (ConnectionResetError, ConnectionAbortedError):
            # 浏览器在面板加载完之前关掉标签页是常事，不是错误。
            LOG.debug("客户端提前断开: %s", request.path)
        finally:
            await upstream_response.aclose()
        return response


def setup_amkr_proxy_routes(app: web.Application, proxy: AmkrProxy) -> None:
    """把 ``/amkr/*`` 交给代理处理。

    裸前缀 ``/amkr`` 刻意不注册：面板只从 ``/amkr/ui/…`` 进入，而 AMKR 的根路径
    本身是个 405，没有任何值得转发的东西。
    """
    app.router.add_route("*", f"{AMKR_PROXY_PREFIX}/{{tail:.*}}", proxy.handle)
