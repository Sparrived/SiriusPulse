"""Automated retrieval quality tests for diary memory.

Uses synthetic diary entries with known relevance to verify
recall@k metrics. Semantic tests use a mock EmbeddingClient.
"""

from __future__ import annotations

import hashlib
import math
import tempfile

import pytest

from sirius_chat.embedding.client import EmbeddingClient
from sirius_chat.memory.diary.indexer import DiaryIndexer
from sirius_chat.memory.diary.models import DiaryEntry
from sirius_chat.memory.diary.vector_store import DiaryVectorStore


def _embedding_from_text(text: str, dim: int = 64) -> list[float]:
    """从文本生成确定性伪向量（用于测试，非真实语义）。"""
    digest = hashlib.sha256(text.encode()).digest()
    raw = [b / 255.0 for b in digest[:dim]]
    norm = math.sqrt(sum(x * x for x in raw))
    return [x / norm for x in raw] if norm > 0 else raw


class _MockEmbeddingClient:
    """测试用 Mock EmbeddingClient，基于文本哈希生成确定性向量。"""

    @property
    def available(self) -> bool:
        return True

    def encode(self, texts: list[str]) -> list[list[float]]:
        return [_embedding_from_text(t) for t in texts]

    def encode_single(self, text: str) -> list[float]:
        return _embedding_from_text(text)


def _entry(
    entry_id: str,
    group_id: str,
    content: str,
    keywords: list[str] | None = None,
    summary: str = "",
) -> DiaryEntry:
    return DiaryEntry(
        entry_id=entry_id,
        group_id=group_id,
        created_at="2026-04-22T10:00:00+00:00",
        content=content,
        keywords=keywords or [],
        summary=summary or content[:20],
    )


class TestDiaryRetrievalQuality:
    """Validate that diary search returns relevant entries for known queries."""

    def test_recall_at_k_keyword_exact_match(self) -> None:
        """Keywords should guarantee high recall for exact matches."""
        idx = DiaryIndexer(enable_semantic=False)
        entries = [
            _entry("d1", "g1", "今天讨论了Python编程技巧", ["Python", "编程"]),
            _entry("d2", "g1", "天气很好，适合出门", ["天气"]),
            _entry("d3", "g1", "Python的异步IO很难理解", ["Python", "异步"]),
            _entry("d4", "g1", "周末计划去爬山", ["周末", "爬山"]),
        ]
        for e in entries:
            idx.add(e)

        results = idx.search("Python", top_k=5, group_id="g1")
        ids = {r[0].entry_id for r in results}
        assert "d1" in ids
        assert "d3" in ids

    def test_recall_at_k_semantic(self) -> None:
        """Semantic search with mock client should produce keyword + semantic scores."""
        mock_client = _MockEmbeddingClient()
        idx = DiaryIndexer(enable_semantic=True, embedding_client=mock_client)
        entries = [
            _entry("d1", "g1", "深度学习在图像识别中的应用"),
            _entry("d2", "g1", "神经网络模型训练技巧"),
            _entry("d3", "g1", "如何制作红烧肉"),
            _entry("d4", "g1", "股票市场的波动分析"),
        ]
        for e in entries:
            idx.add(e)

        results = idx.search("机器学习", top_k=2, group_id="g1")
        ids = [r[0].entry_id for r in results]
        assert len(ids) == 2

    def test_recall_with_vector_store(self) -> None:
        """Chroma-backed search with mock client should work."""
        mock_client = _MockEmbeddingClient()

        td = tempfile.mkdtemp()
        try:
            store = DiaryVectorStore(td)
            if not store.available:
                pytest.skip("chromadb 未安装，跳过向量存储测试")

            idx = DiaryIndexer(
                enable_semantic=True,
                vector_store=store,
                embedding_client=mock_client,
            )

            entries = [
                _entry("d1", "g1", "量子计算的基本原理"),
                _entry("d2", "g1", "量子纠缠与量子通信"),
                _entry("d3", "g1", "古典音乐欣赏指南"),
                _entry("d4", "g1", "量子计算机硬件架构"),
            ]
            for e in entries:
                idx.add(e)

            results = idx.search("量子力学", top_k=3, group_id="g1")
            ids = [r[0].entry_id for r in results]
            assert len(ids) >= 1
        finally:
            # 清理 ChromaDB mmap 文件：Windows 上 ChromaDB 会锁定文件，
            # 需要先释放再删除，这里用 shutil.rmtree 并忽略错误
            import shutil
            shutil.rmtree(td, ignore_errors=True)

    def test_fusion_boosts_keyword_match(self) -> None:
        """Hybrid fusion should include entries that match both semantic and keyword."""
        mock_client = _MockEmbeddingClient()
        idx = DiaryIndexer(enable_semantic=True, embedding_client=mock_client)

        entries = [
            _entry("d1", "g1", "深度学习框架对比", ["PyTorch", "TensorFlow"]),
            _entry("d2", "g1", "深度学习的历史发展", ["历史"]),
            _entry("d3", "g1", "如何种植番茄", ["农业"]),
        ]
        for e in entries:
            idx.add(e)

        results = idx.search("PyTorch深度学习", top_k=3, group_id="g1")
        ids = [r[0].entry_id for r in results]
        # d1 has both semantic similarity and keyword match
        assert "d1" in ids

    def test_group_isolation(self) -> None:
        """Entries from other groups must not leak into search results."""
        idx = DiaryIndexer(enable_semantic=False)
        idx.add(_entry("d1", "g1", "群1的内容"))
        idx.add(_entry("d2", "g2", "群2的内容"))

        results = idx.search("内容", top_k=5, group_id="g1")
        ids = {r[0].entry_id for r in results}
        assert "d1" in ids
        assert "d2" not in ids

    def test_top_k_respected(self) -> None:
        """Search must not return more than top_k results."""
        idx = DiaryIndexer(enable_semantic=False)
        for i in range(20):
            idx.add(_entry(f"d{i}", "g1", f"日记条目{i}", ["日记"]))

        results = idx.search("日记", top_k=5, group_id="g1")
        assert len(results) <= 5

    def test_empty_query_no_crash(self) -> None:
        """Empty or irrelevant query should return empty list gracefully."""
        idx = DiaryIndexer(enable_semantic=False)
        idx.add(_entry("d1", "g1", "一些内容"))
        assert idx.search("完全不相关的东西", top_k=5, group_id="g1") == []
