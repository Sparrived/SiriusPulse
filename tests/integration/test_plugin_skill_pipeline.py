"""Integration tests for plugin command execution through the engine entry point."""

from __future__ import annotations

from pathlib import Path

import pytest

from sirius_pulse.core.emotional_engine import EmotionalGroupChatEngine
from sirius_pulse.memory.user.unified_models import UnifiedUser
from sirius_pulse.models.models import Message
from sirius_pulse.models.persona import PersonaProfile
from sirius_pulse.plugins import PluginBase, PluginExecutor, PluginRegistry
from sirius_pulse.plugins.models import (
    CommandAST,
    PluginCommandDef,
    PluginDefinition,
    PluginPermissionDef,
    PluginRenderDef,
    PluginResponse,
)
from sirius_pulse.providers.mock import MockProvider

pytestmark = pytest.mark.integration


class EchoPlugin(PluginBase):
    def execute(self, cmd: CommandAST) -> PluginResponse:
        text = " ".join(arg.raw for arg in cmd.args).strip()
        return PluginResponse.ok(text=f"plugin echo: {text}")


def _definition() -> PluginDefinition:
    return PluginDefinition(
        name="echo_plugin",
        display_name="Echo",
        description="Echo user command text.",
        version="1.0",
        commands=[
            PluginCommandDef(
                name="echo",
                patterns=["/echo"],
                pattern_type="prefix",
                description="Echo text.",
            )
        ],
        events=[],
        parameters=[],
        permissions=PluginPermissionDef(),
        render=PluginRenderDef(mode="direct"),
        dependencies=[],
        source_path=None,
        _plugin_class=EchoPlugin,
    )


def _engine(tmp_path: Path, provider: MockProvider) -> EmotionalGroupChatEngine:
    return EmotionalGroupChatEngine(
        work_path=tmp_path,
        persona=PersonaProfile(name="TestBot", aliases=["bot"]),
        provider_async=provider,
        config={"sensitivity": 1.0},
    )


def _close_engine(engine: EmotionalGroupChatEngine) -> None:
    for attr in (
        "event_bus",
        "semantic_memory",
        "user_manager",
        "evolution_chain",
        "provenance_store",
        "situation_store",
        "_memory_storage",
        "cognition_store",
    ):
        target = getattr(engine, attr, None)
        close = getattr(target, "close", None)
        if callable(close):
            result = close()
            if hasattr(result, "send"):
                result.close()


@pytest.mark.asyncio
async def test_plugin_pipeline_when_rule_command_matches_then_engine_executes_without_llm(
    tmp_path: Path,
):
    provider = MockProvider(["should not be used"])
    engine = _engine(tmp_path, provider)
    registry = PluginRegistry()
    registry.register(_definition(), instance=None)
    executor = PluginExecutor(
        registry,
        persona_data_path=tmp_path,
        default_execution_timeout=1.0,
        engine=engine,
    )
    engine.set_plugin_runtime(plugin_registry=registry, plugin_executor=executor)

    try:
        result = await engine.process_message(
            Message(
                role="human",
                content="/echo hello integration",
                speaker="Alice",
                channel="qq",
                channel_user_id="1001",
                message_id="msg-1",
            ),
            [
                UnifiedUser(
                    user_id="u1",
                    name="Alice",
                    identities={"qq": "1001"},
                )
            ],
            "group-a",
        )
    finally:
        _close_engine(engine)

    assert result["strategy"] == "plugin_verified"
    assert result["plugin_intent"] == "echo_plugin"
    assert result["reply"] == "plugin echo: hello integration"
    assert provider.requests == []

    entries = engine.basic_memory.get_all("group-a")
    assert [entry.role for entry in entries] == ["human", "assistant"]
    assert entries[1].content == "plugin echo: hello integration"
