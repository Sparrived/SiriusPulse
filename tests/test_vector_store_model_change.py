"""「换 embedding 模型后索引过期」的检测与重建。

业务视角：ChromaDB 的 collection 在创建时固定维度并记下模型名。换模型必然换维度，
旧向量与新查询向量不在同一空间，不重建就会得到静默错误的检索结果，因此必须能被
检测出来并整体重建。
"""

from __future__ import annotations

import pytest

from sirius_pulse.memory.diary.vector_store import DiaryVectorStore


def test_vector_store_when_index_model_differs_then_reports_stale(tmp_path):
    """索引由旧模型建立时必须报告过期——这是提醒用户重建索引的依据。"""
    vector_db = tmp_path / "vector_db"
    old = DiaryVectorStore(vector_db, model_name="BAAI/bge-small-zh")
    if not old.available:
        pytest.skip("chromadb 不可用")
    old._get_collection("group_a")

    # 换模型后重新打开同一个库：记录里是旧模型，当前是 bge-m3，维度不同。
    current = DiaryVectorStore(vector_db, model_name="BAAI/bge-m3")

    stats = current.get_stats()

    assert stats["indexed_model"] == "BAAI/bge-small-zh"
    assert stats["stale"] is True


def test_vector_store_when_dropped_and_rebuilt_then_no_longer_stale(tmp_path):
    """重建索引后必须不再报过期，否则提示会一直挂着。"""
    vector_db = tmp_path / "vector_db"
    old = DiaryVectorStore(vector_db, model_name="BAAI/bge-small-zh")
    if not old.available:
        pytest.skip("chromadb 不可用")
    old._get_collection("group_a")

    current = DiaryVectorStore(vector_db, model_name="BAAI/bge-m3")
    current.drop_group("group_a")
    current._get_collection("group_a")

    stats = current.get_stats()

    assert stats["indexed_model"] == "BAAI/bge-m3"
    assert stats["stale"] is False


def test_vector_store_when_index_model_matches_then_not_stale(tmp_path):
    store = DiaryVectorStore(tmp_path / "vector_db", model_name="BAAI/bge-m3")
    if not store.available:
        pytest.skip("chromadb 不可用")
    store._get_collection("group_a")

    stats = store.get_stats()

    assert stats["indexed_model"] == "BAAI/bge-m3"
    assert stats["stale"] is False


def test_vector_store_when_no_collections_then_no_indexed_model(tmp_path):
    store = DiaryVectorStore(tmp_path / "vector_db", model_name="BAAI/bge-m3")
    if not store.available:
        pytest.skip("chromadb 不可用")

    stats = store.get_stats()

    assert stats["indexed_model"] == ""
    assert stats["stale"] is False


def test_vector_store_when_models_are_mixed_then_reports_stale(tmp_path):
    """混合状态（例如重建到一半）必须仍然报过期。

    这里最容易出的错是「拿不到单一模型名 → 当作不过期」，恰好会把最该报警的情况
    吞掉：一半 collection 还是旧维度，检索结果一半不可信。
    """
    vector_db = tmp_path / "vector_db"
    old = DiaryVectorStore(vector_db, model_name="BAAI/bge-small-zh")
    if not old.available:
        pytest.skip("chromadb 不可用")
    old._get_collection("group_old")

    current = DiaryVectorStore(vector_db, model_name="BAAI/bge-m3")
    current._get_collection("group_new")

    stats = current.get_stats()

    assert stats["stale"] is True
    # 提示里要能说出到底是哪个旧模型建的库。
    assert stats["indexed_model"] == "BAAI/bge-small-zh"
    assert stats["indexed_models"] == ["BAAI/bge-m3", "BAAI/bge-small-zh"]
