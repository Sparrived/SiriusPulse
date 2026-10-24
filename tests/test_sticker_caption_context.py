"""表情包内容必须进模型上下文（用户视角回归）。

线上反馈「模型不知道别人发的表情包是什么」：读图描述原本只在纯图片消息分支生成，
而该分支排在延迟队列 pending 短路之后，于是排队合并、没拿到调度租约、图文同条
这三条路径收到的表情包，对模型来说只剩一句默认的「动画表情」。

这里只从「群友发了一张表情包」这个动作出发，断言模型最终读到的会话历史里带的是
真实的读图描述，而不是产品的默认占位符。

另外钉住工具链窗口这条路：窗口开着时 @她的消息被 inject_tool_chain_message 接管、
不进 process_message，当轮 prompt 由 _append_tool_chain_messages 渲染 —— 那轮注入的
文本同样必须带上描述，否则她得等到下一轮重建历史才看得懂。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sirius_pulse.config.models import ExpressivenessConfig
from sirius_pulse.core.bg_tasks_delayed import DelayedQueueTasks
from sirius_pulse.core.cognition import CognitionAnalyzer
from sirius_pulse.core.delayed_response_queue import DelayedResponseQueue
from sirius_pulse.core.engine_core import _EmotionalGroupChatEngineBase
from sirius_pulse.core.helpers import Helpers
from sirius_pulse.core.pipeline import Pipeline
from sirius_pulse.core.prompt_factory import PromptFactory
from sirius_pulse.core.rhythm import RhythmAnalyzer
from sirius_pulse.memory.basic.manager import BasicMemoryManager
from sirius_pulse.memory.context_assembler import ContextAssembler
from sirius_pulse.models.models import Message
from sirius_pulse.models.response_strategy import ResponseStrategy, StrategyDecision
from sirius_pulse.providers.mock import MockProvider

_GROUP_ID = "123456"
_STICKER_CAPTION = "一只橘猫瘫在键盘上，表情生无可恋。"
_PHOTO_CAPTION = "屏幕上是一张报错截图，红字提示连接超时。"


class _VisionInjectionReached(Exception):
    """边界探针：引擎准备把图片塞进视觉通道时中断，用来观察它究竟准备了什么。"""


def _participants() -> list[SimpleNamespace]:
    return [SimpleNamespace(user_id="u1", name="Alice", is_developer=False, identities={})]


def _engine_for_sticker_caption(
    responses: list[str],
) -> tuple[_EmotionalGroupChatEngineBase, MockProvider]:
    """构造一个会把消息真正写进基础记忆的引擎。

    参考 ``tests/test_reply_sending_window.py::_engine_for_sending_window``，区别是
    这里保留真实的 Pipeline.perception 与真实 BasicMemoryManager：读图描述要写回
    「落进记忆的那一条」，所以记忆必须是真的。
    """
    engine = object.__new__(_EmotionalGroupChatEngineBase)
    provider = MockProvider(responses)

    engine.persona = SimpleNamespace(name="Luna", aliases=["月白"], reply_frequency="moderate")
    engine.config = {}
    engine._current_adapter_type = ""
    engine.expressiveness = ExpressivenessConfig()
    engine.cognition_analyzer = CognitionAnalyzer(
        provider_async=provider,
        ai_name="Luna",
        ai_aliases=["月白"],
    )
    engine.cognition_store = SimpleNamespace(add=lambda **kwargs: None)
    engine.rhythm_analyzer = RhythmAnalyzer()
    engine._helpers = SimpleNamespace(get_recent_messages=lambda group_id, n: [])
    engine._last_reply_at = {}
    engine._topic_window = {}
    engine._topic_window_max_size = 10
    engine._delayed_event_emitted = {}
    engine.basic_memory = BasicMemoryManager()
    engine.basic_store = SimpleNamespace(append=lambda entry: None)
    engine.event_bus = SimpleNamespace(emit=AsyncMock(return_value=True))
    engine.delayed_queue = DelayedResponseQueue()
    engine.assistant_emotion = SimpleNamespace(
        update_from_interaction=lambda emotion, user_id: None
    )
    engine.semantic_memory = SimpleNamespace(
        get_user_profile=lambda group_id, user_id: None,
        settle_engagement=lambda **kwargs: None,
        record_interaction=lambda **kwargs: None,
        set_user_profile_fields=lambda *args, **kwargs: None,
    )
    engine._persistence = SimpleNamespace(persist_group_state=lambda group_id: None)
    engine._log_inner_thought = lambda *args, **kwargs: None
    engine._work_mode_runs = {}
    engine._group_last_message_at = {}
    engine.identity_resolver = SimpleNamespace(
        resolve=lambda *args, **kwargs: None,
        resolve_with_alias=lambda *args, **kwargs: SimpleNamespace(
            user_id="u1", confidence=1.0, source="alias"
        ),
    )
    engine.user_manager = SimpleNamespace(
        get_user=lambda user_id, group_id: SimpleNamespace(
            user_id=user_id,
            name="Alice",
            is_developer=False,
            identities={},
            identity_anchors=[],
        )
    )

    pipeline = Pipeline(engine)
    pipeline.background_update = lambda group_id, message, emotion, intent, user_id: None
    engine._pipeline = pipeline

    return engine, provider


def _sticker_input(tmp_path: Path, name: str = "keyboard_cat.gif") -> dict[str, str]:
    """NapCat 落地的表情包输入项：本地副本 + sub_type=1。"""
    path = tmp_path / name
    path.write_bytes(f"fake-sticker-bytes-{name}".encode())
    return {"type": "image", "value": str(path), "file_path": str(path), "sub_type": "1"}


def _photo_input(tmp_path: Path, name: str = "screenshot.png") -> dict[str, str]:
    path = tmp_path / name
    path.write_bytes(f"fake-photo-bytes-{name}".encode())
    return {"type": "image", "value": str(path), "file_path": str(path)}


def _image_content(image: dict[str, str]) -> str:
    """适配器渲染出的纯图片内容：``[动画表情："keyboard_cat.gif"]`` / ``[图片："a.png"]``。"""
    label = "动画表情" if image.get("sub_type") == "1" else "图片"
    return f'[{label}："{Path(image["value"]).name}"]'


def _model_visible_history(engine: _EmotionalGroupChatEngineBase, group_id: str = _GROUP_ID) -> str:
    """模型真正读到的会话历史 XML（与拼 prompt 时同一个渲染函数）。"""
    return ContextAssembler._entries_to_xml(engine.basic_memory.get_all(group_id))


def _assert_model_sees_caption(xml: str, caption: str) -> None:
    assert f'<image type="sticker" caption="{caption}"' in xml, xml
    assert caption in xml
    # 默认占位符一旦出现在历史里，模型还是只会知道「这是个动画表情」。
    assert 'caption="动画表情"' not in xml, xml


def _open_tool_chain(engine: _EmotionalGroupChatEngineBase) -> None:
    """打开工具链注入窗口：窗口开着时 @她的消息不走 process_message。"""
    engine._active_tool_chain_groups = set()
    engine._tool_chain_messages = {}
    engine.begin_tool_chain(_GROUP_ID)


async def _inject_at_persona(
    engine: _EmotionalGroupChatEngineBase,
    image: dict[str, str],
    *,
    message_id: str,
) -> bool:
    """她正忙工具链时，群友 @她并发了这张图（平台已把 @ 标成 mentions_current_bot）。"""
    return await engine.inject_tool_chain_message(
        Message(
            role="user",
            content=_image_content(image),
            speaker="Alice",
            channel="group",
            channel_user_id="200",
            message_id=message_id,
            multimodal_inputs=[image],
            mentions_current_bot=True,
        ),
        _participants(),
        _GROUP_ID,
    )


def _render_tool_chain_round(engine: _EmotionalGroupChatEngineBase) -> list[dict[str, str]]:
    """用生产渲染器拼出工具链当轮真正发给模型的注入消息。"""
    messages: list[dict[str, str]] = []
    assert DelayedQueueTasks._append_tool_chain_messages(engine, messages, _GROUP_ID) is True
    return messages


def _tagged(text: str, *, message_id: str) -> str:
    """渲染器应产出的 <message> 标签（同一条 pathway 的期望值）。"""
    return PromptFactory.tag_message(
        text,
        speaker="Alice",
        user_id="200",
        platform_message_id=message_id,
        group_id=_GROUP_ID,
    )


@pytest.mark.asyncio
async def test_sticker_joins_pending_batch_and_keeps_its_caption(tmp_path):
    """群里刚有人说完话（已有待回复批次），紧接着发的表情包也要能被看懂。"""
    engine, provider = _engine_for_sticker_caption([_STICKER_CAPTION])
    sticker = _sticker_input(tmp_path)

    await engine.process_message(
        Message(role="user", content="Luna 这个报错怎么修？", speaker="Alice"),
        _participants(),
        _GROUP_ID,
    )
    assert engine.delayed_queue.get_pending(_GROUP_ID), "前置条件：应存在待回复批次"

    result = await engine.process_message(
        Message(
            role="user",
            content=_image_content(sticker),
            speaker="Alice",
            multimodal_inputs=[sticker],
        ),
        _participants(),
        _GROUP_ID,
    )

    assert result["strategy"] == "merged"
    xml = _model_visible_history(engine)
    assert "Luna 这个报错怎么修？" in xml
    _assert_model_sees_caption(xml, _STICKER_CAPTION)
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_single_sticker_in_idle_group_keeps_its_caption(tmp_path):
    """队列为空时单独发一张表情包，同样要留下可读的描述。"""
    engine, provider = _engine_for_sticker_caption([_STICKER_CAPTION])
    sticker = _sticker_input(tmp_path)

    result = await engine.process_message(
        Message(
            role="user",
            content=_image_content(sticker),
            speaker="Alice",
            multimodal_inputs=[sticker],
        ),
        _participants(),
        _GROUP_ID,
    )

    assert result["strategy"] == "silent"
    assert result["intent"]["image_caption"] == _STICKER_CAPTION
    _assert_model_sees_caption(_model_visible_history(engine), _STICKER_CAPTION)
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_observed_sticker_without_dispatch_lease_keeps_its_caption(tmp_path):
    """没拿到群调度租约、只被 observe 记录的表情包也要能被看懂。"""
    engine, provider = _engine_for_sticker_caption([_STICKER_CAPTION])
    sticker = _sticker_input(tmp_path)

    await engine.observe_message(
        Message(
            role="user",
            content=_image_content(sticker),
            speaker="Alice",
            multimodal_inputs=[sticker],
        ),
        _participants(),
        _GROUP_ID,
    )

    _assert_model_sees_caption(_model_visible_history(engine), _STICKER_CAPTION)
    # observe 只记录不排队：她这次没被调度，但下次开口时能看懂那张图。
    assert engine.delayed_queue.get_pending(_GROUP_ID) == []
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_sticker_with_text_in_one_message_keeps_both(tmp_path):
    """表情包和文字在同一条消息里：正文照旧，图片描述也不能丢。"""
    engine, provider = _engine_for_sticker_caption([_STICKER_CAPTION])
    sticker = _sticker_input(tmp_path)
    content = f"{_image_content(sticker)}这个表情笑死我了"

    await engine.process_message(
        Message(
            role="user",
            content=content,
            speaker="Alice",
            multimodal_inputs=[sticker],
        ),
        _participants(),
        _GROUP_ID,
    )

    xml = _model_visible_history(engine)
    assert "这个表情笑死我了" in xml
    _assert_model_sees_caption(xml, _STICKER_CAPTION)
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_repeated_sticker_reuses_caption_without_second_vision_call(tmp_path):
    """同一张表情包再发一次：不该为她再花一次读图调用。"""
    engine, provider = _engine_for_sticker_caption([_STICKER_CAPTION])

    for _ in range(2):
        sticker = _sticker_input(tmp_path)
        await engine.process_message(
            Message(
                role="user",
                content=_image_content(sticker),
                speaker="Alice",
                multimodal_inputs=[sticker],
            ),
            _participants(),
            _GROUP_ID,
        )

    xml = _model_visible_history(engine)
    assert xml.count('<image type="sticker"') == 2
    assert xml.count(f'caption="{_STICKER_CAPTION}"') == 2
    assert len(provider.requests) == 1


@pytest.mark.asyncio
async def test_sticker_is_never_offered_to_the_chat_vision_channel(tmp_path):
    """表情包不进视觉通道：只有真图片会变成 image_url；表情包只以描述+本地路径出现。"""
    engine, provider = _engine_for_sticker_caption([_STICKER_CAPTION])
    sticker = _sticker_input(tmp_path)
    photo = _photo_input(tmp_path)
    offered: list[list[dict[str, str]]] = []

    def _capture_vision_payload(messages, multimodal_inputs):
        offered.append(list(multimodal_inputs or []))
        raise _VisionInjectionReached

    engine._helpers = SimpleNamespace(
        get_recent_messages=lambda group_id, n: [
            {"role": "human", "content": "早", "user_id": "u1", "multimodal_inputs": None},
            {
                "role": "human",
                "content": f"{_image_content(sticker)}这个表情笑死我了",
                "user_id": "u1",
                "multimodal_inputs": [sticker],
            },
        ],
        inject_multimodal_into_user_message=_capture_vision_payload,
    )
    engine.context_assembler = SimpleNamespace(
        build_messages_with_breakdown=lambda **kwargs: (
            [
                {"role": "system", "content": "SYS"},
                {"role": "user", "content": "USER"},
            ],
            {},
        )
    )
    photo["caption"] = _PHOTO_CAPTION
    engine.delayed_queue.enqueue(
        group_id=_GROUP_ID,
        user_id="u1",
        channel="group",
        channel_user_id="200",
        message_content=f"{_image_content(sticker)}这个表情笑死我了",
        strategy_decision=StrategyDecision(strategy=ResponseStrategy.IMMEDIATE),
        speaker_name="Alice",
        multimodal_inputs=[sticker, photo],
        adapter_type="napcat",
    )

    # 走到「把多模态塞进最后一条用户消息」这一步就停：这里正好能看清引擎决定
    # 把哪些图片交给视觉通道，而不用把整轮生成跑完。
    tasks = DelayedQueueTasks(engine)
    tasks._build_delayed_prompt = (
        lambda items, group_id, caller_is_developer=False, adapter_type=None: SimpleNamespace(
            system_prompt="SYS", user_content="USER", dynamic_context=None
        )
    )

    with pytest.raises(_VisionInjectionReached):
        await tasks._tick_delayed_queue_impl(_GROUP_ID)

    offered_values = [entry["value"] for entry in offered[0]]
    # 表情包路径也要交给注入函数：它不进视觉通道，但路径要写进文本。
    assert offered_values == [sticker["value"], photo["value"]]

    messages = Helpers.inject_multimodal_into_user_message(
        [{"role": "user", "content": "USER"}], offered[0]
    )
    parts = messages[0]["content"]
    assert isinstance(parts, list)
    image_parts = [part for part in parts if part["type"] == "image_url"]
    assert [part["image_url"]["url"] for part in image_parts] == [photo["value"]]
    assert len(image_parts) == 1, "表情包不能被当成视觉输入发出去"

    # 两个本地路径都以文本形式给了模型，模型能把它们交给生图工具当参考图。
    text = parts[0]["text"]
    assert sticker["value"] in text
    assert photo["value"] in text
    assert len(provider.requests) == 0


@pytest.mark.asyncio
async def test_tool_chain_round_text_carries_image_captions(tmp_path):
    """工具链窗口开着时 @她发的图，当轮 prompt 里就得能看出内容。"""
    engine, provider = _engine_for_sticker_caption([_STICKER_CAPTION, _PHOTO_CAPTION])
    _open_tool_chain(engine)
    sticker = _sticker_input(tmp_path)
    photo = _photo_input(tmp_path)

    assert await _inject_at_persona(engine, sticker, message_id="msg-1") is True
    assert await _inject_at_persona(engine, photo, message_id="msg-2") is True

    messages = _render_tool_chain_round(engine)

    sticker_line, photo_line = (message["content"] for message in messages)
    # 文件名标签之外多了真实描述和本地路径，当轮就看得懂、也能直接拿去当参考图。
    assert f'[动画表情："{Path(sticker["value"]).name}"]' in sticker_line
    assert f"[动画表情：{_STICKER_CAPTION}]" in sticker_line
    assert f"[本地图片路径：{sticker['value']}]" in sticker_line
    assert f'[图片："{Path(photo["value"]).name}"]' in photo_line
    assert f"[图片描述：{_PHOTO_CAPTION}]" in photo_line
    assert f"[本地图片路径：{photo['value']}]" in photo_line
    # 两条消息各带各的描述，不会串味。
    assert _PHOTO_CAPTION not in sticker_line
    assert _STICKER_CAPTION not in photo_line
    assert len(provider.requests) == 2


@pytest.mark.asyncio
async def test_tool_chain_round_text_for_plain_text_is_unchanged():
    """普通文本消息在工具链当轮渲染结果一字不变，不能凭空多出描述。"""
    engine, provider = _engine_for_sticker_caption([])
    _open_tool_chain(engine)
    accepted = await engine.inject_tool_chain_message(
        Message(
            role="user",
            content="Luna 这个报错怎么修？",
            speaker="Alice",
            channel="group",
            channel_user_id="200",
            message_id="msg-text",
            mentions_current_bot=True,
        ),
        _participants(),
        _GROUP_ID,
    )

    assert accepted is True
    assert _render_tool_chain_round(engine) == [
        {
            "role": "user",
            "content": _tagged("Luna 这个报错怎么修？", message_id="msg-text"),
        }
    ]
    assert provider.requests == []


@pytest.mark.asyncio
async def test_tool_chain_round_text_without_caption_adds_no_empty_tag(tmp_path):
    """读图没给出描述时（cache 未命中且返回空），不能追加空标签。"""
    engine, provider = _engine_for_sticker_caption([""])
    _open_tool_chain(engine)
    sticker = _sticker_input(tmp_path)

    assert await _inject_at_persona(engine, sticker, message_id="msg-empty") is True

    messages = _render_tool_chain_round(engine)

    expected_text = f"{_image_content(sticker)} [本地图片路径：{sticker['value']}]"
    assert messages == [
        {
            "role": "user",
            "content": _tagged(expected_text, message_id="msg-empty"),
        }
    ]
    assert "图片描述" not in messages[0]["content"]
    assert messages[0]["content"].count("动画表情") == 1
    # 确实读过图，只是这次没读出内容。
    assert len(provider.requests) == 1
