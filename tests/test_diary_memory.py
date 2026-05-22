"""Tests for diary memory generator, indexer, and manager."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from sirius_pulse.memory.diary import (
    DiaryEntry,
    DiaryGenerationResult,
    DiaryGenerator,
    DiaryIndexer,
    DiaryRetriever,
    DiaryManager,
)
from sirius_pulse.memory.basic import BasicMemoryEntry


class TestDiaryGenerator:
    @pytest.mark.asyncio
    async def test_generate_success(self) -> None:
        gen = DiaryGenerator()
        mock_provider = AsyncMock()
        mock_provider.generate_async.return_value = (
            '{"content": "大家聊了很多有趣的话题", '
            '"keywords": ["闲聊", "兴趣"], '
            '"summary": "群聊日常", '
            '"dominant_topic": "闲聊", '
            '"interest_topics": ["游戏", "音乐"]}'
        )
        candidates = [
            BasicMemoryEntry("b1", "g1", "alice", "human", "你好", "2026-04-22T10:00:00+00:00"),
            BasicMemoryEntry("b2", "g1", "bob", "human", "你好呀", "2026-04-22T10:01:00+00:00"),
        ]
        result = await gen.generate(
            group_id="g1",
            candidates=candidates,
            persona_name="小星",
            persona_description="一个温柔的AI助手",
            provider_async=mock_provider,
            model_name="gpt-4o-mini",
        )
        assert result is not None
        assert isinstance(result, DiaryGenerationResult)
        entry = result.entry
        assert entry.group_id == "g1"
        assert "大家聊了很多有趣的话题" in entry.content
        assert entry.source_ids == ["b1", "b2"]
        assert len(entry.keywords) == 2
        assert result.dominant_topic == "闲聊"
        assert result.interest_topics == ["游戏", "音乐"]

    @pytest.mark.asyncio
    async def test_generate_empty_candidates(self) -> None:
        gen = DiaryGenerator()
        result = await gen.generate(
            group_id="g1",
            candidates=[],
            persona_name="小星",
            persona_description="",
            provider_async=AsyncMock(),
            model_name="gpt-4o-mini",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_generate_llm_failure(self) -> None:
        gen = DiaryGenerator()
        mock_provider = AsyncMock()
        mock_provider.generate_async.side_effect = RuntimeError("timeout")
        candidates = [
            BasicMemoryEntry("b1", "g1", "alice", "human", "hello", "2026-04-22T10:00:00+00:00"),
        ]
        result = await gen.generate(
            group_id="g1",
            candidates=candidates,
            persona_name="小星",
            persona_description="",
            provider_async=mock_provider,
            model_name="gpt-4o-mini",
        )
        assert result is None

    @pytest.mark.asyncio
    async def test_generate_retry_on_invalid_json(self) -> None:
        gen = DiaryGenerator()
        mock_provider = AsyncMock()
        mock_provider.generate_async.side_effect = [
            "这不是 JSON",
            '{"content": "重试成功", "keywords": ["k"], "summary": "摘要", '
            '"dominant_topic": "话题", "interest_topics": ["t1"]}',
        ]
        candidates = [
            BasicMemoryEntry("b1", "g1", "alice", "human", "hello", "2026-04-22T10:00:00+00:00"),
        ]
        result = await gen.generate(
            group_id="g1",
            candidates=candidates,
            persona_name="小星",
            persona_description="",
            provider_async=mock_provider,
            model_name="gpt-4o-mini",
            max_retries=2,
        )
        assert result is not None
        assert result.entry.content == "重试成功"
        assert mock_provider.generate_async.await_count == 2
        # 第二次请求的系统提示应包含重试提醒
        second_call = mock_provider.generate_async.await_args_list[1]
        request = second_call[0][0]
        assert "重要提醒" in request.system_prompt

    @pytest.mark.asyncio
    async def test_generate_retry_exhausted(self) -> None:
        gen = DiaryGenerator()
        mock_provider = AsyncMock()
        mock_provider.generate_async.return_value = "永远不是 JSON"
        candidates = [
            BasicMemoryEntry("b1", "g1", "alice", "human", "hello", "2026-04-22T10:00:00+00:00"),
        ]
        result = await gen.generate(
            group_id="g1",
            candidates=candidates,
            persona_name="小星",
            persona_description="",
            provider_async=mock_provider,
            model_name="gpt-4o-mini",
            max_retries=1,
        )
        assert result is None
        assert mock_provider.generate_async.await_count == 2


class TestDiaryIndexer:
    def test_keyword_search(self) -> None:
        idx = DiaryIndexer(enable_semantic=False)
        idx.add(DiaryEntry("d1", "g1", "2026-04-22T10:00:00+00:00", content="今天讨论了Python"))
        idx.add(DiaryEntry("d2", "g1", "2026-04-22T10:00:00+00:00", content="天气很好"))
        results = idx.search("Python", top_k=5)
        assert len(results) == 1
        assert results[0][0].entry_id == "d1"
        assert results[0][1] > 0

    def test_keyword_search_with_keywords_field(self) -> None:
        idx = DiaryIndexer(enable_semantic=False)
        idx.add(DiaryEntry(
            "d1", "g1", "2026-04-22T10:00:00+00:00",
            content="内容", keywords=["编程", "Python"]
        ))
        results = idx.search("编程", top_k=5)
        assert len(results) == 1

    def test_empty_search(self) -> None:
        idx = DiaryIndexer(enable_semantic=False)
        assert idx.search("anything") == []

    def test_cosine_sim(self) -> None:
        a = [1.0, 0.0]
        b = [1.0, 0.0]
        c = [0.0, 1.0]
        assert DiaryIndexer._cosine_sim(a, b) == pytest.approx(1.0)
        assert DiaryIndexer._cosine_sim(a, c) == pytest.approx(0.0)


class TestDiaryRetriever:
    def test_token_budget(self) -> None:
        idx = DiaryIndexer()
        for i in range(10):
            idx.add(DiaryEntry(
                f"d{i}", "g1", "2026-04-22T10:00:00+00:00",
                content="这是一段非常长的日记内容" * 3,  # ~90 chars
                summary=f"摘要{i}",
            ))
        retriever = DiaryRetriever(idx)
        results = retriever.retrieve("日记", top_k=10, max_tokens_budget=200)
        # 200 tokens * 1.5 ≈ 300 chars budget
        # Each entry ~48 chars, so should fit ~6 entries
        assert 1 <= len(results) <= 7


class TestDiaryManager:
    def test_is_source_diarized(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            mgr = DiaryManager(td)
            mgr._diarized_sources["g1"] = {"b1", "b2"}
            assert mgr.is_source_diarized("g1", "b1") is True
            assert mgr.is_source_diarized("g1", "b3") is False

    def test_retrieve_empty(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            mgr = DiaryManager(td)
            assert mgr.retrieve("hello") == []

    def test_add_and_load(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            mgr = DiaryManager(td)
            entry = DiaryEntry(
                "d1", "g1", datetime.now(timezone.utc).isoformat(),
                content="测试日记", summary="测试",
            )
            mgr.add_entry("g1", entry)
            loaded = mgr._store.load("g1")
            assert len(loaded) == 1
            assert loaded[0].content == "测试日记"

    @pytest.mark.asyncio
    async def test_generate_from_candidates_min_threshold(self) -> None:
        """只有候选消息不足 12 条时不应生成日记。"""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            mgr = DiaryManager(td)
            mock_provider = AsyncMock()
            candidates = [
                BasicMemoryEntry(f"b{i}", "g1", "alice", "human", f"msg{i}", "2026-04-22T10:00:00+00:00")
                for i in range(8)
            ]
            result = await mgr.generate_from_candidates(
                group_id="g1",
                candidates=candidates,
                persona_name="小星",
                persona_description="",
                provider_async=mock_provider,
                model_name="gpt-4o-mini",
            )
            assert result is None
            mock_provider.generate_async.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_generate_from_candidates_with_overlap(self) -> None:
        """生成日记时应带上前次末尾 3 条消息作为重叠。"""
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            mgr = DiaryManager(td)
            mock_provider = AsyncMock()
            mock_provider.generate_async.return_value = (
                '{"content": "日记内容", '
                '"keywords": ["k1"], '
                '"summary": "摘要", '
                '"dominant_topic": "话题", '
                '"interest_topics": ["t1"]}'
            )
            # 第一次用 12 条生成
            first_candidates = [
                BasicMemoryEntry(f"b{i:02d}", "g1", "alice", "human", f"msg{i}", "2026-04-22T10:00:00+00:00")
                for i in range(12)
            ]
            result1 = await mgr.generate_from_candidates(
                group_id="g1",
                candidates=first_candidates,
                persona_name="小星",
                persona_description="",
                provider_async=mock_provider,
                model_name="gpt-4o-mini",
            )
            assert result1 is not None
            # 模拟第二次生成，前 3 条 source_ids 应该被自动 prepend
            # 重叠的 source_ids 是 b09, b10, b11，需要包含在 second_candidates 里
            # 这样 manager 才能找到它们并 prepend
            second_candidates = [
                BasicMemoryEntry(f"b{i:02d}", "g1", "alice", "human", f"msg{i}", "2026-04-22T10:00:00+00:00")
                for i in range(9, 24)
            ]
            result2 = await mgr.generate_from_candidates(
                group_id="g1",
                candidates=second_candidates,
                persona_name="小星",
                persona_description="",
                provider_async=mock_provider,
                model_name="gpt-4o-mini",
            )
            assert result2 is not None
            # 检查第二次调用时 candidates 包含了前次末尾 3 条
            call_args = mock_provider.generate_async.await_args_list[-1]
            request = call_args[0][0]
            # 请求中的 messages[0].content 应该包含重叠的 b09, b10, b11
            prompt = request.messages[0]["content"]
            # prompt 里显示的是 msg9 等而不是 b09，因为 prompt 由 speaker_name + content 构成
            assert "msg9" in prompt
            assert "msg10" in prompt
            assert "msg11" in prompt
            # 同时确认重叠消息出现在新消息之前（prepend 逻辑）
            assert prompt.index("msg9") < prompt.index("msg12")
            assert prompt.index("msg10") < prompt.index("msg12")
            assert prompt.index("msg11") < prompt.index("msg12")
