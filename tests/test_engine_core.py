from __future__ import annotations

from types import SimpleNamespace

import pytest

from sirius_pulse.core import engine_core
from sirius_pulse.core.brain import Brain, ChatRequest
from sirius_pulse.core.engine_core import _EmotionalGroupChatEngineBase
from sirius_pulse.memory.basic import BasicMemoryManager
from sirius_pulse.providers.base import GenerationRequest, GenerationResult, ToolCall
from sirius_pulse.providers.mock import MockProvider

_LONG_REPLY = "第一段。\n第二段。\n第三段。\n第四段。"


class _ToolCallingProvider(MockProvider):
    """在普通文本之外再带上一个工具调用，复现「正文与工具同轮返回」。"""

    def __init__(self, content: str, tool_calls: list[ToolCall]) -> None:
        super().__init__([content])
        self._tool_calls = tool_calls

    async def generate_async(
        self, request: GenerationRequest, return_reasoning: bool = False
    ) -> GenerationResult:
        result = await super().generate_async(request, return_reasoning)
        result.tool_calls = self._tool_calls
        return result


def _engine_with_hooks(provider: MockProvider) -> _EmotionalGroupChatEngineBase:
    """装上真实 post-hooks 的引擎外壳，用来观察去重与记忆两个 hook 的实际行为。"""
    engine = _EmotionalGroupChatEngineBase.__new__(_EmotionalGroupChatEngineBase)
    engine.persona = SimpleNamespace(name="月白", build_system_prompt=lambda: "")
    engine.brain = Brain(
        provider_async=provider,
        model_router=SimpleNamespace(
            resolve=lambda *args, **kwargs: SimpleNamespace(
                model_name="mock-model",
                max_tokens=100,
                temperature=0.1,
                timeout=30,
            )
        ),
        persona=engine.persona,
    )
    engine._last_reply_at = {}
    engine._last_reply_depth = {}
    engine._recent_sent_replies = {}
    engine._qq_group_members = {}
    engine._reply_dedup_window = 300
    engine._reply_dedup_threshold = 0.85
    engine.basic_memory = BasicMemoryManager()
    stored = []
    engine.basic_store = SimpleNamespace(append=stored.append)
    engine.semantic_memory = SimpleNamespace(record_ai_sent=lambda **kwargs: None)
    engine._persist_group_state = lambda group_id: None
    engine._register_engine_hooks()
    return engine


async def _reply(
    engine: _EmotionalGroupChatEngineBase,
    content: str,
    *,
    tool_choice: str | None = None,
):
    return await engine.brain.chat(
        ChatRequest(
            group_id="group-1",
            user_id="u1",
            system_prompt="system",
            messages=[{"role": "user", "content": "看看部署结果"}],
            tool_choice=tool_choice,
            post_process=True,
        )
    )


def test_engine_when_pending_message_is_low_information_then_detects_filler():
    assert _EmotionalGroupChatEngineBase._is_low_information_pending_message("哈哈") is True
    assert _EmotionalGroupChatEngineBase._is_low_information_pending_message("ok") is True
    assert _EmotionalGroupChatEngineBase._is_low_information_pending_message("怎么了？") is False


def test_engine_orchestration_defaults_when_no_config_then_no_task_overrides(tmp_path):
    """没有人格编排配置时不预设任何模型相关的覆盖项。"""
    engine = engine_core._EmotionalGroupChatEngineBase.__new__(
        engine_core._EmotionalGroupChatEngineBase
    )
    engine.work_path = tmp_path
    engine.config = {}

    engine._init_orchestration_and_task_models()
    engine._init_model_router()

    # 每个任务都原样发出任务名，不因缺失配置而换成某个本地模型。
    assert engine.model_router.resolve("cognition_analyze").model_name == "cognition_analyze"
    assert engine.model_router.resolve("memory_extract").model_name == "memory_extract"


def test_engine_orchestration_custom_config_then_only_local_fields_are_applied(tmp_path):
    """编排配置里只有超时与重试属于本地；模型字段一律被忽略。"""
    from sirius_pulse.core.orchestration_store import OrchestrationStore

    OrchestrationStore.save(
        tmp_path,
        {
            "analysis_model": "vision-model",
            "chat_model": "chat-model",
            "task_models": {"cognition_analyze": "memory-model"},
            "task_timeout": {"response_generate": 45.0},
            "task_retries": {"memory_extract": 3},
        },
    )
    engine = engine_core._EmotionalGroupChatEngineBase.__new__(
        engine_core._EmotionalGroupChatEngineBase
    )
    engine.work_path = tmp_path
    engine.config = {}

    engine._init_orchestration_and_task_models()
    engine._init_model_router()

    assert engine.model_router.resolve("response_generate").timeout == 45.0
    assert engine.model_router.resolve("memory_extract").retries == 3
    # 配置里写了模型名也不生效：模型归 AMKR 的任务定义。
    assert engine.model_router.resolve("cognition_analyze").model_name == "cognition_analyze"


def test_engine_adapter_routes_when_registered_then_resolve_groups_and_private_users():
    engine = _EmotionalGroupChatEngineBase.__new__(_EmotionalGroupChatEngineBase)
    engine._adapter_routes = []
    first = SimpleNamespace(adapter_type="napcat")
    second = SimpleNamespace(adapter_type="discord")

    third = SimpleNamespace(adapter_type="napcat")
    engine.register_adapter(first, group_ids=["g1", "g2"], private_user_ids=[])
    engine.register_adapter(second, group_ids=["g2"], private_user_ids=["u2"])
    engine.register_adapter(third, group_ids=["g1"], private_user_ids=[])

    assert engine.resolve_adapter_types("g1") == ["napcat"]
    assert engine.resolve_adapter_types("g2") == ["napcat", "discord"]
    assert engine.resolve_adapter_types("g3") == []
    assert engine.resolve_adapter_types("private_u2") == ["napcat", "discord"]
    assert engine.resolve_adapter_types("private_u1") == ["napcat"]
    assert engine.resolve_adapter_route_counts("g1") == {"napcat": 2}
    assert engine.resolve_adapter_route_counts("g2") == {"napcat": 1, "discord": 1}

    engine.unregister_adapter(first)
    assert engine.resolve_adapter_types("g1") == ["napcat"]
    assert engine.resolve_adapter_route_counts("g1") == {"napcat": 1}


def test_engine_records_delivered_markdown_card_in_basic_history():
    engine = _EmotionalGroupChatEngineBase.__new__(_EmotionalGroupChatEngineBase)
    stored = []
    semantic = []
    persisted = []
    engine.persona = SimpleNamespace(name="月白")
    engine.basic_memory = BasicMemoryManager()
    engine.basic_store = SimpleNamespace(append=stored.append)
    engine.semantic_memory = SimpleNamespace(
        record_ai_sent=lambda **kwargs: semantic.append(kwargs)
    )
    engine._persist_group_state = persisted.append

    engine._record_assistant_message(
        group_id="9001",
        target_user_id="1001",
        content="部署结论\n\n- 服务已恢复",
        tags=[{"type": "image", "label": "富文本卡片"}],
        platform_message_id="42",
        injected_request={
            "system_prompt": "完整 system",
            "messages": [{"role": "user", "content": "完整 user"}],
            "tools": [],
            "tool_choice": None,
        },
    )

    entry = engine.basic_memory.get_context("9001", n=1)[0]
    assert entry.content == "部署结论\n\n- 服务已恢复"
    assert entry.tags == [{"type": "image", "label": "富文本卡片"}]
    assert entry.platform_message_id == "42"
    assert entry.system_prompt == "完整 system"
    assert entry.injected_request == {"tool_choice": None}
    assert stored == [entry]
    assert semantic[0]["target_user_id"] == "1001"
    assert persisted == ["9001"]


@pytest.mark.asyncio
async def test_engine_when_long_reply_goes_out_as_text_then_it_is_still_recorded():
    """超过三条的最终回复若仍走文本通道，必须照常写进记忆。"""
    engine = _engine_with_hooks(MockProvider([_LONG_REPLY]))

    result = await _reply(engine, "看看部署结果", tool_choice="none")

    # 撞到工具轮次上限后单独补出的这一轮带 tool_choice="none"，调度器的图片分支到不了，
    # 文本会被适配器原样发出，所以记忆必须由 hook 记下来，否则这段回复彻底丢失。
    assert result.clean_text == _LONG_REPLY
    entries = engine.basic_memory.get_all("group-1")
    assert [entry.content for entry in entries] == [_LONG_REPLY]


@pytest.mark.asyncio
async def test_engine_when_long_reply_is_delivered_as_image_then_hook_leaves_it_to_scheduler():
    """真正会转成图片的那一轮由调度器记录，hook 不能抢先记一遍。"""
    engine = _engine_with_hooks(MockProvider([_LONG_REPLY]))

    await _reply(engine, "看看部署结果")

    assert engine.basic_memory.get_all("group-1") == []


@pytest.mark.asyncio
async def test_engine_when_reply_carries_a_tool_call_then_its_text_is_recorded():
    """正文与工具调用同轮返回时，正文是当普通消息发出去的，不该按图片归档。"""
    engine = _engine_with_hooks(
        _ToolCallingProvider(
            _LONG_REPLY,
            [ToolCall(id="call-1", function_name="lookup", function_arguments="{}")],
        )
    )

    await _reply(engine, "看看部署结果")

    entries = engine.basic_memory.get_all("group-1")
    assert [entry.content for entry in entries] == [_LONG_REPLY]


@pytest.mark.asyncio
async def test_engine_when_long_reply_is_sent_as_text_then_dedup_still_sees_it():
    """走文本通道的长回复要进去重窗口，否则会被原样重复发第二遍。"""
    engine = _engine_with_hooks(MockProvider([_LONG_REPLY, _LONG_REPLY]))

    await _reply(engine, "看看部署结果", tool_choice="none")
    second = await _reply(engine, "看看部署结果", tool_choice="none")

    assert second.clean_text == ""


@pytest.mark.asyncio
async def test_engine_when_long_reply_is_delivered_as_image_then_dedup_leaves_it_alone():
    """会转成图片的长回复不进去重窗口，避免恰好相似的两张卡片被吞掉一张。"""
    engine = _engine_with_hooks(MockProvider([_LONG_REPLY]))

    await _reply(engine, "看看部署结果")

    assert engine._recent_sent_replies.get("group-1") in (None, [])
