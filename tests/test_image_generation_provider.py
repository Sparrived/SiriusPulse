"""生图桥接：Python 把契约 §5.1.1 的报文交给侧车，并把响应还原成结果与异常。

被测对象是**桥接层**，所以这里用一个线程内的替身侧车（不是真 Go 二进制）：它记录
``POST /v1/generate-image`` 的原始报文，并按用例脚本回包。上游 AMKR 那一侧的形态
（generations / edits / multipart 回退 / url 下载 / 浏览器 UA）是侧车职责，由
``tests/test_go_transport.py`` 的真链路用例覆盖。

这样切分的理由很实在：报文形态、超时下限、侧车错误码映射只有侧车看得到，而上游
响应形态只有假 AMKR 看得到。两边都断言，才不会留下「桥接和侧车都以为对方在做」
的空洞；反过来，把桥接断言塞进真链路用例就只能看到副作用，看不到报文本身。

**为什么这些用例永不跳过**：它们不需要 Go 二进制，因此在任何环境里都跑，包括
还没跑过 ``scripts/build_llm_transport.py`` 的机器。
"""

from __future__ import annotations

import asyncio
import base64
import json
import socket
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Iterator

import pytest

from sirius_pulse.exceptions import (
    ProviderAuthError,
    ProviderConnectionError,
    ProviderResponseError,
)
from sirius_pulse.providers.base import ImageGenerationRequest, ImageGenerationResult
from sirius_pulse.providers.go_transport import GoTransportProvider

PROMPT = "画一只坐在窗台上的橘猫"
SIDECAR_TOKEN = "sidecar-secret"
AMKR_BASE_URL = "http://amkr.test"
API_KEY = "amkr_ik_test"
WORKSPACE = "persona-alpha"

#: 真 PNG 字节（1×1 透明）：断言「字节原样往返」，不能用假前缀。
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8DwHwAFAAH/q842iQAAAABJRU5ErkJggg=="
)

#: 真 JPEG 字节（1×1 白色）：多张参考图里用来验证每张各自编码。
JPEG_1PX = base64.b64decode(
    "/9j/4AAQSkZJRgABAQEAYABgAAD/2wBDAAgGBgcGBQgHBwcJCQgKDBQNDAsLDBkSEw8UHRofHh0a"
    "HBwgJC4nICIsIxwcKDcpLDAxNDQ0Hyc5PTgyPC4zNDL/wAALCAABAAEBAREA/8QAFAABAAAAAAAA"
    "AAAAAAAAAAAACf/EABQQAQAAAAAAAAAAAAAAAAAAAAD/2gAIAQEAAD8AKp//2Q=="
)


# ── 替身侧车 ──────────────────────────────────────────────
@dataclass(slots=True)
class _Recorded:
    """替身侧车收到的一次请求。"""

    path: str
    headers: dict[str, str]
    body: bytes

    @property
    def json_body(self) -> dict[str, Any]:
        parsed = json.loads(self.body)
        assert isinstance(parsed, dict)
        return parsed


#: 一个脚本化响应：``(状态码, 响应体, Content-Type)``。
_Response = tuple[int, bytes, str]


def _json(status: int, payload: object) -> _Response:
    """把 JSON 载荷打包成脚本化响应（默认 200，调用方按需覆盖）。"""
    return (status, json.dumps(payload, ensure_ascii=False).encode("utf-8"), "application/json")


def _image_ok(image_bytes: bytes = PNG_1PX, *, mime_type: str | None = "image/png") -> _Response:
    """侧车生图成功响应（契约 §5.1.3）。"""
    payload: dict[str, Any] = {"image_base64": base64.b64encode(image_bytes).decode("ascii")}
    if mime_type is not None:
        payload["mime_type"] = mime_type
    payload["transport"] = {"endpoint": "generations", "downloaded": False, "upstream_ms": 12.5}
    return _json(200, payload)


def _sidecar_error(
    status: int, error_code: str, message: str, *, is_retryable: bool, upstream_status: int | None
) -> _Response:
    """侧车错误响应（契约 §6）。"""
    return _json(
        status,
        {
            "error": {
                "error_code": error_code,
                "message": message,
                "http_status": status,
                "upstream_status": upstream_status,
                "is_retryable": is_retryable,
            }
        },
    )


class _StubSidecar:
    """线程内的替身 LLM 传输层：记录请求，按 ``responder`` 回放响应。"""

    def __init__(self, responder: Callable[[_Recorded], _Response]) -> None:
        self._responder = responder
        self._records: list[_Recorded] = []
        self._lock = threading.Lock()
        self._server = _StubServer(("127.0.0.1", 0), _StubHandler)
        self._server.stub = self  # type: ignore[attr-defined]
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            kwargs={"poll_interval": 0.02},
            name="stub-llm-sidecar",
            daemon=True,
        )

    # ── 生命周期 ──────────────────────────────────────
    def start(self) -> "_StubSidecar":
        self._thread.start()
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    # ── 地址与记录 ────────────────────────────────────
    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{int(self._server.server_address[1])}"

    @property
    def token(self) -> str:
        return SIDECAR_TOKEN

    @property
    def records(self) -> list[_Recorded]:
        with self._lock:
            return list(self._records)

    # ── 内部 ──────────────────────────────────────────
    def record(self, item: _Recorded) -> None:
        with self._lock:
            self._records.append(item)

    def respond(self, item: _Recorded) -> _Response:
        return self._responder(item)


class _StubHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "StubSidecar/1.0"

    def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler 的命名约定
        stub: _StubSidecar = self.server.stub  # type: ignore[attr-defined]
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        recorded = _Recorded(
            path=self.path,
            headers={key.lower(): value for key, value in self.headers.items()},
            body=body,
        )
        stub.record(recorded)
        status, payload, content_type = stub.respond(recorded)
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        try:
            self.wfile.write(payload)
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        """静默：替身侧车的访问日志只会污染 pytest 输出。"""


class _StubServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request: object, client_address: object) -> None:
        # 客户端取消请求（关闭连接）不是测试失败，别把 traceback 打到 stderr。
        return


# ── 夹具与辅助 ────────────────────────────────────────────
@pytest.fixture
def sidecar_factory() -> Iterator[Callable[..., _StubSidecar]]:
    """按脚本起替身侧车，用例结束一张不停（失败的用例也能收干净）。"""
    started: list[_StubSidecar] = []

    def _make(responder: Callable[[_Recorded], _Response]) -> _StubSidecar:
        stub = _StubSidecar(responder).start()
        started.append(stub)
        return stub

    yield _make
    for stub in started:
        stub.stop()


def _provider(
    sidecar: _StubSidecar,
    *,
    timeout_seconds: int = 5,
    workspace: str = WORKSPACE,
) -> GoTransportProvider:
    return GoTransportProvider(
        sidecar_url=sidecar.base_url,
        sidecar_token=sidecar.token,
        base_url=AMKR_BASE_URL,
        api_key=API_KEY,
        timeout_seconds=timeout_seconds,
        workspace=workspace,
    )


def _request(
    reference_images: list[tuple[str, bytes]] | None = None,
    *,
    timeout_seconds: float | None = None,
) -> ImageGenerationRequest:
    return ImageGenerationRequest(
        model="image_generate",
        prompt=PROMPT,
        reference_images=list(reference_images or []),
        timeout_seconds=timeout_seconds,
    )


def _generate(
    provider: GoTransportProvider, request: ImageGenerationRequest
) -> ImageGenerationResult:
    """跑一次生图并关闭客户端，避免 httpx 客户端跨事件循环泄漏。"""

    async def main() -> ImageGenerationResult:
        try:
            return await provider.generate_image_async(request)
        finally:
            await provider.aclose()

    return asyncio.run(main())


def _closed_port() -> int:
    """拿一个刚释放的端口，用来构造「侧车不可达」。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


# ── 发往侧车的报文（契约 §5.1.1）──────────────────────────
def test_generate_image_posts_the_contract_payload(sidecar_factory) -> None:
    """路径、鉴权头与报文结构必须与契约 §5.1.1 逐字段一致。"""
    sidecar = sidecar_factory(lambda record: _image_ok())
    result = _generate(_provider(sidecar), _request())

    assert isinstance(result, ImageGenerationResult)
    assert result.image_bytes == PNG_1PX, "侧车给的 base64 必须解回原始字节"
    assert result.mime_type == "image/png"

    record = sidecar.records[0]
    assert record.path == "/v1/generate-image"
    assert record.headers["authorization"] == f"Bearer {SIDECAR_TOKEN}"
    assert record.json_body == {
        "upstream": {
            "base_url": AMKR_BASE_URL,
            "api_key": API_KEY,
            "workspace": WORKSPACE,
            "timeout_seconds": 120,
        },
        "request": {"model": "image_generate", "prompt": PROMPT, "reference_images": []},
    }


def test_reference_images_are_sent_as_raw_base64(sidecar_factory) -> None:
    """参考图只传字节：表情库/用户上传的图不保证有可读本地路径（§5.1.1）。"""
    sidecar = sidecar_factory(lambda record: _image_ok())
    _generate(
        _provider(sidecar),
        _request([("cat.png", PNG_1PX), ("photo.jpg", JPEG_1PX)]),
    )

    references = sidecar.records[0].json_body["request"]["reference_images"]
    assert references == [
        {"filename": "cat.png", "data_base64": base64.b64encode(PNG_1PX).decode("ascii")},
        {"filename": "photo.jpg", "data_base64": base64.b64encode(JPEG_1PX).decode("ascii")},
    ]


def test_image_timeout_has_a_120_second_floor(sidecar_factory) -> None:
    """生图慢是常态：provider 配 30 秒也得按 120 秒发（迁移前的硬下限）。"""
    sidecar = sidecar_factory(lambda record: _image_ok())
    _generate(_provider(sidecar, timeout_seconds=30), _request())

    assert sidecar.records[0].json_body["upstream"]["timeout_seconds"] == 120


def test_image_timeout_keeps_a_larger_provider_budget(sidecar_factory) -> None:
    """provider 配得比下限更长时按配置走，不被 120 秒压回去。"""
    sidecar = sidecar_factory(lambda record: _image_ok())
    _generate(_provider(sidecar, timeout_seconds=300), _request())

    assert sidecar.records[0].json_body["upstream"]["timeout_seconds"] == 300


def test_fractional_timeout_is_not_truncated(sidecar_factory) -> None:
    """非整秒如实发小数：``30.5`` 悄悄变成 ``30`` 会让调用方少等半秒又说不清。"""
    sidecar = sidecar_factory(lambda record: _image_ok())
    _generate(_provider(sidecar, timeout_seconds=30), _request(timeout_seconds=90.5))

    assert sidecar.records[0].json_body["upstream"]["timeout_seconds"] == 90.5


# ── 侧车响应的还原 ────────────────────────────────────────
def test_missing_mime_type_falls_back_to_png(sidecar_factory) -> None:
    """侧车没给 ``mime_type`` 时按契约兜底 ``image/png``，不要留空串。"""
    sidecar = sidecar_factory(lambda record: _image_ok(mime_type=None))
    result = _generate(_provider(sidecar), _request())

    assert result.mime_type == "image/png"


def test_non_json_sidecar_response_is_a_response_error(sidecar_factory) -> None:
    """侧车回 200 但正文不是 JSON 时，要报「无法解析」而不是 json 解码崩溃。"""
    sidecar = sidecar_factory(
        lambda record: (200, b"<html>gateway</html>", "text/html"),
    )
    with pytest.raises(ProviderResponseError, match="无法解析为 JSON"):
        _generate(_provider(sidecar), _request())


def test_missing_image_base64_is_a_response_error(sidecar_factory) -> None:
    """少了 ``image_base64`` 不是「空图」，是响应结构不对。"""
    sidecar = sidecar_factory(lambda record: _json(200, {"mime_type": "image/png"}))
    with pytest.raises(ProviderResponseError) as excinfo:
        _generate(_provider(sidecar), _request())

    assert str(excinfo.value) == "生图接口没有返回图片数据。"


def test_invalid_base64_is_a_response_error(sidecar_factory) -> None:
    """严格解码：掺了非法字符说明响应已被改坏，不能丢字符硬解出一张坏图。"""
    sidecar = sidecar_factory(
        lambda record: _json(200, {"image_base64": "@@这不是 base64@@", "mime_type": "image/png"})
    )
    with pytest.raises(ProviderResponseError, match="无法解码"):
        _generate(_provider(sidecar), _request())


# ── 侧车错误码 → 本仓库既有异常（契约 §6）────────────────
def test_sidecar_auth_error_maps_to_non_retryable_auth_error(sidecar_factory) -> None:
    """502 ``PROVIDER_AUTH_ERROR``（上游 401）→ 不可重试的 ``ProviderAuthError``。"""
    sidecar = sidecar_factory(
        lambda record: _sidecar_error(
            502,
            "PROVIDER_AUTH_ERROR",
            "生图接口 HTTP 错误 401：invalid key",
            is_retryable=False,
            upstream_status=401,
        )
    )
    with pytest.raises(ProviderAuthError) as excinfo:
        _generate(_provider(sidecar), _request())

    assert excinfo.value.is_retryable is False
    assert getattr(excinfo.value, "http_status", None) == 401, "要报上游状态码，不是侧车的 502"


def test_sidecar_timeout_maps_to_retryable_connection_error(sidecar_factory) -> None:
    """504 ``UPSTREAM_TIMEOUT`` → 可重试的 ``ProviderConnectionError``。

    超时不单独建异常类：改造前生图超时也是一次 httpx 超时，``Brain`` 的重试与兜底
    策略不该因为换传输层多出一个分支。
    """
    sidecar = sidecar_factory(
        lambda record: _sidecar_error(
            504,
            "UPSTREAM_TIMEOUT",
            "生图请求超时",
            is_retryable=True,
            upstream_status=None,
        )
    )
    with pytest.raises(ProviderConnectionError) as excinfo:
        _generate(_provider(sidecar), _request())

    assert excinfo.value.is_retryable is True


def test_unreachable_sidecar_is_a_connection_error() -> None:
    """侧车端口关着 → ``ProviderConnectionError``（可重试），不抛 httpx 原生异常。"""
    provider = GoTransportProvider(
        sidecar_url=f"http://127.0.0.1:{_closed_port()}",
        sidecar_token=SIDECAR_TOKEN,
        base_url=AMKR_BASE_URL,
        api_key=API_KEY,
        timeout_seconds=5,
        workspace=WORKSPACE,
    )
    with pytest.raises(ProviderConnectionError) as excinfo:
        _generate(provider, _request())

    assert excinfo.value.is_retryable is True
    assert "生图请求异常" in str(excinfo.value)
