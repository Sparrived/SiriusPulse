"""生图客户端：generations / edits 两条端点、b64 与 url 两种响应、错误映射。

所有 HTTP 请求都由 ``httpx.MockTransport`` 拦下，测试只断言真实的
``generate_image_async`` 发到线上的东西（路径、JSON 字段、媒体类型、请求头）以及
返回的原始字节与媒体类型；不发起任何真实网络请求。
"""

from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import Callable
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


def test_edits_single_reference_is_sent_as_json_image_data_url(monkeypatch):
    """有参考图走 JSON 的 edits：参考图放 images[].image_url，媒体类型看字节。"""
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["headers"] = dict(request.headers)
        seen["body"] = json.loads(request.content)
        return _b64_response(PNG_1PX)

    # 文件名叫 .jpg，内容是 PNG —— 上游按字节校验，data URL 必须声明 image/png。
    _generate(monkeypatch, _provider(), handler, _request([("cached.jpg", PNG_1PX)]))

    body = seen["body"]
    headers = seen["headers"]
    assert seen["path"] == "/v1/images/edits"
    assert headers["authorization"] == "Bearer amkr_ik_test"
    assert headers["content-type"].startswith("application/json")
    assert headers["user-agent"].startswith("Mozilla/5.0")
    assert body["model"] == "image_generate"
    assert body["prompt"] == PROMPT
    images = body["images"]
    assert len(images) == 1
    data_url = images[0]["image_url"]
    assert data_url.startswith("data:image/png;base64,")
    assert base64.b64decode(data_url.split(",", 1)[1]) == PNG_1PX


def test_edits_multiple_references_are_all_sent_with_byte_sniffed_mime(monkeypatch):
    """多张参考图都放进 images[]，且每张各自按字节声明媒体类型。"""
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return _b64_response(PNG_1PX)

    references = [("cat.png", PNG_1PX), ("photo.jpg", JPEG_2PX)]
    _generate(monkeypatch, _provider(), handler, _request(references))

    images = seen["body"]["images"]
    assert [item["image_url"].split(";", 1)[0] for item in images] == [
        "data:image/png",
        "data:image/jpeg",
    ]
    assert [base64.b64decode(item["image_url"].split(",", 1)[1]) for item in images] == [
        PNG_1PX,
        JPEG_2PX,
    ]


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
