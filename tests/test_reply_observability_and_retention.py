"""运维能看到「她为什么没回」、分析数据不会无限增长、归档补写不重写全文件。"""

from __future__ import annotations

import time

import pytest

from sirius_pulse.core.engine_core import _EmotionalGroupChatEngineBase
from sirius_pulse.memory.basic import store as basic_store_module
from sirius_pulse.memory.basic.models import BasicMemoryEntry
from sirius_pulse.memory.basic.store import BasicMemoryFileStore
from sirius_pulse.platforms.onebot_v11.napcat.adapter import NapCatAdapter
from sirius_pulse.providers.mock import MockProvider


def _engine(tmp_path, **config) -> _EmotionalGroupChatEngineBase:
    return _EmotionalGroupChatEngineBase(
        work_path=tmp_path,
        provider_async=MockProvider(),
        persona={"name": "月白"},
        config={"pending_message_threshold": 0, **config},
    )


def _decide(engine, content: str, *, group_id: str = "g1", user_id: str = "u1") -> str:
    signal = engine._compute_signal(content, user_id, group_id)
    return engine._pre_filter(signal, content, user_id, group_id)


def _decisions(engine, group_id: str = "g1") -> list[dict]:
    engine.cognition_store.flush()
    return engine.cognition_store.get_decision_events(group_id=group_id)


@pytest.mark.parametrize(
    ("content", "config", "reason"),
    [
        ("!roll 100", {"message_prefixes": ["!"]}, "prefix_filtered"),
        ("ok", {}, "too_short"),
    ],
)
def test_when_she_stays_silent_then_the_reason_is_recorded(tmp_path, content, config, reason):
    engine = _engine(tmp_path, **config)

    assert _decide(engine, content) == "reject"

    [record] = _decisions(engine)
    assert record["strategy"] == "silent"
    assert record["reason"] == reason
    assert record["user_id"] == "u1"


def test_when_she_is_called_by_name_then_the_pass_decision_is_recorded_too(tmp_path):
    engine = _engine(tmp_path)

    assert _decide(engine, "月白，帮我看看这个报错怎么修？") == "pass"

    [record] = _decisions(engine)
    assert record["strategy"] != "silent"
    assert record["reason"]
    assert record["score"] >= record["threshold"]


def test_old_analytics_events_are_pruned_but_recent_ones_stay(tmp_path):
    engine = _engine(tmp_path, cognition_retention_days=30)
    store = engine.cognition_store
    store.add_decision(group_id="g1", reason="old", timestamp=time.time() - 40 * 86400)
    store.add_decision(group_id="g1", reason="recent")

    removed = engine._bg_tasks_mgr.prune_old_analytics()

    assert removed == 1
    assert [row["reason"] for row in _decisions(engine)] == ["recent"]


def _entry(index: int) -> BasicMemoryEntry:
    return BasicMemoryEntry(
        entry_id=f"e{index}",
        group_id="g1",
        user_id="u1",
        role="human",
        content=f"第 {index} 条消息，凑点长度好让归档超过补写窗口。" * 3,
        timestamp=f"2026-11-13T10:{index // 60:02d}:{index % 60:02d}+00:00",
    )


@pytest.mark.parametrize("target", [199, 3])
def test_image_caption_patch_survives_restart_whether_entry_is_recent_or_old(
    tmp_path, monkeypatch, target
):
    """补写最近一条走尾部原地替换，补写很早的一条走整份重写；两种结果读回来都一样。"""
    monkeypatch.setattr(basic_store_module, "_TAIL_WINDOW_BYTES", 2048)
    store = BasicMemoryFileStore(tmp_path)
    for index in range(200):
        store.append(_entry(index))

    patched = _entry(target)
    patched.content = "[图片] [图片描述：一只橘猫趴在键盘上]"
    assert store.update_entry(patched) is True

    reloaded = BasicMemoryFileStore(tmp_path).read_all("g1")
    assert len(reloaded) == 200
    assert [entry.entry_id for entry in reloaded] == [f"e{i}" for i in range(200)]
    assert reloaded[target].content == "[图片] [图片描述：一只橘猫趴在键盘上]"
    assert reloaded[target - 1].content == _entry(target - 1).content


@pytest.mark.asyncio
async def test_split_reply_pauses_never_add_up_past_the_total_budget(tmp_path, monkeypatch):
    adapter = NapCatAdapter(
        "ws://example.invalid",
        work_path=tmp_path,
        config={
            "allowed_group_ids": ["100"],
            "human_reply_min_delay_seconds": 3.0,
            "human_reply_max_delay_seconds": 7.0,
            "human_reply_total_delay_seconds": 4.0,
        },
    )
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    sent: list[str] = []

    async def fake_send(group_id, text, reply_refs=None):
        sent.append(text)
        return True

    monkeypatch.setattr("asyncio.sleep", fake_sleep)
    adapter._send_group_text_single_locked = fake_send  # type: ignore[method-assign]

    reply = (
        "今天先把完整的报错日志原样贴出来吧，别截图，文字版方便我搜关键字。"
        "然后告诉我你是在哪一步执行之后卡住的，最好把那条命令也一起贴上来。"
        "最后我们再对照官方文档一起排查一遍，看看是不是版本不匹配导致的问题。"
    )
    assert await adapter._send_group_text("100", reply) is True

    assert len(sent) > 2, "这条回复应当被拆成多段"
    assert sum(slept) <= 4.0 + 1e-6
