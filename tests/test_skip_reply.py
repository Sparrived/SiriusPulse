from __future__ import annotations

from types import SimpleNamespace

import pytest

from sirius_pulse.core.brain import Brain, ChatRequest
from sirius_pulse.core.engine_core import _EmotionalGroupChatEngineBase
from sirius_pulse.core.prompt_factory import TAG_SKIP_REPLY, PromptFactory
from sirius_pulse.providers.mock import MockProvider


def _model_router() -> SimpleNamespace:
    return SimpleNamespace(
        resolve=lambda *args, **kwargs: SimpleNamespace(
            model_name="mock-model",
            max_tokens=100,
            temperature=0.1,
            timeout=30,
        )
    )


def _engine_with_hooks(provider: MockProvider) -> tuple[_EmotionalGroupChatEngineBase, list[str]]:
    """Build an engine shell with the real post-hooks and a mock provider."""
    engine = _EmotionalGroupChatEngineBase.__new__(_EmotionalGroupChatEngineBase)
    engine.persona = SimpleNamespace(name="月白", build_system_prompt=lambda: "")
    engine.brain = Brain(
        provider_async=provider,
        model_router=_model_router(),
        persona=engine.persona,
    )
    engine._last_reply_at = {}
    engine._last_reply_depth = {}
    engine._recent_sent_replies = {}
    engine._qq_group_members = {}
    engine._reply_dedup_window = 0
    engine._reply_dedup_threshold = 1.0
    engine._record_assistant_message = lambda **kwargs: None
    persisted: list[str] = []
    engine._persist_group_state = persisted.append
    engine._register_engine_hooks()
    return engine, persisted


async def _reply_to(engine: _EmotionalGroupChatEngineBase, content: str):
    return await engine.brain.chat(
        ChatRequest(
            group_id="group-1",
            user_id="u1",
            system_prompt="system",
            messages=[{"role": "user", "content": content}],
            post_process=True,
        )
    )


def test_reply_spec_tells_model_to_skip_replies_meant_for_someone_else():
    spec = PromptFactory.build_reply_spec()

    assert TAG_SKIP_REPLY in spec
    assert "叫别人" in spec
    assert "跳过本轮回复" in spec
    assert "你叫错人了" in spec


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    ["[SKIP]", "  [skip]  ", "【SKIP】", "<skip/>", "好，那我不接话了\n[SKIP]"],
)
async def test_engine_when_model_skips_then_nothing_is_sent_and_turn_is_not_counted(content):
    engine, persisted = _engine_with_hooks(MockProvider([content]))

    result = await _reply_to(engine, "@阿离 帮我看下这个报错")

    assert result.clean_text == ""
    assert engine._last_reply_at == {}
    assert engine._last_reply_depth == {}
    assert persisted == []


@pytest.mark.asyncio
async def test_engine_when_reply_is_normal_then_turn_is_counted_as_a_reply():
    engine, persisted = _engine_with_hooks(MockProvider(["在的，怎么了？"]))

    result = await _reply_to(engine, "月白在吗")

    assert result.clean_text == "在的，怎么了？"
    assert engine._last_reply_at
    assert engine._last_reply_depth == {"group-1": 1}
    assert persisted == ["group-1"]


@pytest.mark.asyncio
async def test_engine_when_skip_word_is_only_part_of_the_sentence_then_reply_is_kept():
    engine, _ = _engine_with_hooks(MockProvider(["这句不用 skip 标记"]))

    result = await _reply_to(engine, "skip 是怎么用的？")

    assert result.clean_text == "这句不用 skip 标记"
