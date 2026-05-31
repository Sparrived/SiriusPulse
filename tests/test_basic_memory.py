"""基础记忆（BasicMemoryManager）关键路径测试。"""
from __future__ import annotations

from sirius_pulse.memory.basic import BasicMemoryManager


def test_add_entry():
    """添加记忆条目。"""
    mgr = BasicMemoryManager(hard_limit=30, context_window=5)
    mgr.add_entry("g1", "u1", "user", "hello", speaker_name="Alice")
    ctx = mgr.get_context("g1")
    assert len(ctx) == 1
    assert ctx[0].content == "hello"
    assert ctx[0].speaker_name == "Alice"


def test_context_window():
    """上下文窗口限制。"""
    mgr = BasicMemoryManager(hard_limit=30, context_window=3)
    for i in range(10):
        mgr.add_entry("g1", "u1", "user", f"msg_{i}", speaker_name="A")

    ctx = mgr.get_context("g1")
    assert len(ctx) == 3  # context_window=3
    assert ctx[-1].content == "msg_9"  # 最新消息


def test_hard_limit():
    """无硬限制，所有消息均保留。"""
    mgr = BasicMemoryManager(hard_limit=5, context_window=3)
    for i in range(10):
        mgr.add_entry("g1", "u1", "user", f"msg_{i}", speaker_name="A")

    all_msgs = mgr.get_all("g1")
    assert len(all_msgs) == 10  # 无 maxlen 限制，全部保留
    assert all_msgs[0].content == "msg_0"
    assert all_msgs[-1].content == "msg_9"


def test_archive_candidates():
    """归档候选条目。"""
    mgr = BasicMemoryManager(hard_limit=10, context_window=3)
    for i in range(8):
        mgr.add_entry("g1", "u1", "user", f"msg_{i}", speaker_name="A")

    candidates = mgr.get_archive_candidates("g1")
    assert len(candidates) == 5  # 8 - 3 = 5 条超出上下文窗口


def test_multiple_groups():
    """多群独立性。"""
    mgr = BasicMemoryManager()
    mgr.add_entry("g1", "u1", "user", "g1_msg", speaker_name="A")
    mgr.add_entry("g2", "u2", "user", "g2_msg", speaker_name="B")

    assert len(mgr.get_context("g1")) == 1
    assert len(mgr.get_context("g2")) == 1
    assert mgr.get_context("g1")[0].content == "g1_msg"
    assert mgr.get_context("g2")[0].content == "g2_msg"


def test_heat_calculation():
    """热度计算。"""
    mgr = BasicMemoryManager()
    # 空群热度为 0
    heat = mgr.compute_heat("g1")
    assert 0.0 <= heat <= 1.0

    # 添加消息后热度上升
    import time
    for i in range(5):
        mgr.add_entry("g1", f"u{i}", "user", f"msg_{i}", speaker_name=f"U{i}")

    heat = mgr.compute_heat("g1")
    assert heat > 0.0


def test_serialization():
    """序列化与反序列化。"""
    mgr = BasicMemoryManager()
    mgr.add_entry("g1", "u1", "user", "hello", speaker_name="Alice")
    mgr.add_entry("g1", "u2", "user", "world", speaker_name="Bob")

    data = mgr.to_dict()
    restored = BasicMemoryManager.from_dict(data)

    restored_ctx = restored.get_context("g1")
    assert len(restored_ctx) == 2
    assert restored_ctx[0].content == "hello"
    assert restored_ctx[1].content == "world"


def test_entries_by_user():
    """跨群查询用户发言。"""
    mgr = BasicMemoryManager()
    mgr.add_entry("g1", "u1", "user", "g1_msg", speaker_name="A")
    mgr.add_entry("g2", "u1", "user", "g2_msg", speaker_name="A")
    mgr.add_entry("g2", "u2", "user", "other", speaker_name="B")

    entries = mgr.get_entries_by_user("u1", n=10)
    assert len(entries) == 2
