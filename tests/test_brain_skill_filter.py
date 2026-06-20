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
