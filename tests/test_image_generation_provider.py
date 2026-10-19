"""生图客户端：generations / edits 两条端点、b64 与 url 两种响应、错误映射。

所有 HTTP 请求都由 ``httpx.MockTransport`` 拦下，测试只断言真实的
``generate_image_async`` 发到线上的东西（路径、表单字段、媒体类型、请求头）以及
返回的原始字节与媒体类型；不发起任何真实网络请求。
"""

from __future__ import annotations

import asyncio
import base64
import email
import json
from collections.abc import Callable
from email.message import Message
from io import BytesIO

import httpx
import pytest
from PIL import Image

from sirius_pulse.exceptions import ProviderAuthError, ProviderResponseError
from sirius_pulse.providers.base import ImageGenerationRequest, ImageGenerationResult
from sirius_pulse.providers.openai_compatible import OpenAICompatibleProvider

PROMPT = "画一只坐在窗台上的橘猫"

#: 真实可解码的 1×1 PNG。媒体类型要按字节魔数判定，所以用真字节而不是只造魔数前缀。
PNG_1PX = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKm"
    "MIQAAAABJRU5ErkJggg=="
)


def _make_jpeg() -> bytes:
    """用 Pillow 生成一张真实 JPEG（2×2 纯色），避免实现若校验可解码性时假魔数失效。"""
    buffer = BytesIO()
    Image.new("RGB", (2, 2), (220, 60, 30)).save(buffer, format="JPEG")
    return buffer.getvalue()


JPEG_2PX = _make_jpeg()


def _provider(*, workspace: str = "persona-alpha") -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        base_url="http://amkr.test",
        api_key="amkr_ik_test",
        timeout_seconds=5,
        workspace=workspace,
    )


def _request(reference_images: list[tuple[str, bytes]] | None = None) -> ImageGenerationRequest:
    return ImageGenerationRequest(
        model="image_generate",
        prompt=PROMPT,
        reference_images=list(reference_images or []),
        timeout_seconds=5,
    )


def _b64_response(image_bytes: bytes) -> httpx.Response:
    encoded = base64.b64encode(image_bytes).decode("ascii")
    return httpx.Response(200, json={"data": [{"b64_json": encoded}]})


def _generate(
    monkeypatch: pytest.MonkeyPatch,
    provider: OpenAICompatibleProvider,
    handler: Callable[[httpx.Request], httpx.Response],
    request: ImageGenerationRequest | None = None,
) -> ImageGenerationResult:
    """用 MockTransport 驱动真实的 generate_image_async。"""
    import sirius_pulse.providers.openai_compatible as mod

    transport = httpx.MockTransport(handler)

    class _Client(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(mod.httpx, "AsyncClient", _Client)

    async def main() -> ImageGenerationResult:
        return await provider.generate_image_async(request or _request())

    return asyncio.run(main())


def _multipart_parts(request: httpx.Request) -> list[Message]:
    """把 multipart 请求体交给 email 解析器拆成各个表单部件。"""
    content_type = request.headers.get("content-type", "")
    assert content_type.startswith("multipart/form-data; boundary="), content_type
    raw = b"Content-Type: " + content_type.encode("latin-1") + b"\r\nMIME-Version: 1.0\r\n\r\n"
    raw += request.content
    message = email.message_from_bytes(raw)
    parts = message.get_payload()
    assert isinstance(parts, list), "请求体不是 multipart/form-data"
    return parts


def _form_fields(request: httpx.Request) -> dict[str, list[tuple[bytes, str]]]:
    """字段名 -> [(原始字节, 该部件声明的 Content-Type)]，同一字段可重复出现。"""
    fields: dict[str, list[tuple[bytes, str]]] = {}
    for part in _multipart_parts(request):
        name = part.get_param("name", header="content-disposition")
        payload = part.get_payload(decode=True)
        assert isinstance(name, str)
        assert isinstance(payload, bytes)
        fields.setdefault(name, []).append((payload, part.get_content_type()))
    return fields


def test_generations_request_uses_task_name_and_inference_headers(monkeypatch):
    """无参考图走 JSON 的 generations：路径、model 任务名、推理 key 与工作空间头。"""
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return _b64_response(PNG_1PX)

    result = _generate(monkeypatch, _provider(), handler)

    body = seen["body"]
    headers = seen["headers"]
    assert seen["method"] == "POST"
    assert seen["path"] == "/v1/images/generations"
    assert body["model"] == "image_generate"
    assert body["prompt"] == PROMPT
    assert headers["authorization"] == "Bearer amkr_ik_test"
    assert headers["x-amkr-workspace"] == "persona-alpha"
    assert headers["content-type"].startswith("application/json")
    # 上游有 Cloudflare 校验，默认 httpx UA 会被 1010 拦掉；图像请求必须带浏览器 UA。
    assert headers["user-agent"].startswith("Mozilla/5.0")
    assert "Chrome/" in headers["user-agent"]
    assert isinstance(result, ImageGenerationResult)


def test_b64_json_response_is_decoded_and_mime_sniffed(monkeypatch):
    """data[0].b64_json 必须解码回原始字节，媒体类型按字节识别而不是默认值。"""
    result = _generate(monkeypatch, _provider(), lambda request: _b64_response(PNG_1PX))

    assert result.image_bytes == PNG_1PX
    assert result.mime_type == "image/png"


def test_url_response_is_downloaded(monkeypatch):
    """只有 url 时客户端要再 GET 一次下载；空工作空间不应带 X-AMKR-Workspace。"""
    seen: dict[str, object] = {}
    downloads: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            seen["headers"] = dict(request.headers)
            return httpx.Response(
                200,
                json={"data": [{"url": "http://amkr.test/files/result.png"}]},
            )
        downloads.append(request)
        return httpx.Response(200, content=PNG_1PX, headers={"Content-Type": "image/png"})

    result = _generate(monkeypatch, _provider(workspace=""), handler)

    headers = seen["headers"]
    assert "x-amkr-workspace" not in headers
    assert [str(item.url) for item in downloads] == ["http://amkr.test/files/result.png"]
    assert downloads[0].method == "GET"
    # 结果图可能落在同样有 Cloudflare 校验的 CDN 上，下载也要带浏览器 UA。
    assert downloads[0].headers["user-agent"].startswith("Mozilla/5.0")
    assert result.image_bytes == PNG_1PX
    assert result.mime_type == "image/png"


def test_edits_single_reference_uses_image_field_and_byte_mime(monkeypatch):
    """有参考图走 multipart edits；单图字段名 image，媒体类型看字节（后缀说谎也要纠正）。"""
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["headers"] = dict(request.headers)
        seen["fields"] = _form_fields(request)
        return _b64_response(PNG_1PX)

    # 文件名叫 .jpg，内容是 PNG —— 上游按字节校验，声明的类型必须是 image/png。
    _generate(monkeypatch, _provider(), handler, _request([("cached.jpg", PNG_1PX)]))

    fields = seen["fields"]
    headers = seen["headers"]
    assert seen["path"] == "/v1/images/edits"
    assert headers["authorization"] == "Bearer amkr_ik_test"
    assert headers["content-type"].startswith("multipart/form-data; boundary=")
    assert headers["user-agent"].startswith("Mozilla/5.0")
    assert fields["model"][0][0] == b"image_generate"
    assert fields["prompt"][0][0] == PROMPT.encode("utf-8")
    assert fields["image"] == [(PNG_1PX, "image/png")]
    assert "image[]" not in fields


def test_edits_multiple_references_use_image_array_field(monkeypatch):
    """多张参考图要用 image[] 重复字段，且每张各自按字节声明媒体类型。"""
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["fields"] = _form_fields(request)
        return _b64_response(PNG_1PX)

    references = [("cat.png", PNG_1PX), ("photo.png", JPEG_2PX)]
    _generate(monkeypatch, _provider(), handler, _request(references))

    fields = seen["fields"]
    assert "image" not in fields
    assert fields["image[]"] == [(PNG_1PX, "image/png"), (JPEG_2PX, "image/jpeg")]


@pytest.mark.parametrize("status_code", [401, 403])
def test_auth_failures_map_to_provider_auth_error(monkeypatch, status_code):
    """凭据问题（401/403）必须映射成不可重试的 ProviderAuthError。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"error": {"message": "invalid key"}})

    with pytest.raises(ProviderAuthError):
        _generate(monkeypatch, _provider(), handler)


@pytest.mark.parametrize("status_code", [400, 500])
def test_error_statuses_map_to_provider_response_error(monkeypatch, status_code):
    """请求错误与上游故障（400/500）映射成 ProviderResponseError。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, json={"error": {"message": "upstream failed"}})

    with pytest.raises(ProviderResponseError):
        _generate(monkeypatch, _provider(), handler)


def test_empty_data_maps_to_provider_response_error(monkeypatch):
    """HTTP 200 但 data 为空不是成功，必须报 ProviderResponseError。"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"data": []})

    with pytest.raises(ProviderResponseError):
        _generate(monkeypatch, _provider(), handler)
