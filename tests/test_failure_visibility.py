"""失败可见性与入口队列上限。

评估指出两类「静默失效」：
1. 大量 `except: pass` / 只写 DEBUG 的处理器把真实故障吞掉，表现为功能
   莫名其妙不生效、日志里什么都没有。
2. 适配器的 `wait_event()` 旁路队列无上限，长跑进程会一直堆积。

这里覆盖其中的入口队列上限与关键告警路径。
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

from sirius_pulse.core.pipeline import Pipeline
from sirius_pulse.platforms.onebot_v11.napcat.adapter import (
    _EVENT_QUEUE_MAX_SIZE,
    NapCatAdapter,
)


def _event(index: int) -> dict:
    return {"post_type": "message", "message_id": f"m-{index}"}


# ── 事件旁路队列上限 ────────────────────────────────────────────────


def test_event_queue_is_bounded(tmp_path):
    adapter = NapCatAdapter("ws://example.invalid", work_path=tmp_path, config={})

    assert adapter._event_queue.maxsize == _EVENT_QUEUE_MAX_SIZE
    assert adapter._event_queue.maxsize > 0


def test_event_queue_drops_oldest_when_full(tmp_path):
    """没有消费者时保留最新事件，绝不阻塞入站消息处理。"""
    adapter = NapCatAdapter("ws://example.invalid", work_path=tmp_path, config={})

    for i in range(_EVENT_QUEUE_MAX_SIZE + 10):
        adapter._enqueue_event_for_waiters(_event(i))

    assert adapter._event_queue.qsize() == _EVENT_QUEUE_MAX_SIZE
    # 队首应是「最早还活着的那条」= 总数 - 容量，而不是第 0 条。
    first = adapter._event_queue.get_nowait()
    assert first["message_id"] == f"m-{10}"
    last = None
    while not adapter._event_queue.empty():
        last = adapter._event_queue.get_nowait()
    assert last["message_id"] == f"m-{_EVENT_QUEUE_MAX_SIZE + 9}"


def test_enqueue_never_raises_when_full(tmp_path):
    """队列满必须是丢弃而不是抛异常——调用方是入站消息处理路径。"""
    adapter = NapCatAdapter("ws://example.invalid", work_path=tmp_path, config={})

    for i in range(_EVENT_QUEUE_MAX_SIZE * 2):
        adapter._enqueue_event_for_waiters(_event(i))  # 不应抛 QueueFull

    assert adapter._event_queue.full()


# ── 静默处理器现在会留痕 ────────────────────────────────────────────


def test_cognition_store_failure_is_logged(caplog):
    """认知事件落库失败必须留 WARNING，否则「认知库为什么缺记录」无从查起。"""

    class _BrokenStore:
        def add(self, **kwargs):
            raise RuntimeError("db is locked")

    pipeline = Pipeline.__new__(Pipeline)
    pipeline._engine = SimpleNamespace(cognition_store=_BrokenStore())

    signal = SimpleNamespace(
        emotion=None,
        social_intent="social",
        urgency_score=0.5,
        relevance_score=0.5,
        directed_score=0.0,
        sarcasm_score=0.0,
        entitlement_score=0.0,
        turn_gap_readiness=0.0,
    )

    with caplog.at_level(logging.WARNING):
        # 不应抛异常：认知事件落库失败不能拖垮本轮回复。
        pipeline._persist_cognition_event("group-1", "u1", signal)

    assert any("认知事件落库失败" in record.message for record in caplog.records)


def test_cognition_store_success_is_silent(caplog):
    """正常路径不该刷 WARNING。"""
    written: list[dict] = []

    class _Store:
        def add(self, **kwargs):
            written.append(kwargs)

    pipeline = Pipeline.__new__(Pipeline)
    pipeline._engine = SimpleNamespace(cognition_store=_Store())

    signal = SimpleNamespace(
        emotion=None,
        social_intent="social",
        urgency_score=0.5,
        relevance_score=0.5,
        directed_score=0.0,
        sarcasm_score=0.0,
        entitlement_score=0.0,
        turn_gap_readiness=0.0,
    )

    with caplog.at_level(logging.WARNING):
        pipeline._persist_cognition_event("group-1", "u1", signal)

    assert len(written) == 1
    assert written[0]["group_id"] == "group-1"
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]
