"""失败处理的边界：不可重试的错误不再重试，provider 保留异常类型。

评估发现两处同源问题：
1. `Brain._call_with_retry()` 无条件重试，忽略异常自带的 `is_retryable`。
   凭据失效（401/403）时重试只是白烧配额并放大故障。
2. 传输层曾把所有失败都包成 `RuntimeError`，于是结构化异常（`ProviderAuthError`
   等）从来没被抛出过，上游无法据此分流。

本文件只留**与传输实现无关**的断言：`Brain` 的重试/兜底策略，以及异常自带的重试
语义（`ProviderResponseError.is_retryable` 的状态码规则）。「HTTP 状态码 → 哪个异常」
的映射属于传输层，已由 `tests/test_go_transport.py` 的真链路用例覆盖。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from sirius_pulse.core.brain import Brain, ChatRequest
from sirius_pulse.exceptions import (
    ProviderAuthError,
    ProviderConnectionError,
    ProviderContentFilteredError,
    ProviderResponseError,
)
from sirius_pulse.providers.base import GenerationResult


def _brain(provider, fallback: str = "fallback_generate", **config) -> Brain:
    return Brain(
        provider_async=provider,
        model_router=SimpleNamespace(
            resolve=lambda *args, **kwargs: SimpleNamespace(
                model_name="model",
                max_tokens=100,
                temperature=0.1,
                timeout=30,
                retries=3,
                fallback_model_name=fallback,
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


# ── 内容策略拦截：换兜底任务，不按原任务重试 ────────────────────────


def test_content_filter_switches_to_fallback_task():
    """Gemini 拦截文案被当 200 正文返回时，改发兜底任务名，把回复救回来。"""

    class _BlockedThenFallbackOk:
        def __init__(self) -> None:
            self.models: list[str] = []

        async def generate_async(self, request):
            self.models.append(request.model)
            if request.model != "fallback_generate":
                raise ProviderContentFilteredError(
                    "amkr",
                    "提供商把内容策略拦截的说明文字当作正常回复返回。",
                    content_preview="The prompt could not be submitted.",
                )
            return GenerationResult(content="兜底模型的回复")

    provider = _BlockedThenFallbackOk()
    outcome: dict[str, object] = {}

    async def main() -> None:
        brain = _brain(provider, task_retries={"response_generate": 3})
        outcome["result"] = await brain.chat(_request())

    asyncio.run(main())

    assert provider.models == ["model", "fallback_generate"], "必须换成兜底任务，而不是原任务重试"
    assert outcome["result"].clean_text == "兜底模型的回复"


def test_when_fallback_is_blocked_too_then_gives_up_without_reply():
    """兜底也被拦就放弃本轮：宁可沉默，也不能把报错文案当成回复发出去。"""

    class _AlwaysBlocked:
        def __init__(self) -> None:
            self.models: list[str] = []

        async def generate_async(self, request):
            self.models.append(request.model)
            raise ProviderContentFilteredError(
                "amkr",
                "提供商把内容策略拦截的说明文字当作正常回复返回。",
                content_preview="The prompt could not be submitted.",
            )

    provider = _AlwaysBlocked()

    async def main() -> None:
        brain = _brain(provider, task_retries={"response_generate": 3})
        with pytest.raises(ProviderContentFilteredError):
            await brain.chat(_request())

    asyncio.run(main())

    assert provider.models == ["model", "fallback_generate"]


def test_content_filter_without_fallback_is_not_retried():
    """没配兜底任务时，拦截文案第一次就放弃——重试同一提示词只会再被拦。"""

    class _Blocked:
        def __init__(self) -> None:
            self.calls = 0

        async def generate_async(self, request):
            self.calls += 1
            raise ProviderContentFilteredError(
                "amkr",
                "提供商把内容策略拦截的说明文字当作正常回复返回。",
                content_preview="The prompt could not be submitted.",
            )

    provider = _Blocked()

    async def main() -> None:
        brain = _brain(provider, fallback="", task_retries={"response_generate": 3})
        with pytest.raises(ProviderContentFilteredError):
            await brain.chat(_request())

    asyncio.run(main())

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


# ── 异常自带的重试语义（与传输实现无关）────────────────────────────


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
