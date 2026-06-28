from __future__ import annotations

from types import SimpleNamespace

import pytest

from sirius_pulse.core.brain import Brain, ChatRequest
from sirius_pulse.providers.base import GenerationResult
from sirius_pulse.skills import SkillDefinition, SkillRegistry


def _skill(name: str) -> SkillDefinition:
    def run(**kwargs):
        return {"success": True, "text": name}

    return SkillDefinition(
        name=name,
        description=f"{name} skill",
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


@pytest.mark.asyncio
async def test_brain_chat_when_skill_is_disabled_then_tool_schema_is_not_sent_to_provider():
    provider = _Provider()
    registry = SkillRegistry()
    registry.register(_skill("send_sticker"))
    registry.register(_skill("lookup"))
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
        skill_registry=registry,
    )

    await brain.chat(
        ChatRequest(
            group_id="group-1",
            user_id="u1",
            system_prompt="system",
            messages=[{"role": "user", "content": "hello"}],
            disabled_skill_names={"send_sticker"},
        )
    )

    assert provider.last_request is not None
    tool_names = [
        tool["function"]["name"]
        for tool in (provider.last_request.tools or [])
    ]
    assert tool_names == ["lookup"]


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
    assert "latest user" in provider.last_request.messages[2]["content"]
