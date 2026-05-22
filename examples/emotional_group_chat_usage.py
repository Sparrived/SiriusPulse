"""EmotionalGroupChatEngine 完整使用示例（v0.28+）.

演示场景：
1. 创建情感化群聊引擎
2. 处理群消息（感知→认知→决策→执行）
3. 订阅事件流监控 pipeline 状态
4. 延迟响应队列管理
5. 主动触发检查
6. 状态持久化与恢复
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from sirius_pulse import (
    EmotionalGroupChatEngine,
    Message,
    Participant,
    SessionEventType,
    create_emotional_engine,
)
from sirius_pulse.providers import MockProvider


async def _event_subscriber(engine: EmotionalGroupChatEngine) -> None:
    """后台任务：订阅并打印引擎事件流."""
    async for event in engine.event_bus.subscribe():
        print(f"[事件] {event.type.value} | data={event.data}")


async def _delayed_queue_ticker(engine: EmotionalGroupChatEngine, group_id: str) -> None:
    """后台任务：每 10 秒检查一次延迟队列."""
    while True:
        await asyncio.sleep(10)
        results = await engine.tick_delayed_queue(group_id)
        for r in results:
            print(f"[延迟回复] {r['reply']}")


async def _proactive_checker(engine: EmotionalGroupChatEngine, group_id: str) -> None:
    """后台任务：每 60 秒检查一次主动触发."""
    while True:
        await asyncio.sleep(60)
        result = await engine.proactive_check(group_id)
        if result:
            print(f"[主动发起] {result['reply']}")


async def _run() -> None:
    work_path = Path("data/emotional_group_chat_demo")
    work_path.mkdir(parents=True, exist_ok=True)

    # 使用 MockProvider 演示（生产环境替换为真实 provider）
    provider = MockProvider(responses=["收到！", " interesting", "稍等我想想..."])
    engine = create_emotional_engine(work_path, provider=provider)

    # 启动事件订阅
    event_task = asyncio.create_task(_event_subscriber(engine))

    group_id = "demo_group"

    # 注册参与者
    participants = [
        Participant(name="Alice", user_id="alice"),
        Participant(name="Bob", user_id="bob"),
    ]

    # ── 场景 1：日常求助（IMMEDIATE 策略） ──
    print("=== 场景 1：求助 ===")
    msg1 = Message(
        role="human",
        content="这个项目报错了，怎么排查啊？",
        speaker="alice",
    )
    result1 = await engine.process_message(msg1, participants, group_id)
    print(f"策略: {result1['strategy']}, 回复: {result1['reply']}")

    # ── 场景 2：情感倾诉（IMMEDIATE + 共情策略） ──
    print("\n=== 场景 2：情感倾诉 ===")
    msg2 = Message(
        role="human",
        content="最近压力好大，感觉快崩溃了",
        speaker="bob",
    )
    result2 = await engine.process_message(msg2, participants, group_id)
    print(f"策略: {result2['strategy']}, 情绪: {result2['emotion']}")

    # ── 场景 3：日常闲聊（SILENT 策略） ──
    print("\n=== 场景 3：闲聊 ===")
    msg3 = Message(
        role="human",
        content="哈哈确实",
        speaker="alice",
    )
    result3 = await engine.process_message(msg3, participants, group_id)
    print(f"策略: {result3['strategy']}, 回复: {result3['reply']}")

    # ── 场景 4：保存状态 ──
    print("\n=== 保存状态 ===")
    engine.save_state()
    print("引擎状态已保存")

    # ── 场景 5：恢复状态 ──
    print("\n=== 恢复状态 ===")
    engine2 = create_emotional_engine(work_path, provider=provider)
    engine2.load_state()
    print("引擎状态已恢复")

    # 清理
    event_task.cancel()
    try:
        await event_task
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    asyncio.run(_run())
