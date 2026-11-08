"""记忆召回：整理不卡死、窗口外原文及时整理、语义检索可恢复、冷层与上下文召回。

都从使用者能观察到的结果出发：原文是否被整理成记忆、某个问题能否召回某条记忆、
聊天回合里是否还发出网络请求。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from sirius_pulse.core.bg_tasks import BackgroundTasks
from sirius_pulse.embedding import client as embedding_client_module
from sirius_pulse.embedding.client import EmbeddingClient
from sirius_pulse.memory.basic import BasicMemoryManager
from sirius_pulse.memory.cold_detector import ColdDetector
from sirius_pulse.memory.context_assembler import ContextAssembler
from sirius_pulse.memory.units import MemoryUnit, MemoryUnitManager

GROUP = "group-a"
_VOCAB = ("猫", "火锅", "部署", "考试", "旅行", "吉他", "搬家", "面试", "生日", "游戏", "跑步", "画画")


class TopicEmbedder:
    """按主题词出现与否生成向量的确定性 embedding，并记录每次编码请求。"""

    model = "topic-test"
    available = True
    dimension = len(_VOCAB) + 1

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def encode(self, texts, timeout=None):
        self.calls.append(list(texts))
        return [[1.0 if word in text else 0.0 for word in _VOCAB] + [0.05] for text in texts]

    def encode_single(self, text):
        return self.encode([text])[0]


def _unit(unit_id: str, summary: str, **changes) -> MemoryUnit:
    values = {
        "unit_id": unit_id,
        "group_id": GROUP,
        "created_at": "2026-07-12T00:00:00+00:00",
        "summary": summary,
        "salience": 0.6,
        "confidence": 0.7,
        "source_ids": [f"src-{unit_id}"],
    }
    values.update(changes)
    return MemoryUnit(**values)


# ---------------------------------------------------------------------------
# checkpoint：闲聊不再堵死队首；窗口外的原文及时整理
# ---------------------------------------------------------------------------


class PickyModel:
    """只把含「事实」的消息记成单元，整批都是闲聊时如实返回空结果。"""

    def __init__(self) -> None:
        self.checkpointed: set[str] = set()
        self.batches: list[list[str]] = []

    def is_source_checkpointed(self, _group_id, entry_id):
        return entry_id in self.checkpointed

    async def generate_from_candidates(self, **kwargs):
        batch = list(kwargs["candidates"])
        self.batches.append([entry.content for entry in batch])
        useful = [entry.entry_id for entry in batch if "事实" in entry.content]
        self.checkpointed.update(useful)
        units = [SimpleNamespace(source_ids=useful)] if useful else []
        return SimpleNamespace(units=units, dropped=0)

    def defer_checkpoint_retry(self, *args, **kwargs):
        raise AssertionError("看过整批的结论不该被当成失败退避")


def _engine(basic, manager, *, config, assembler=None):
    return SimpleNamespace(
        config=config,
        provider_async=object(),
        basic_memory=basic,
        memory_unit_manager=manager,
        cold_detector=ColdDetector(),
        context_assembler=assembler,
        model_router=SimpleNamespace(resolve=lambda _: SimpleNamespace(model_name="m", retries=0)),
        persona=SimpleNamespace(name="月白", full_system_prompt=""),
        brain=object(),
        _bg_running=False,
    )


def test_chit_chat_never_blocks_later_facts_from_becoming_memories():
    now = datetime.now(timezone.utc)
    basic = BasicMemoryManager(hard_limit=0, context_window=5)
    for index in range(400):
        text = f"事实{index}" if index % 4 == 0 else f"哈哈{index}"
        basic.add_entry(
            GROUP, "alice", "human", text, timestamp=(now - timedelta(hours=3)).isoformat()
        )
    for index in range(5):
        basic.add_entry(GROUP, "alice", "human", f"刚说的{index}", timestamp=now.isoformat())
    model = PickyModel()
    engine = _engine(
        basic,
        model,
        config={
            "memory_idle_consolidation_seconds": 3600,
            "memory_unit_volume_threshold": 8,
            "memory_unit_token_trigger": 1,
            "memory_unit_token_target": 1,
        },
    )
    tasks = BackgroundTasks(engine)

    for _ in range(20):
        asyncio.run(tasks._checkpoint_memory_once())

    remaining = [entry.content for entry in basic.get_all(GROUP)]
    assert not [text for text in remaining if text.startswith("事实")], "事实都应变成记忆"
    assert remaining == [f"刚说的{index}" for index in range(5)], "当前对话的原文保留"


def test_active_group_summarizes_history_that_scrolled_out_of_the_prompt():
    """群一直很热、原文也没到 token 阈值时，滑出提示词窗口的原文仍要立即整理。"""
    now = datetime.now(timezone.utc)
    basic = BasicMemoryManager(hard_limit=0, context_window=5)
    for index in range(60):
        basic.add_entry(
            GROUP,
            f"user{index % 3}",
            "human",
            f"事实{index} " + "聊" * 40,
            timestamp=(now - timedelta(seconds=60 - index)).isoformat(),
        )
    model = PickyModel()
    assembler = ContextAssembler(basic, is_source_checkpointed=model.is_source_checkpointed)
    budget = 1200
    engine = _engine(
        basic,
        model,
        assembler=assembler,
        config={
            "memory_idle_consolidation_seconds": 3600,
            "memory_unit_volume_threshold": 8,
            "basic_memory_history_token_budget": budget,
        },
    )
    in_prompt_before = assembler.build_messages(GROUP, "现在", "系统", history_token_budget=budget)
    outside_before = assembler.entries_outside_history_window(GROUP, budget)
    assert len(outside_before) >= 8

    asyncio.run(BackgroundTasks(engine)._checkpoint_memory_once())

    assert assembler.entries_outside_history_window(GROUP, budget) == []
    summarized = {text for batch in model.batches for text in batch}
    assert {entry.content for entry in outside_before} <= summarized
    in_prompt_after = assembler.build_messages(GROUP, "现在", "系统", history_token_budget=budget)
    assert in_prompt_after[1:] == in_prompt_before[1:], "提示词里的原文不受影响"


def test_reviewed_batch_without_memories_is_a_result_not_a_failure(tmp_path):
    manager = MemoryUnitManager(tmp_path)
    basic = BasicMemoryManager(context_window=0)
    candidates = [basic.add_entry(GROUP, "alice", "human", f"哈哈{i}") for i in range(8)]

    async def run(reply: str):
        brain = SimpleNamespace(raw_call=lambda request: asyncio.sleep(0, result=reply))
        return await manager.generate_from_candidates(
            group_id=GROUP,
            candidates=candidates,
            persona_name="月白",
            persona_description="",
            brain=brain,
            model_name="memory_extract",
        )

    reviewed = asyncio.run(run(json.dumps({"units": []})))
    assert reviewed is not None and reviewed.units == []

    unsourced = json.dumps({"units": [{"summary": "有人在笑", "source_indices": [99]}]})
    assert asyncio.run(run(unsourced)) is None, "模型想记却给不出来源，仍按失败退避"


# ---------------------------------------------------------------------------
# embedding：一次失败不再永久关掉语义检索
# ---------------------------------------------------------------------------


def test_semantic_search_recovers_after_a_transient_embedding_failure(monkeypatch):
    client = EmbeddingClient("http://127.0.0.1:9")
    monkeypatch.setattr(client, "_check_health", lambda: True)
    assert client.available is True

    with pytest.raises(RuntimeError):
        client.encode(["一次超时"], timeout=0.01)
    assert client.available is False, "失败后先进入冷却"

    monkeypatch.setattr(embedding_client_module, "UNAVAILABLE_RETRY_SECONDS", 0.0)
    client._set_available(False)
    assert client.available is True, "冷却结束后重新探测并恢复"


# ---------------------------------------------------------------------------
# 检索：聊天回合内不发网络请求；冷层；短追问；按相关度决定条数
# ---------------------------------------------------------------------------


def _manager(tmp_path, units, monkeypatch, *, active_limit=500):
    monkeypatch.setattr(
        "sirius_pulse.memory.units.manager.DEFAULT_MEMORY_UNIT_ACTIVE_LIMIT", active_limit
    )
    embedder = TopicEmbedder()
    manager = MemoryUnitManager(tmp_path, embedding_client=embedder)
    asyncio.run(manager.add_units(GROUP, units))
    return manager, embedder


def test_prefetched_retrieval_sends_no_embedding_request_inside_the_chat_turn(
    tmp_path, monkeypatch
):
    manager, embedder = _manager(tmp_path, [_unit("cat", "小林家的猫叫团子。")], monkeypatch)
    manager.prepare_retrieval("你家猫最近怎样", group_id=GROUP)
    calls_before = len(embedder.calls)

    found = manager.retrieve("你家猫最近怎样", group_id=GROUP)

    assert [unit.unit_id for unit in found] == ["cat"]
    assert len(embedder.calls) == calls_before


def test_overflow_retired_memory_is_still_recalled_when_it_is_exactly_relevant(
    tmp_path, monkeypatch
):
    units = [
        _unit("deploy-1", "上周部署了新版本。", salience=0.9, confidence=0.9),
        _unit("game-1", "大家周末一起打游戏。", salience=0.9, confidence=0.9),
        _unit("guitar", "阿青在学吉他，卡在横按。", salience=0.1, confidence=0.5),
        _unit(
            "hidden",
            "阿青说过吉他是前任送的，不想再提。",
            salience=0.1,
            confidence=0.5,
            should_prompt=False,
            metadata={"retired": "manual"},
        ),
    ]
    manager, _ = _manager(tmp_path, units, monkeypatch, active_limit=2)
    by_id = {unit.unit_id: unit for unit in manager.get_units_for_group(GROUP)}
    assert by_id["guitar"].should_prompt is False, "低显著度单元已超额退休"

    manager.prepare_retrieval("吉他练得怎么样了", group_id=GROUP)
    found = [unit.unit_id for unit in manager.retrieve("吉他练得怎么样了", group_id=GROUP)]

    assert found == ["guitar"]
    assert "hidden" not in found, "手动隐藏的记忆永远不被冷层捞回"


def test_short_follow_up_recalls_memory_through_recent_conversation(tmp_path, monkeypatch):
    units = [
        _unit("move", "小林下个月搬家到杭州。"),
        _unit("run", "阿青每天早上跑步。"),
    ]
    manager, _ = _manager(tmp_path, units, monkeypatch)
    context = "小林: 我最近在忙搬家的事"

    assert manager.retrieve("那后来呢", group_id=GROUP) == []
    manager.prepare_retrieval("那后来呢", group_id=GROUP, context=context)
    found = manager.retrieve("那后来呢", group_id=GROUP, context=context)

    assert [unit.unit_id for unit in found] == ["move"]


def test_number_of_injected_memories_follows_relevance_not_a_fixed_five(tmp_path, monkeypatch):
    relevant = [_unit(f"hotpot-{i}", f"第{i}次吃火锅的记录。") for i in range(12)]
    unrelated = [_unit("exam", "阿青下周考试，考完去旅行。", keywords=["火锅"])]
    manager, _ = _manager(tmp_path, [*relevant, *unrelated], monkeypatch)
    manager.prepare_retrieval("火锅", group_id=GROUP)

    found = [unit.unit_id for unit in manager.retrieve("火锅", group_id=GROUP, top_k=10)]

    assert len(found) == 10 and all(unit_id.startswith("hotpot-") for unit_id in found)
