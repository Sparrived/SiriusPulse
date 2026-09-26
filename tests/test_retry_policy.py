"""失败处理的边界：不可重试的错误不再重试，provider 保留异常类型。

评估发现两处同源问题：
1. `Brain._call_with_retry()` 无条件重试，忽略异常自带的 `is_retryable`。
   凭据失效（401/403）时重试只是白烧配额并放大故障。
2. `openai_compatible.py` 把所有失败都包成 `RuntimeError`，于是结构化异常
   （`ProviderAuthError` 等）从来没被抛出过，上游无法据此分流。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest

from sirius_pulse.core.brain import Brain, ChatRequest
from sirius_pulse.exceptions import (
    ProviderAuthError,
    ProviderConnectionError,
    ProviderResponseError,
)
from sirius_pulse.providers.base import GenerationResult
from sirius_pulse.providers.openai_compatible import OpenAICompatibleProvider


def _brain(provider, **config) -> Brain:
    return Brain(
        provider_async=provider,
        model_router=SimpleNamespace(
            resolve=lambda *args, **kwargs: SimpleNamespace(
                model_name="model",
                max_tokens=100,
                temperature=0.1,
                timeout=30,
                retries=3,
            )
        ),
        persona=SimpleNamespace(name="tester", build_system_prompt=lambda: ""),
        config=config,
    )


def _request() -> ChatRequest:
    return ChatRequest(
        group_id="group-1",
        user_id="u1",
        system_prompt="system",
        messages=[{"role": "user", "content": "hello"}],
    )


# ── 重试策略 ────────────────────────────────────────────────────────


def test_auth_error_is_not_retried():
    """凭据失效重试多少次都一样，必须第一次就放弃。"""

    class _AuthFailing:
        def __init__(self) -> None:
            self.calls = 0

        async def generate_async(self, request):
            self.calls += 1
            raise ProviderAuthError("amkr", "API Key 无效", http_status=401)

    provider = _AuthFailing()

    async def main() -> None:
        brain = _brain(provider, task_retries={"response_generate": 3})
        with pytest.raises(ProviderAuthError):
            await brain.chat(_request())

    asyncio.run(main())

    assert provider.calls == 1, "认证错误不得重试"


def test_transient_connection_error_is_still_retried():
    """连接类错误仍按原样重试，别把可恢复故障一起关掉。"""

    class _Flaky:
        def __init__(self) -> None:
            self.calls = 0

        async def generate_async(self, request):
            self.calls += 1
            if self.calls < 2:
                raise ProviderConnectionError("amkr", "connection reset")
            return GenerationResult(content="ok")

    provider = _Flaky()

    async def main() -> None:
        brain = _brain(provider, task_retries={"response_generate": 3})
        await brain.chat(_request())

    asyncio.run(main())

    assert provider.calls == 2


def test_empty_response_is_not_retried():
    """空响应归类为 empty_response，再试通常还是空。"""

    class _Empty:
        def __init__(self) -> None:
            self.calls = 0

        async def generate_async(self, request):
            self.calls += 1
            raise ProviderResponseError("amkr", "提供商响应内容为空。", http_status=200)

    provider = _Empty()

    async def main() -> None:
        brain = _brain(provider, task_retries={"response_generate": 3})
        with pytest.raises(ProviderResponseError):
            await brain.chat(_request())

    asyncio.run(main())

    # 200 状态不被 http_status 判为不可重试，但 classify 归类为 empty_response。
    assert provider.calls == 1


def test_plain_exception_still_retries():
    """没有结构化信息的异常保持旧的乐观重试行为。"""

    class _Plain:
        def __init__(self) -> None:
            self.calls = 0

        async def generate_async(self, request):
            self.calls += 1
            if self.calls < 2:
                raise RuntimeError("莫名其妙的瞬时故障")
            return GenerationResult(content="ok")

    provider = _Plain()

    async def main() -> None:
        brain = _brain(provider, task_retries={"response_generate": 3})
        await brain.chat(_request())

    asyncio.run(main())

    assert provider.calls == 2


# ── provider 保留异常类型 ───────────────────────────────────────────


def _provider() -> OpenAICompatibleProvider:
    return OpenAICompatibleProvider(
        base_url="http://amkr.invalid/v1",
        api_key="amkr_ik_test",
        timeout_seconds=5,
    )


def _request_with(provider: OpenAICompatibleProvider):
    from sirius_pulse.providers.base import GenerationRequest

    return GenerationRequest(
        model="response_generate",
        system_prompt="system",
        messages=[{"role": "user", "content": "hi"}],
        timeout_seconds=5,
    )


def _install_transport(provider, handler) -> None:
    transport = httpx.MockTransport(handler)
    provider._test_transport = transport  # type: ignore[attr-defined]


def _patch_client(monkeypatch, provider) -> None:
    """把 provider 里的 httpx.AsyncClient 换成带 MockTransport 的版本。"""
    import sirius_pulse.providers.openai_compatible as mod

    transport = getattr(provider, "_test_transport")

    class _Client(httpx.AsyncClient):
        def __init__(self, *args, **kwargs):
            kwargs["transport"] = transport
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(mod.httpx, "AsyncClient", _Client)


def test_http_401_surfaces_as_provider_auth_error(monkeypatch):
    provider = _provider()
    _install_transport(provider, lambda request: httpx.Response(401, text="invalid key"))
    _patch_client(monkeypatch, provider)

    async def main():
        return await provider.generate_async(_request_with(provider))

    with pytest.raises(ProviderAuthError) as info:
        asyncio.run(main())

    assert info.value.is_retryable is False
    assert info.value.http_status == 401


def test_http_503_surfaces_as_retryable_provider_response_error(monkeypatch):
    provider = _provider()
    _install_transport(provider, lambda request: httpx.Response(503, text="overloaded"))
    _patch_client(monkeypatch, provider)

    async def main():
        return await provider.generate_async(_request_with(provider))

    with pytest.raises(ProviderResponseError) as info:
        asyncio.run(main())

    assert info.value.is_retryable is True
    assert info.value.http_status == 503


@pytest.mark.parametrize("status", [500, 502, 504])
def test_server_side_failures_stay_retryable(status):
    """5xx 是上游瞬时故障，必须继续重试——否则网关抖动会直接变成回复失败。"""
    exc = ProviderResponseError("amkr", "上游错误", http_status=status)

    assert exc.is_retryable is True


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_client_side_failures_do_not_retry(status):
    exc = ProviderResponseError("amkr", "请求有问题", http_status=status)

    assert exc.is_retryable is False


def test_response_error_without_status_stays_retryable():
    """没有状态码的是本地格式判定，保持旧的乐观行为。"""
    assert ProviderResponseError("amkr", "格式错误").is_retryable is True


def test_connection_failure_surfaces_as_provider_connection_error(monkeypatch):
    provider = _provider()

    def boom(request):
        raise httpx.ConnectError("connection refused")

    _install_transport(provider, boom)
    _patch_client(monkeypatch, provider)

    async def main():
        return await provider.generate_async(_request_with(provider))

    with pytest.raises(ProviderConnectionError) as info:
        asyncio.run(main())

    assert info.value.is_retryable is True


def test_non_json_success_body_surfaces_as_provider_response_error(monkeypatch):
    """反代返回 HTML 错误页时，报错要说清是「非 JSON」而不是 json 解析崩溃。"""
    provider = _provider()
    _install_transport(
        provider,
        lambda request: httpx.Response(200, text="<html>502 Bad Gateway</html>"),
    )
    _patch_client(monkeypatch, provider)

    async def main():
        return await provider.generate_async(_request_with(provider))

    with pytest.raises(ProviderResponseError, match="非 JSON"):
        asyncio.run(main())


def test_empty_choices_surfaces_as_provider_response_error(monkeypatch):
    provider = _provider()
    _install_transport(provider, lambda request: httpx.Response(200, json={"choices": []}))
    _patch_client(monkeypatch, provider)

    async def main():
        return await provider.generate_async(_request_with(provider))

    with pytest.raises(ProviderResponseError, match="choices"):
        asyncio.run(main())
