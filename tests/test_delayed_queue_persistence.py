"""延迟队列的持久化与失败回队。

对应评估报告 §3.4「消息静默丢失」。旧实现有两个漏洞：

1. ``tick()`` 是破坏性消费 —— 条目在生成**之前**就被移出队列并置为
   ``triggered``；生成阶段抛错时只剩一行 warning，用户的消息永久消失。
2. 延迟队列从不落盘 —— ``save_delayed_queue()`` 没有任何调用方，
   ``persist_full_state()`` 还硬编码 ``delayed_queue=[]``；重启即丢。
   更深一层：``DelayedResponseItem.to_dict()`` 读取 slots dataclass 的
   ``__dict__``，一调用就 AttributeError，所以持久化根本接不上。
"""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from sirius_pulse.core.bg_tasks_delayed import DelayedQueueTasks
from sirius_pulse.core.delayed_response_queue import (
    DEFAULT_MAX_GENERATION_RETRIES,
    DelayedResponseQueue,
)
from sirius_pulse.models.response_strategy import (
    DelayedResponseItem,
    ResponseStrategy,
    StrategyDecision,
)
from sirius_pulse.tools.models import ToolResult


def _decision(strategy: ResponseStrategy = ResponseStrategy.IMMEDIATE) -> StrategyDecision:
    return StrategyDecision(strategy=strategy, urgency=50.0, reason="test")


def _past(seconds: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(seconds=seconds)).isoformat()


# ----------------------------------------------------------------------
# to_dict / from_dict：持久化的地基
# ----------------------------------------------------------------------


def test_delayed_item_to_dict_does_not_touch_slots_dunder_dict():
    """StrategyDecision 是 slots dataclass，没有 __dict__。

    旧 to_dict() 直接取 ``self.strategy_decision.__dict__``，一调用就
    AttributeError —— 这正是延迟队列持久化一直没接上的根因。
    """
    item = DelayedResponseItem(
        item_id="i1",
        group_id="group-1",
        user_id="u1",
        message_content="你好",
        strategy_decision=_decision(ResponseStrategy.DELAYED),
    )

    payload = item.to_dict()

    assert payload["strategy_decision"]["strategy"] == "delayed"
    assert payload["message_content"] == "你好"


def test_delayed_item_dict_roundtrip_preserves_delivery_fields():
    """往返一圈后，投递所需字段（含适配器路由）必须完整保留。"""
    original = DelayedResponseItem(
        item_id="i2",
        group_id="group-2",
        user_id="u2",
        channel="qq",
        channel_user_id="999",
        message_content="在吗",
        speaker_name="Bob",
        strategy_decision=_decision(ResponseStrategy.DELAYED),
        candidate_memories=["m1"],
        enqueue_time=_past(1),
        window_seconds=12.5,
        status="pending",
        multimodal_inputs=[{"type": "image", "url": "http://x/y.png"}],
        adapter_type="napcat",
        adapter_route_id="route-7",
        heat_level="hot",
        pace="accelerating",
        related_user_ids=["u2", "u3"],
        retry_count=1,
    )

    restored = DelayedResponseItem.from_dict(original.to_dict())

    assert restored.item_id == original.item_id
    assert restored.group_id == original.group_id
    assert restored.adapter_route_id == "route-7"
    assert restored.window_seconds == 12.5
    assert restored.related_user_ids == ["u2", "u3"]
    assert restored.multimodal_inputs == [{"type": "image", "url": "http://x/y.png"}]
    assert restored.retry_count == 1
    assert restored.strategy_decision.strategy is ResponseStrategy.DELAYED


def test_delayed_item_from_dict_degrades_per_field_instead_of_dropping():
    """坏字段逐项降级；一条消息不能因为某个字段坏了就整条丢失。"""
    item = DelayedResponseItem.from_dict(
        {
            "item_id": "i3",
            "group_id": "group-3",
            "message_content": "hello",
            "window_seconds": "not-a-number",
            "strategy_decision": {"strategy": "no-such-strategy", "score": "x"},
            "related_user_ids": "not-a-list",
            "retry_count": "bad",
        }
    )

    assert item.item_id == "i3"
    assert item.message_content == "hello"
    assert item.window_seconds == 30.0
    assert item.strategy_decision.strategy is ResponseStrategy.SILENT
    assert item.related_user_ids == []
    assert item.retry_count == 0


# ----------------------------------------------------------------------
# 失败回队
# ----------------------------------------------------------------------


def test_requeue_after_failure_resets_window_start_and_counts_retry():
    """回队必须重置 enqueue_time，否则旧起点会让新窗口立刻过期。"""
    queue = DelayedResponseQueue()
    item = queue.enqueue("group-1", "u1", "hello", _decision())
    item.enqueue_time = _past(999)
    item.status = "triggered"
    queue._in_flight[item.item_id] = item

    assert queue.requeue_after_failure(item, reset_window_seconds=5.0) is True

    assert item.status == "pending"
    assert item.retry_count == 1
    assert item.window_seconds == 5.0
    elapsed = (
        datetime.now(timezone.utc) - datetime.fromisoformat(item.enqueue_time)
    ).total_seconds()
    assert elapsed < 5
    assert queue.get_pending("group-1") == [item]


def test_requeue_after_failure_gives_up_at_the_retry_cap(caplog):
    """达到上限后放弃并记 ERROR，避免坏条目无限占用 tick。"""
    queue = DelayedResponseQueue()
    item = queue.enqueue("group-1", "u1", "hello", _decision())
    item.status = "triggered"
    item.retry_count = DEFAULT_MAX_GENERATION_RETRIES
    queue._queues["group-1"] = []

    assert queue.requeue_after_failure(item) is False
    assert queue.get_pending("group-1") == []


def test_requeue_after_failure_does_not_duplicate_an_item_already_queued():
    """条目若已在队列中（例如被合并回来），回队不得产生重复。"""
    queue = DelayedResponseQueue()
    item = queue.enqueue("group-1", "u1", "hello", _decision())
    item.status = "triggered"

    assert queue.requeue_after_failure(item) is True
    assert queue.requeue_after_failure(item) is True

    assert len(queue.get_pending("group-1")) == 1


def test_commit_in_flight_marks_items_sent_and_clears_them():
    queue = DelayedResponseQueue()
    item = queue.enqueue("group-1", "u1", "hello", _decision())
    item.status = "triggered"
    queue._in_flight[item.item_id] = item

    queue.commit_in_flight([item.item_id])

    assert item.status == "sent"
    assert queue.in_flight_ids() == []


# ----------------------------------------------------------------------
# 快照 / 恢复
# ----------------------------------------------------------------------


def test_snapshot_exports_only_pending_items():
    """已触发/已取消的条目不该在重启后复活。"""
    queue = DelayedResponseQueue()
    pending = queue.enqueue("group-1", "u1", "pending one", _decision())
    triggered = queue.enqueue("group-2", "u1", "triggered one", _decision())
    triggered.status = "triggered"

    snapshot = queue.snapshot()

    assert [raw["item_id"] for raw in snapshot] == [pending.item_id]


def test_restore_rebuilds_queue_and_makes_items_immediately_due():
    """恢复的条目窗口立即到期，重启耗时不被算进等待时间。"""
    source = DelayedResponseQueue()
    item = source.enqueue("group-1", "u1", "remember me", _decision())
    item.enqueue_time = _past(9999)
    payload = source.snapshot()

    target = DelayedResponseQueue()
    assert target.restore(payload) == 1

    restored = target.get_pending("group-1")
    assert len(restored) == 1
    assert "remember me" in restored[0].message_content
    assert restored[0].window_seconds == 0.0


def test_restore_skips_corrupt_entries_without_losing_the_rest():
    source = DelayedResponseQueue()
    good = source.enqueue("group-1", "u1", "good", _decision())
    payload = source.snapshot() + [
        "not-a-dict",
        {"item_id": "", "group_id": "g"},
        {"item_id": "x", "group_id": "g", "status": "cancelled"},
        # 策略名坏掉但其余字段可用：降级为 SILENT 后仍应恢复，不能整条丢弃。
        {
            "item_id": "y",
            "group_id": "g",
            "status": "pending",
            "strategy_decision": {"strategy": "nope"},
        },
    ]

    target = DelayedResponseQueue()
    restored = target.restore(payload)

    assert restored == 2  # good + 策略名坏掉但其余可用的 y
    ids = {item.item_id for item in target.get_pending("group-1")}
    ids |= {item.item_id for item in target.get_pending("g")}
    assert good.item_id in ids


def test_restore_empty_payload_is_a_noop():
    assert DelayedResponseQueue().restore([]) == 0
    assert DelayedResponseQueue().restore(None) == 0


def test_state_store_round_trips_delayed_queue_through_disk(tmp_path):
    """验证磁盘契约：snapshot → 落盘 → 读回 → restore 后条目仍在。"""
    import json

    from sirius_pulse.core.engine_persistence import EngineStateStore

    store = EngineStateStore(tmp_path)

    source = DelayedResponseQueue()
    item = source.enqueue("group-1", "u1", "重启也要记得我", _decision())
    store.save_delayed_queue(source.snapshot())

    on_disk = json.loads(
        (tmp_path / "engine_state" / "delayed_queue.json").read_text(encoding="utf-8")
    )
    assert len(on_disk["items"]) == 1

    target = DelayedResponseQueue()
    assert target.restore(store.load_delayed_queue()) == 1
    assert [i.item_id for i in target.get_pending("group-1")] == [item.item_id]


# ----------------------------------------------------------------------
# 生成失败 → 回队（端到端）
# ----------------------------------------------------------------------


def _tasks_with_failing_chat(queue, failures: int):
    """构造一个 chat() 前 N 次抛错、之后成功的 DelayedQueueTasks。"""
    results: list[object] = [RuntimeError("提供商响应内容为空。")] * failures + [
        SimpleNamespace(
            raw_text="好了",
            clean_text="好了",
            tool_calls=[],
            reply_references=[],
            injected_request={},
        )
    ]
    profile = SimpleNamespace(name="Alice", is_developer=False)
    engine = SimpleNamespace(
        config={"max_tool_rounds": 1},
        delayed_queue=queue,
        _helpers=SimpleNamespace(
            get_recent_messages=lambda group_id, n: [],
            inject_multimodal_into_user_message=lambda messages, inputs: messages,
        ),
        rhythm_analyzer=SimpleNamespace(analyze=lambda group_id, recent: SimpleNamespace()),
        identity_resolver=SimpleNamespace(
            resolve_with_alias=lambda ctx, user_manager, group_id, **kwargs: SimpleNamespace(
                user_id="u1"
            )
        ),
        user_manager=SimpleNamespace(
            get_user=lambda user_id, group_id: profile,
            entries={"group-1": {"u1": profile}},
        ),
        semantic_memory=SimpleNamespace(
            get_user_profile=lambda group_id, user_id: SimpleNamespace(engagement_rate=1.0)
        ),
        context_assembler=SimpleNamespace(
            build_messages_with_breakdown=lambda **kwargs: (
                [
                    {"role": "system", "content": "system"},
                    {"role": "user", "content": kwargs["current_query"]},
                ],
                {},
            )
        ),
        brain=SimpleNamespace(chat=AsyncMock(side_effect=results)),
        _tool_registry=SimpleNamespace(get=lambda name: None),
        _tool_executor=SimpleNamespace(
            set_chat_context=lambda **kwargs: None,
            execute_async=AsyncMock(return_value=ToolResult(success=True, data={})),
        ),
        _log_inner_thought=lambda text: None,
        event_bus=SimpleNamespace(emit=AsyncMock()),
    )
    tasks = DelayedQueueTasks(engine)
    tasks._build_delayed_prompt = lambda *args, **kwargs: SimpleNamespace(
        system_prompt="system",
        user_content="request",
        token_breakdown=None,
        dynamic_context="",
    )
    return tasks, engine


@pytest.mark.asyncio
async def test_generation_failure_requeues_the_message_instead_of_losing_it():
    """生成失败后消息必须回到队列，而不是只留一行 warning。"""
    queue = DelayedResponseQueue()
    item = queue.enqueue("group-1", "u1", "你还在吗", _decision())
    item.enqueue_time = _past(item.window_seconds + 1)
    tasks, _engine = _tasks_with_failing_chat(queue, failures=1)

    with pytest.raises(RuntimeError):
        await tasks.tick_delayed_queue("group-1")

    pending = queue.get_pending("group-1")
    assert len(pending) == 1, "失败的消息必须重新入队"
    assert pending[0].item_id == item.item_id
    assert pending[0].retry_count == 1


@pytest.mark.asyncio
async def test_successful_generation_does_not_leave_the_item_queued():
    """成功生成后条目不应回队，否则会重复回复。"""
    queue = DelayedResponseQueue()
    item = queue.enqueue("group-1", "u1", "在吗", _decision())
    item.enqueue_time = _past(item.window_seconds + 1)
    tasks, _engine = _tasks_with_failing_chat(queue, failures=0)

    results = await tasks.tick_delayed_queue("group-1")

    assert results and results[0]["reply"] == "好了"
    assert queue.get_pending("group-1") == []
    assert queue.in_flight_ids() == []


@pytest.mark.asyncio
async def test_repeated_failures_stop_at_the_retry_cap(caplog):
    """反复失败到上限后放弃并记 ERROR，队列不再无限增长。"""
    import logging

    queue = DelayedResponseQueue()
    item = queue.enqueue("group-1", "u1", "在吗", _decision())
    item.enqueue_time = _past(item.window_seconds + 1)
    item.retry_count = DEFAULT_MAX_GENERATION_RETRIES
    tasks, _engine = _tasks_with_failing_chat(queue, failures=1)

    with caplog.at_level(logging.ERROR):
        with pytest.raises(RuntimeError):
            await tasks.tick_delayed_queue("group-1")

    assert queue.get_pending("group-1") == []
    assert queue.in_flight_ids() == []
    assert "已达重试上限被放弃" in caplog.text
