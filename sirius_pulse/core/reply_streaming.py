"""边生成边开口：模型还在写后面的话时，先把写完的第一句发出去。

一轮回复要等模型整段写完才发，群里看到的就是「对方正在输入」挂好几秒、然后一口气
冒出好几条。这里接住流式增量，第一句一写完、而且后面确实还有话（说明不是只有这
一句），就把它当成一条独立消息先发出去；剩下的照常走完整的后处理和投递。

这不是投递层的分段：怎么切成气泡、每段停多久仍由平台适配器决定。这里只决定
「什么时候可以不等了」，而且只在第一句是纯文本时才这么做——带交互标记（引用、
表情、戳一戳、[SKIP]）、XML 回显或 Markdown 的开头一律等整段结果，交给原有逻辑。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)

#: 句末标点。英文句点不在内：「3.5」「v1.2」里的点不是句子结束。
_SENTENCE_END = "。！？!?…～~\n"
#: 句末标点后面可以一起带走的收尾符号（引号、括号）。
_CLOSERS = "」』”’\"')）】"
#: 第一句里出现这些就不提前发：交互标记、XML 回显、Markdown 都要看整段才能处理。
_UNSAFE_CHARS = re.compile(r"[\[\]<>`*#|]")
_LIST_START = re.compile(r"^\s*(?:[-•·>]|\d+[.、)）])")

#: 第一句至少这么多个非空白字符：单独一个「！」不值得先发。
MIN_SENTENCE_CHARS = 2
#: 第一句之后至少已经写出这么多字，才算「后面确实还有话」。只说一句的回复不必拆，
#: 也能挡住「我记一下。」紧接着调用记事工具这类本不该外发的草稿。
MIN_TAIL_CHARS = 4


def first_sentence_end(text: str) -> int | None:
    """第一句结束的位置（含句末标点与收尾引号）；还没有完整的一句时返回 None。"""
    for index, char in enumerate(text):
        if char not in _SENTENCE_END:
            continue
        end = index + 1
        while end < len(text) and (text[end] in _SENTENCE_END or text[end] in _CLOSERS):
            end += 1
        if len(text[:end].strip()) >= MIN_SENTENCE_CHARS + 1:
            return end
    return None


def is_plain_sentence(text: str) -> bool:
    """这一句能不能不经后处理直接发：不含标记、回显、Markdown，也不是列表开头。"""
    return not _UNSAFE_CHARS.search(text) and not _LIST_START.match(text)


class FirstSentenceStreamer:
    """接住一轮生成的正文增量，第一句可发时立即交给 ``send``。

    作为 ``ChatRequest.stream_sink`` 交给 Brain：``on_delta`` 在读流的协程里同步调用，
    所以真正的发送放进后台任务，不耽误读下一段增量；调用方在拿到完整结果后
    ``await settle()``，发送失败会在那里抛出。

    ``committed`` 是已经决定发出去的那段**原文**前缀（含原样的空白），Brain 用它
    从完整结果里去掉已发出的部分。
    """

    def __init__(
        self,
        send: Callable[[str], Awaitable[None]],
        *,
        min_tail_chars: int = MIN_TAIL_CHARS,
    ) -> None:
        self._send = send
        self._min_tail_chars = max(1, int(min_tail_chars))
        self._buffer = ""
        self._blocked = False
        self._task: asyncio.Task[None] | None = None
        self.committed = ""
        self.sent_at: float | None = None

    def reset(self) -> None:
        """丢掉还没决定发出的增量（Brain 每次重试前调用）；已发出的收不回来，不动。"""
        if not self.committed:
            self._buffer = ""
            self._blocked = False

    def on_delta(self, text: str) -> None:
        if self.committed or self._blocked or not text:
            return
        self._buffer += text
        end = first_sentence_end(self._buffer)
        head = self._buffer if end is None else self._buffer[:end]
        if not is_plain_sentence(head):
            # 第一句不是纯文本：这一轮整段交给原有后处理，不再尝试。
            self._blocked = True
            return
        if end is None or len(self._buffer[end:].strip()) < self._min_tail_chars:
            return
        self.committed = self._buffer[:end]
        sentence = self.committed.strip()
        self._task = asyncio.get_running_loop().create_task(
            self._deliver(sentence), name="stream-first-sentence"
        )

    async def _deliver(self, sentence: str) -> None:
        await self._send(sentence)
        self.sent_at = time.monotonic()
        logger.info("回复还没写完，先把第一句发出去了：%s", sentence[:40])

    async def settle(self) -> None:
        """等提前发送完成；发送失败在这里抛出。"""
        if self._task is not None:
            await self._task
