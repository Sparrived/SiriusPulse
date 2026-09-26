"""并发生成闸门：一个群的长生成不得串行阻塞其余群。

`max_concurrent_llm_calls` 曾只存在于配置模型里、没有任何运行时消费者，
等价于进程级单锁——一个群 30 秒的生成会把其余群全部串行阻塞。适配器的
处理锁同样是全局单锁，这里一并锁定它已按会话分锁。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from sirius_pulse.core.brain import Brain, ChatRequest
from sirius_pulse.providers.base import GenerationResult


class _SlowProvider:
    """记录同时在跑的生成数，用来证明闸门真的在限流。"""

    def __init__(self, hold_seconds: float = 0.05) -> None:
        self.hold_seconds = hold_seconds
        self.in_flight = 0
        self.peak = 0
        self.started = 0

    async def generate_async(self, request):
        self.started += 1
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await asyncio.sleep(self.hold_seconds)
            return GenerationResult(content="ok")
        finally:
            self.in_flight -= 1


def _brain(provider=None, **config) -> Brain:
    return Brain(
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
        config=config,
    )


def _request(group_id: str) -> ChatRequest:
    return ChatRequest(
        group_id=group_id,
        user_id="u1",
        system_prompt="system",
        messages=[{"role": "user", "content": "hello"}],
    )


# ── 并发闸门 ────────────────────────────────────────────────────────


def test_default_concurrency_serializes_calls():
    """默认值 1 必须与旧的进程级单锁行为一致，不得悄悄放开。"""
    provider = _SlowProvider()

    async def main() -> None:
        brain = _brain(provider)
        await asyncio.gather(*(brain.chat(_request(f"group-{i}")) for i in range(4)))

    asyncio.run(main())

    assert provider.started == 4
    assert provider.peak == 1


def test_configured_concurrency_lets_separate_groups_overlap():
    """调大上限后，不同群的生成可以真正并行。"""
    provider = _SlowProvider()

    async def main() -> None:
        brain = _brain(provider, max_concurrent_llm_calls=3)
        await asyncio.gather(*(brain.chat(_request(f"group-{i}")) for i in range(3)))

    asyncio.run(main())

    assert provider.started == 3
    assert provider.peak == 3


def test_zero_concurrency_means_unlimited():
    provider = _SlowProvider()

    async def main() -> None:
        brain = _brain(provider, max_concurrent_llm_calls=0)
        await asyncio.gather(*(brain.chat(_request(f"group-{i}")) for i in range(5)))

    asyncio.run(main())

    assert provider.peak == 5


def test_gate_still_releases_slots_when_a_call_fails():
    """一次失败不得永久占住名额，否则引擎会慢慢僵死。"""

    class _Switchable:
        """在 fail=True 期间整轮失败（含内部重试），之后恢复正常。"""

        def __init__(self) -> None:
            self.fail = True
            self.calls = 0

        async def generate_async(self, request):
            self.calls += 1
            if self.fail:
                raise RuntimeError("boom")
            return GenerationResult(content="ok")

    provider = _Switchable()

    async def main() -> None:
        brain = _brain(provider)
        with pytest.raises(RuntimeError):
            await brain.chat(_request("group-a"))
        provider.fail = False
        await asyncio.wait_for(brain.chat(_request("group-b")), timeout=5)

    asyncio.run(main())

    assert provider.calls >= 3


def test_invalid_concurrency_config_falls_back_to_one():
    provider = _SlowProvider()

    async def main() -> None:
        brain = _brain(provider, max_concurrent_llm_calls="不是数字")
        await asyncio.gather(*(brain.chat(_request(f"group-{i}")) for i in range(2)))

    asyncio.run(main())

    assert provider.peak == 1


def test_set_concurrency_limit_applies_on_hot_reload():
    """热重载改上限后，闸门必须立刻按新值工作。"""
    provider = _SlowProvider()

    async def main() -> None:
        brain = _brain(provider, max_concurrent_llm_calls=1)
        await asyncio.gather(*(brain.chat(_request(f"a-{i}")) for i in range(2)))
        assert provider.peak == 1

        brain.config["max_concurrent_llm_calls"] = 4
        brain.set_concurrency_limit()
        await asyncio.gather(*(brain.chat(_request(f"b-{i}")) for i in range(4)))

    asyncio.run(main())

    assert provider.peak == 4


def _fake_engine(tmp_path, config: dict) -> SimpleNamespace:
    """只带 `_init_orchestration_and_task_models()` 所需字段的假引擎。"""
    return SimpleNamespace(
        work_path=tmp_path,
        config=dict(config),
        _explicit_config_keys=set(config),
    )


def _load_orchestration(engine: SimpleNamespace) -> None:
    from sirius_pulse.core.engine_core import _EmotionalGroupChatEngineBase

    _EmotionalGroupChatEngineBase._init_orchestration_and_task_models(engine)


def test_concurrency_limit_is_read_from_orchestration_json(tmp_path):
    """orchestration.json 是并发上限的权威来源，必须真的流进引擎配置。"""
    from sirius_pulse.core.orchestration_store import OrchestrationStore

    OrchestrationStore.save(str(tmp_path), {"max_concurrent_llm_calls": 5})
    engine = _fake_engine(tmp_path, {})

    _load_orchestration(engine)

    assert engine.config["max_concurrent_llm_calls"] == 5
    assert _brain(**engine.config)._max_concurrent_llm_calls() == 5


def test_explicit_engine_config_outranks_orchestration_json(tmp_path):
    """调用方显式写死的取值不得被配置文件覆盖。"""
    from sirius_pulse.core.orchestration_store import OrchestrationStore

    OrchestrationStore.save(str(tmp_path), {"max_concurrent_llm_calls": 5})
    engine = _fake_engine(tmp_path, {"max_concurrent_llm_calls": 2})

    _load_orchestration(engine)

    assert engine.config["max_concurrent_llm_calls"] == 2


def test_hot_reload_refreshes_the_limit_from_disk(tmp_path):
    """热重载必须把磁盘上的新上限读进来，而不是留着上一轮的旧值。"""
    from sirius_pulse.core.orchestration_store import OrchestrationStore

    OrchestrationStore.save(str(tmp_path), {"max_concurrent_llm_calls": 1})
    engine = _fake_engine(tmp_path, {})
    _load_orchestration(engine)
    assert engine.config["max_concurrent_llm_calls"] == 1

    OrchestrationStore.save(str(tmp_path), {"max_concurrent_llm_calls": 6})
    _load_orchestration(engine)
    assert engine.config["max_concurrent_llm_calls"] == 6

    # 键被删除时要回落到默认值，不能留着旧值。
    OrchestrationStore.save(str(tmp_path), {})
    _load_orchestration(engine)
    assert engine.config.get("max_concurrent_llm_calls") is None


# ── 适配器处理锁的粒度 ──────────────────────────────────────────────


def _adapter(tmp_path):
    from sirius_pulse.platforms.onebot_v11.napcat.adapter import NapCatAdapter

    return NapCatAdapter("ws://example.invalid", work_path=tmp_path, config={})


def test_conversation_key_separates_groups_and_private_chats(tmp_path):
    adapter = _adapter(tmp_path)

    assert adapter._conversation_key({"group_id": "g1"}) == "group:g1"
    assert adapter._conversation_key({"group_id": "g2"}) == "group:g2"
    assert adapter._conversation_key({"user_id": "u1"}) == "private:u1"
    assert adapter._conversation_key({}) == "private:unknown"


def test_same_conversation_reuses_one_lock(tmp_path):
    adapter = _adapter(tmp_path)

    async def main():
        first = await adapter._conversation_lock("group:g1")
        second = await adapter._conversation_lock("group:g1")
        other = await adapter._conversation_lock("group:g2")
        return first, second, other

    first, second, other = asyncio.run(main())

    assert first is second, "同一会话必须共用一把锁"
    assert first is not other, "不同会话必须是不同的锁"


def test_different_groups_are_not_serialized_by_the_adapter(tmp_path):
    """一个群的长生成不得阻塞另一个群——这正是全局单锁的病症。"""
    adapter = _adapter(tmp_path)
    order: list[str] = []

    async def fake_impl(event):
        group = event["group_id"]
        order.append(f"start-{group}")
        await asyncio.sleep(0.05 if group == "slow" else 0)
        order.append(f"end-{group}")
        return True

    adapter._process_event_impl = fake_impl  # type: ignore[method-assign]

    async def main():
        await asyncio.gather(
            adapter._process_event({"group_id": "slow"}),
            adapter._process_event({"group_id": "fast"}),
        )

    asyncio.run(main())

    # 全局单锁下 fast 只能在 slow 之后结束；分锁后它能先跑完。
    assert order.index("end-fast") < order.index("end-slow")


def test_same_group_messages_stay_serialized(tmp_path):
    adapter = _adapter(tmp_path)
    order: list[str] = []

    async def fake_impl(event):
        tag = event["tag"]
        order.append(f"start-{tag}")
        await asyncio.sleep(0.02)
        order.append(f"end-{tag}")
        return True

    adapter._process_event_impl = fake_impl  # type: ignore[method-assign]

    async def main():
        await asyncio.gather(
            adapter._process_event({"group_id": "g1", "tag": "a"}),
            adapter._process_event({"group_id": "g1", "tag": "b"}),
        )

    asyncio.run(main())

    assert order == ["start-a", "end-a", "start-b", "end-b"]


# ── 当前平台来源按 task 隔离 ────────────────────────────────────────


def _bare_engine():
    """不带 __init__ 的引擎实例，只用来验证 task-local 的平台来源。"""
    from sirius_pulse.core.engine_core import _EmotionalGroupChatEngineBase

    engine = _EmotionalGroupChatEngineBase.__new__(_EmotionalGroupChatEngineBase)
    engine._default_adapter_type = ""
    return engine


def test_current_adapter_type_is_task_local():
    """两个群并发处理时，各自的平台来源不得互相覆盖。"""
    engine = _bare_engine()
    observed: dict[str, str] = {}

    async def handle(name: str, adapter_type: str) -> None:
        engine._current_adapter_type = adapter_type
        await asyncio.sleep(0)  # 让另一个 task 有机会写入
        observed[name] = engine._current_adapter_type

    async def main() -> None:
        await asyncio.gather(
            handle("a", "napcat"),
            handle("b", "other_platform"),
        )

    asyncio.run(main())

    assert observed == {"a": "napcat", "b": "other_platform"}


def test_current_adapter_type_falls_back_to_the_engine_default():
    """后台任务没有入站消息，只应看到引擎级默认值，而不是别人的适配器。"""
    engine = _bare_engine()

    assert engine._current_adapter_type == ""

    engine._default_adapter_type = "napcat"
    assert engine._current_adapter_type == "napcat"

    # 入站消息在 task 内设置的取值覆盖默认值，同时更新「最近一次」兜底。
    async def inbound() -> str:
        engine._current_adapter_type = "other_platform"
        return engine._current_adapter_type

    assert asyncio.run(inbound()) == "other_platform"
    assert engine._default_adapter_type == "other_platform"
