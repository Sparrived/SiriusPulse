"""边生成边开口：长回复的第一句不用等模型写完整段。

从群里能看到的结果出发：第一句什么时候发出、剩下的部分还会不会重复发、
出错时会不会把已经发出去的半句再说一遍。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from sirius_pulse.core.bg_tasks_delayed import DelayedQueueTasks
from sirius_pulse.core.brain import Brain, ChatRequest
from sirius_pulse.core.reply_streaming import FirstSentenceStreamer
from sirius_pulse.exceptions import ProviderConnectionError
from sirius_pulse.providers.base import GenerationResult


class _StreamingModel:
    """按段吐出正文的模型替身；``hold_after`` 段之后停住，直到放行。"""

    def __init__(self, *attempts: list[str], hold_after: int | None = None, fail: bool = False):
        self.attempts = list(attempts)
        self.hold_after = hold_after
        self.fail = fail
        self.calls = 0
        self.release = asyncio.Event()

    async def generate_async(self, request, return_reasoning=False, on_text_delta=None):
        chunks = self.attempts[min(self.calls, len(self.attempts) - 1)]
        self.calls += 1
        for index, chunk in enumerate(chunks):
            if on_text_delta is not None:
                on_text_delta(chunk)
            await asyncio.sleep(0)
            if index == self.hold_after:
                await self.release.wait()
        if self.fail or (len(self.attempts) > 1 and self.calls < len(self.attempts)):
            raise ProviderConnectionError("fake", "连接断了")
        return GenerationResult(content="".join(chunks))


def _brain(model: _StreamingModel) -> Brain:
    return Brain(
        provider_async=model,
        model_router=SimpleNamespace(
            resolve=lambda *args, **kwargs: SimpleNamespace(
                model_name="response_generate", timeout=30, fallback_model_name=""
            )
        ),
        persona=SimpleNamespace(name="月白", build_system_prompt=lambda: ""),
    )


def _request(streamer: FirstSentenceStreamer, **overrides) -> ChatRequest:
    fields = {
        "group_id": "g1",
        "user_id": "u1",
        "system_prompt": "",
        "messages": [{"role": "user", "content": "周末去哪"}],
        "enable_tools": False,
        "stream_sink": streamer,
        "retry_delay": 0.0,
    }
    fields.update(overrides)
    return ChatRequest(**fields)


@pytest.mark.asyncio
async def test_first_sentence_reaches_the_group_while_the_model_is_still_writing():
    sent: list[str] = []

    async def send(text: str) -> None:
        sent.append(text)

    model = _StreamingModel(["好呀，周末去爬山。", "我查了天气，周六晴", "。记得带水。"], hold_after=1)
    streamer = FirstSentenceStreamer(send)
    turn = asyncio.create_task(_brain(model).chat(_request(streamer)))

    for _ in range(50):
        if sent:
            break
        await asyncio.sleep(0.01)
    assert sent == ["好呀，周末去爬山。"], "模型还没写完，第一句就该已经发出"
    assert not turn.done()

    model.release.set()
    result = await turn
    await streamer.settle()

    assert result.streamed_prefix == "好呀，周末去爬山。"
    assert result.clean_text == "我查了天气，周六晴。记得带水。", "已发出的第一句不能再发一遍"
    assert sent == ["好呀，周末去爬山。"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "chunks",
    [
        pytest.param(["好呀。"], id="只有一句"),
        pytest.param(["[REPLY:3] 好呀。", "后面还有好多话要说。"], id="带引用标记"),
        pytest.param(["[SKIP]"], id="主动跳过"),
        pytest.param(["# 攻略。", "第一天去山顶看日出。"], id="Markdown"),
        pytest.param(["1. 先去山顶。", "2. 再去湖边。"], id="列表"),
    ],
)
async def test_replies_that_need_the_whole_text_are_not_sent_early(chunks):
    sent: list[str] = []

    async def send(text: str) -> None:
        sent.append(text)

    streamer = FirstSentenceStreamer(send)
    result = await _brain(_StreamingModel(chunks)).chat(_request(streamer))
    await streamer.settle()

    assert sent == []
    assert result.streamed_prefix == ""


@pytest.mark.asyncio
async def test_a_failure_after_the_first_sentence_went_out_is_not_retried():
    """已经说出口的半句收不回来：重试会生成另一段话，和发出去的对不上。"""
    sent: list[str] = []

    async def send(text: str) -> None:
        sent.append(text)

    model = _StreamingModel(["好呀，周末去爬山。", "我查了天气"], fail=True)
    streamer = FirstSentenceStreamer(send)

    with pytest.raises(ProviderConnectionError):
        await _brain(model).chat(_request(streamer, retry_max=2))
    await streamer.settle()

    assert model.calls == 1
    assert sent == ["好呀，周末去爬山。"]


@pytest.mark.asyncio
async def test_a_retry_does_not_stitch_the_failed_attempt_into_the_reply():
    sent: list[str] = []

    async def send(text: str) -> None:
        sent.append(text)

    model = _StreamingModel(["周末我们"], ["好呀，周末去爬山。", "记得带水哦。"])
    streamer = FirstSentenceStreamer(send)
    result = await _brain(model).chat(_request(streamer, retry_max=1))
    await streamer.settle()

    assert model.calls == 2
    assert sent == ["好呀，周末去爬山。"]
    assert result.clean_text == "记得带水哦。"


@pytest.mark.asyncio
async def test_the_early_sentence_is_delivered_and_kept_in_history():
    delivered: list[str] = []
    history: list[dict] = []

    async def on_partial_reply(text: str) -> None:
        delivered.append(text)

    engine = SimpleNamespace(
        config={},
        _record_assistant_message=lambda **kwargs: history.append(kwargs),
    )
    streamer = DelayedQueueTasks._first_sentence_streamer(
        engine,
        "g1",
        SimpleNamespace(user_id="u1"),
        on_partial_reply=on_partial_reply,
        eligible=True,
    )
    assert streamer is not None
    streamer.on_delta("好呀，周末去爬山。")
    streamer.on_delta("我查了天气。")
    await streamer.settle()

    assert delivered == ["好呀，周末去爬山。"]
    assert [entry["content"] for entry in history] == ["好呀，周末去爬山。"]
    assert streamer.sent_at is not None


@pytest.mark.parametrize(
    ("config", "eligible", "has_sender"),
    [({"stream_first_sentence": False}, True, True), ({}, False, True), ({}, True, False)],
    ids=["关闭开关", "工作模式", "没有发送通道"],
)
def test_streaming_is_skipped_where_text_must_not_go_out_early(config, eligible, has_sender):
    async def on_partial_reply(text: str) -> None:  # pragma: no cover - 不应被调用
        raise AssertionError(text)

    streamer = DelayedQueueTasks._first_sentence_streamer(
        SimpleNamespace(config=config),
        "g1",
        SimpleNamespace(user_id="u1"),
        on_partial_reply=on_partial_reply if has_sender else None,
        eligible=eligible,
    )
    assert streamer is None
