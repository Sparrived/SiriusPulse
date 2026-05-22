"""Tests for basic memory manager and heat calculator."""

from __future__ import annotations

import pytest

from sirius_pulse.memory.basic import BasicMemoryManager, HeatCalculator, BasicMemoryEntry


class TestHeatCalculator:
    def test_empty_entries_zero_heat(self) -> None:
        assert HeatCalculator.calculate([]) == 0.0

    def test_single_entry_low_heat(self) -> None:
        from datetime import datetime, timezone, timedelta
        old_ts = (datetime.now(timezone.utc) - timedelta(seconds=400)).isoformat()
        entry = BasicMemoryEntry(
            entry_id="e1", group_id="g1", user_id="alice",
            role="human", content="hello", timestamp=old_ts,
        )
        heat = HeatCalculator.calculate([entry])
        assert 0.0 <= heat <= 1.0
        # Single old entry should be cold
        assert heat < 0.3

    def test_frequent_messages_high_heat(self) -> None:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        entries = []
        for i in range(10):
            ts = now.isoformat()
            entries.append(BasicMemoryEntry(
                entry_id=f"e{i}", group_id="g1", user_id=f"user_{i % 3}",
                role="human", content=f"msg {i}", timestamp=ts,
            ))
        heat = HeatCalculator.calculate(entries)
        assert heat > 0.5

    def test_is_cold_threshold(self) -> None:
        assert HeatCalculator.is_cold(0.1, 400) is True
        assert HeatCalculator.is_cold(0.5, 400) is False
        assert HeatCalculator.is_cold(0.1, 100) is False


class TestBasicMemoryManager:
    def test_add_and_get_context(self) -> None:
        mgr = BasicMemoryManager(hard_limit=30, context_window=5)
        for i in range(10):
            mgr.add_entry("g1", f"u{i}", "human", f"msg {i}")
        ctx = mgr.get_context("g1", n=5)
        assert len(ctx) == 5
        assert ctx[-1].content == "msg 9"

    def test_hard_limit_enforced(self) -> None:
        mgr = BasicMemoryManager(hard_limit=5, context_window=2)
        for i in range(10):
            mgr.add_entry("g1", "u1", "human", f"msg {i}")
        all_entries = mgr.get_all("g1")
        assert len(all_entries) == 5
        assert all_entries[-1].content == "msg 9"

    def test_archive_candidates(self) -> None:
        mgr = BasicMemoryManager(hard_limit=30, context_window=5)
        for i in range(10):
            mgr.add_entry("g1", "u1", "human", f"msg {i}")
        candidates = mgr.get_archive_candidates("g1")
        assert len(candidates) == 5
        assert candidates[0].content == "msg 0"
        assert candidates[-1].content == "msg 4"

    def test_system_prompt_recorded(self) -> None:
        mgr = BasicMemoryManager()
        mgr.add_entry("g1", "assistant", "assistant", "hi", system_prompt="you are kind")
        entry = mgr.get_all("g1")[0]
        assert entry.system_prompt == "you are kind"

    def test_is_cold_after_silence(self) -> None:
        from datetime import datetime, timezone, timedelta
        mgr = BasicMemoryManager()
        old_ts = (datetime.now(timezone.utc) - timedelta(seconds=400)).isoformat()
        mgr.add_entry("g1", "u1", "human", "hello", timestamp=old_ts)
        assert mgr.is_cold("g1") is True

    def test_serialization_roundtrip(self) -> None:
        mgr = BasicMemoryManager()
        mgr.add_entry("g1", "u1", "human", "hello")
        mgr.add_entry("g1", "assistant", "assistant", "hi", system_prompt="sys")
        data = mgr.to_dict()
        restored = BasicMemoryManager.from_dict(data)
        assert len(restored.get_all("g1")) == 2
        assert restored.get_all("g1")[1].system_prompt == "sys"
