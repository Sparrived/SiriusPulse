from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from sirius_pulse.core.brain import Brain, ChatRequest
from sirius_pulse.providers.base import GenerationResult
from sirius_pulse.tools import ToolDefinition, ToolRegistry


def _tool(name: str) -> ToolDefinition:
    def run(**kwargs):
        return {"success": True, "text": name}

    return ToolDefinition(
        name=name,
        description=f"{name} tool",
        parameters=[],
        source_path=None,
        _run_func=run,
    )


class _Provider:
    def __init__(self) -> None:
        self.last_request = None

    async def generate_async(self, request):
        self.last_request = request
        return GenerationResult(content="ok")


class _RetryingProvider:
    def __init__(self) -> None:
        self.requests = []

    async def generate_async(self, request):
        self.requests.append(deepcopy(request))
        if len(self.requests) == 1:
            raise RuntimeError("temporary failure")
        return GenerationResult(content="ok")


@pytest.mark.asyncio
async def test_brain_chat_when_tools_are_enabled_then_all_available_schemas_are_sent():
    provider = _Provider()
    registry = ToolRegistry()
    registry.register(_tool("social_tool"))
    registry.register(_tool("lookup"))
    brain = Brain(
        provider_async=provider,
        model_router=SimpleNamespace(
            resolve=lambda *args, **kwargs: SimpleNamespace(
                model_name="model",
                max_tokens=100,
                temperature=0.1,
                timeout=30,
            )
        ),
        persona=SimpleNamespace(name="tester", build_system_prompt=lambda: ""),
        tool_registry=registry,
    )

    await brain.chat(
        ChatRequest(
            group_id="group-1",
            user_id="u1",
            system_prompt="system",
            messages=[{"role": "user", "content": "hello"}],
        )
    )

    assert provider.last_request is not None
    tool_names = [tool["function"]["name"] for tool in (provider.last_request.tools or [])]
    assert tool_names == ["social_tool", "lookup"]


@pytest.mark.asyncio
async def test_brain_chat_result_records_injected_tool_names():
    provider = _Provider()
    registry = ToolRegistry()
    registry.register(_tool("lookup"))
    brain = Brain(
        provider_async=provider,
        model_router=SimpleNamespace(
            resolve=lambda *args, **kwargs: SimpleNamespace(
                model_name="model",
                max_tokens=100,
                temperature=0.1,
                timeout=30,
            )
        ),
        persona=SimpleNamespace(name="tester", build_system_prompt=lambda: ""),
        tool_registry=registry,
    )

    result = await brain.chat(
        ChatRequest(
            group_id="group-1",
            user_id="u1",
            system_prompt="system",
            messages=[{"role": "user", "content": "hello"}],
            extra_tools=[
                {
                    "type": "function",
                    "function": {"name": "extra_tool", "description": "extra", "parameters": {}},
                }
            ],
        )
    )

    assert result.injected_tool_names == ["lookup", "extra_tool"]
    assert provider.last_request is not None
    assert result.injected_request == {
        "model": provider.last_request.model,
        "system_prompt": provider.last_request.system_prompt,
        "messages": deepcopy(provider.last_request.messages),
        "tools": deepcopy(provider.last_request.tools),
        "tool_choice": provider.last_request.tool_choice,
        "timeout_seconds": provider.last_request.timeout_seconds,
        "purpose": provider.last_request.purpose,
        "response_format": provider.last_request.response_format,
        "reasoning_effort": provider.last_request.reasoning_effort,
    }
    provider.last_request.messages[0]["content"] = "mutated"
    assert result.injected_request["messages"][0]["content"] != "mutated"


@pytest.mark.asyncio
async def test_brain_chat_after_retry_records_the_final_provider_request():
    provider = _RetryingProvider()
    brain = Brain(
        provider_async=provider,
        model_router=SimpleNamespace(
            resolve=lambda *args, **kwargs: SimpleNamespace(
                model_name="model", max_tokens=100, temperature=0.1, timeout=30
            )
        ),
        persona=SimpleNamespace(name="tester", build_system_prompt=lambda: "persona"),
    )
    contexts = iter(["first context", "final context"])
    brain._build_current_time_context = lambda: next(contexts)

    result = await brain.chat(
        ChatRequest(
            group_id="group-1",
            user_id="u1",
            system_prompt="system",
            messages=[{"role": "user", "content": "hello"}],
            retry_max=1,
            retry_delay=0,
        )
    )

    assert len(provider.requests) == 2
    assert "first context" in provider.requests[0].messages[0]["content"]
    assert "final context" in provider.requests[1].messages[0]["content"]
    assert result.injected_request["messages"] == provider.requests[1].messages
    assert result.injected_request["system_prompt"] == provider.requests[1].system_prompt


@pytest.mark.asyncio
async def test_brain_chat_when_tools_are_enabled_then_unrelated_schemas_are_sent_too():
    provider = _Provider()
    registry = ToolRegistry()
    registry.register(_tool("weather_lookup"))
    registry.register(_tool("calendar_lookup"))
    brain = Brain(
        provider_async=provider,
        model_router=SimpleNamespace(
            resolve=lambda *args, **kwargs: SimpleNamespace(
                model_name="model", max_tokens=100, temperature=0.1, timeout=30
            )
        ),
        persona=SimpleNamespace(name="tester", build_system_prompt=lambda: ""),
        tool_registry=registry,
    )

    await brain.chat(
        ChatRequest(
            group_id="group-1",
            user_id="u1",
            system_prompt="system",
            messages=[{"role": "user", "content": "weather"}],
        )
    )

    assert provider.last_request is not None
    assert [tool["function"]["name"] for tool in (provider.last_request.tools or [])] == [
        "weather_lookup",
        "calendar_lookup",
    ]


@pytest.mark.asyncio
async def test_brain_chat_when_tool_choice_is_none_then_all_schemas_are_still_sent():
    provider = _Provider()
    registry = ToolRegistry()
    registry.register(_tool("lookup"))
    brain = Brain(
        provider_async=provider,
        model_router=SimpleNamespace(
            resolve=lambda *args, **kwargs: SimpleNamespace(
                model_name="model", max_tokens=100, temperature=0.1, timeout=30
            )
        ),
        persona=SimpleNamespace(name="tester", build_system_prompt=lambda: ""),
        tool_registry=registry,
    )

    await brain.chat(
        ChatRequest(
            group_id="group-1",
            user_id="u1",
            system_prompt="system",
            messages=[{"role": "user", "content": "write text only"}],
            tool_choice="none",
        )
    )

    assert provider.last_request.tool_choice == "none"
    assert [tool["function"]["name"] for tool in (provider.last_request.tools or [])] == ["lookup"]


@pytest.mark.asyncio
async def test_brain_chat_when_not_in_work_mode_then_heavy_tools_are_hidden():
    provider = _Provider()
    registry = ToolRegistry()
    registry.register(_tool("bash"))
    registry.register(_tool("read_skill"))
    registry.register(_tool("lookup"))
    brain = Brain(
        provider_async=provider,
        model_router=SimpleNamespace(
            resolve=lambda *args, **kwargs: SimpleNamespace(
                model_name="model", max_tokens=100, temperature=0.1, timeout=30
            )
        ),
        persona=SimpleNamespace(name="tester", build_system_prompt=lambda: ""),
        tool_registry=registry,
    )

    await brain.chat(
        ChatRequest(
            group_id="group-1",
            user_id="u1",
            system_prompt="system",
            messages=[{"role": "user", "content": "hello"}],
        )
    )

    assert [tool["function"]["name"] for tool in (provider.last_request.tools or [])] == ["lookup"]


@pytest.mark.asyncio
async def test_brain_chat_when_in_work_mode_then_heavy_tools_are_offered():
    provider = _Provider()
    registry = ToolRegistry()
    registry.register(_tool("bash"))
    registry.register(_tool("lookup"))
    brain = Brain(
        provider_async=provider,
        model_router=SimpleNamespace(
            resolve=lambda *args, **kwargs: SimpleNamespace(
                model_name="model", max_tokens=100, temperature=0.1, timeout=30
            )
        ),
        persona=SimpleNamespace(name="tester", build_system_prompt=lambda: ""),
        tool_registry=registry,
    )

    await brain.chat(
        ChatRequest(
            group_id="group-1",
            user_id="u1",
            system_prompt="system",
            messages=[{"role": "user", "content": "hello"}],
            work_mode=True,
        )
    )

    assert [tool["function"]["name"] for tool in (provider.last_request.tools or [])] == [
        "bash",
        "lookup",
    ]


@pytest.mark.asyncio
async def test_brain_chat_injects_current_time_into_user_message_not_system_prompt():
    provider = _Provider()
    brain = Brain(
        provider_async=provider,
        model_router=SimpleNamespace(
            resolve=lambda *args, **kwargs: SimpleNamespace(
                model_name="model",
                max_tokens=100,
                temperature=0.1,
                timeout=30,
            )
        ),
        persona=SimpleNamespace(name="tester", build_system_prompt=lambda: ""),
    )

    await brain.chat(
        ChatRequest(
            group_id="group-1",
            user_id="u1",
            system_prompt="system",
            messages=[{"role": "user", "content": "hello"}],
        )
    )

    assert provider.last_request is not None
    assert "【当前时间】" not in provider.last_request.system_prompt
    assert provider.last_request.messages[0]["role"] == "user"
    assert "【当前时间】" in provider.last_request.messages[0]["content"]
    assert "请记住你tester的身份" not in provider.last_request.messages[0]["content"]
    assert "hello" in provider.last_request.messages[0]["content"]


@pytest.mark.asyncio
async def test_brain_chat_injects_current_time_into_latest_user_message():
    provider = _Provider()
    brain = Brain(
        provider_async=provider,
        model_router=SimpleNamespace(
            resolve=lambda *args, **kwargs: SimpleNamespace(
                model_name="model",
                max_tokens=100,
                temperature=0.1,
                timeout=30,
            )
        ),
        persona=SimpleNamespace(name="tester", build_system_prompt=lambda: ""),
    )

    await brain.chat(
        ChatRequest(
            group_id="group-1",
            user_id="u1",
            system_prompt="system",
            messages=[
                {"role": "user", "content": "older user"},
                {"role": "assistant", "content": "older assistant"},
                {"role": "user", "content": "latest user"},
            ],
        )
    )

    assert provider.last_request is not None
    current_time_tag = "\u3010\u5f53\u524d\u65f6\u95f4\u3011"
    assert current_time_tag not in provider.last_request.system_prompt
    assert current_time_tag not in provider.last_request.messages[0]["content"]
    assert current_time_tag in provider.last_request.messages[2]["content"]
    assert "请记住你tester的身份" not in provider.last_request.messages[0]["content"]
    assert "请记住你tester的身份" not in provider.last_request.messages[2]["content"]
    assert "latest user" in provider.last_request.messages[2]["content"]


@pytest.mark.asyncio
async def test_brain_chat_waits_for_configured_main_reply_cooldown(monkeypatch):
    provider = _Provider()
    now = {"value": 100.0}
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        now["value"] += seconds

    monkeypatch.setattr("sirius_pulse.core.brain.time.monotonic", lambda: now["value"])
    monkeypatch.setattr("sirius_pulse.core.brain.asyncio.sleep", fake_sleep)

    brain = Brain(
        provider_async=provider,
        model_router=SimpleNamespace(
            resolve=lambda *args, **kwargs: SimpleNamespace(
                model_name="model",
                max_tokens=100,
                temperature=0.1,
                timeout=30,
            )
        ),
        persona=SimpleNamespace(name="tester", build_system_prompt=lambda: ""),
        config={"main_model_reply_cooldown_seconds": 5},
    )

    request = ChatRequest(
        group_id="group-1",
        user_id="u1",
        system_prompt="system",
        messages=[{"role": "user", "content": "hello"}],
    )
    await brain.chat(request)
    now["value"] += 2
    await brain.chat(request)

    assert sleeps == [3.0]


@pytest.mark.asyncio
async def test_brain_chat_cooldown_does_not_apply_to_non_main_reply_task(monkeypatch):
    provider = _Provider()
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    monkeypatch.setattr("sirius_pulse.core.brain.asyncio.sleep", fake_sleep)

    brain = Brain(
        provider_async=provider,
        model_router=SimpleNamespace(
            resolve=lambda *args, **kwargs: SimpleNamespace(
                model_name="model",
                max_tokens=100,
                temperature=0.1,
                timeout=30,
            )
        ),
        persona=SimpleNamespace(name="tester", build_system_prompt=lambda: ""),
        config={"main_model_reply_cooldown_seconds": 5},
    )

    request = ChatRequest(
        group_id="group-1",
        user_id="u1",
        system_prompt="system",
        messages=[{"role": "user", "content": "hello"}],
        task_name="plugin_generate",
    )
    await brain.chat(request)
    await brain.chat(request)

    assert sleeps == []
