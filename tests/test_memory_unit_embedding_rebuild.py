"""记忆单元向量的维度失配处理。

业务视角：记忆单元的向量内联存在 ``memory_units/*.json`` 里。换 embedding 模型后
维度会变（``bge-small-zh`` 512 维、``bge-m3`` 1024 维）。旧向量若被当作有效向量参与
比较，语义检索会静默给出错误结果——这正是必须显式处理的地方。
"""

from __future__ import annotations

import pytest

from sirius_pulse.memory.units.indexer import MemoryUnitIndexer
from sirius_pulse.memory.units.manager import rebuild_memory_unit_embeddings
from sirius_pulse.memory.units.models import MemoryUnit
from sirius_pulse.memory.units.store import MemoryUnitFileStore


class _StubEmbedding:
    """固定维度的假 embedding 客户端，按文本内容给一个确定性的向量。"""

    def __init__(self, dimension: int, *, model: str = "BAAI/bge-m3") -> None:
        self._dimension = dimension
        self.model = model
        self.available = True
        self.calls: list[list[str]] = []

    @property
    def dimension(self) -> int:
        return self._dimension

    def encode(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [[float(len(text) % 7 + 1)] * self._dimension for text in texts]

    def encode_single(self, text: str) -> list[float]:
        return self.encode([text])[0]


def _unit(unit_id: str, *, embedding: list[float] | None) -> MemoryUnit:
    return MemoryUnit(
        unit_id=unit_id,
        group_id="group_a",
        created_at="2026-01-01T00:00:00+00:00",
        summary=f"summary {unit_id}",
        embedding=embedding,
    )


def test_cosine_sim_when_dimensions_differ_then_not_similar():
    """维度不同必须判为不相似，而不是按短的那条截断算出一个看似合理的分数。"""
    indexer = MemoryUnitIndexer(embedding_client=None)

    assert indexer._cosine_sim([1.0] * 512, [1.0] * 1024) == 0.0


def test_cosine_sim_when_dimensions_match_then_scores_normally():
    indexer = MemoryUnitIndexer(embedding_client=None)

    assert indexer._cosine_sim([1.0, 0.0], [1.0, 0.0]) == pytest.approx(1.0)


def test_ensure_model_current_when_dimensions_stale_then_recomputes_all():
    client = _StubEmbedding(dimension=1024)
    indexer = MemoryUnitIndexer(embedding_client=client)
    units = [_unit("u1", embedding=[0.5] * 512), _unit("u2", embedding=[0.5] * 512)]

    changed = indexer.ensure_model_current(units)

    assert changed is True
    assert all(len(unit.embedding or []) == 1024 for unit in units)


def test_ensure_model_current_when_already_current_then_leaves_untouched():
    client = _StubEmbedding(dimension=1024)
    indexer = MemoryUnitIndexer(embedding_client=client)
    original = [0.25] * 1024
    units = [_unit("u1", embedding=list(original))]

    changed = indexer.ensure_model_current(units)

    assert changed is False
    assert units[0].embedding == original
    assert client.calls == []


def test_ensure_model_current_when_embedding_missing_then_fills_it():
    client = _StubEmbedding(dimension=1024)
    indexer = MemoryUnitIndexer(embedding_client=client)
    units = [_unit("u1", embedding=None)]

    changed = indexer.ensure_model_current(units)

    assert changed is True
    assert len(units[0].embedding or []) == 1024


def test_ensure_model_current_when_unavailable_then_no_op():
    indexer = MemoryUnitIndexer(embedding_client=None)
    units = [_unit("u1", embedding=[0.5] * 512)]

    assert indexer.ensure_model_current(units) is False
    assert len(units[0].embedding or []) == 512


def test_rebuild_memory_unit_embeddings_when_dimensions_stale_then_persists_new_vectors(
    tmp_path,
):
    """重建必须落盘：只在内存里改，重启后又回到旧维度。"""
    store = MemoryUnitFileStore(tmp_path)
    store.save("group_a", [_unit("u1", embedding=[0.5] * 512)])
    client = _StubEmbedding(dimension=1024)

    total, failed = rebuild_memory_unit_embeddings(client, store)

    assert (total, failed) == (1, 0)
    reloaded = store.load("group_a")
    assert len(reloaded[0].embedding or []) == 1024


def test_rebuild_memory_unit_embeddings_when_nothing_stale_then_skips_write(tmp_path, monkeypatch):
    store = MemoryUnitFileStore(tmp_path)
    store.save("group_a", [_unit("u1", embedding=[0.5] * 1024)])
    client = _StubEmbedding(dimension=1024)

    writes: list[str] = []
    monkeypatch.setattr(store, "save", lambda group, units: writes.append(group))

    total, failed = rebuild_memory_unit_embeddings(client, store)

    assert (total, failed) == (0, 0)
    assert writes == []


def test_rebuild_memory_unit_embeddings_when_batch_encode_fails_then_reports_failure(
    tmp_path,
):
    """整批编码失败必须计入失败数。

    线上 64 条一批偶发超时，接口原本照样返回成功，用户以为索引已修好，实际那批还留在
    旧维度。失败的条数要如实上报，界面才能提示重试。
    """
    store = MemoryUnitFileStore(tmp_path)
    store.save(
        "group_a",
        [_unit("u1", embedding=[0.5] * 512), _unit("u2", embedding=[0.5] * 512)],
    )

    class _Failing:
        model = "BAAI/bge-m3"
        dimension = 1024
        available = True

        def encode(self, texts):
            raise RuntimeError("Embedding 服务请求失败: timed out")

    total, failed = rebuild_memory_unit_embeddings(_Failing(), store)

    assert total == 0
    assert failed == 2


def test_rebuild_memory_unit_embeddings_when_one_unit_fails_then_retries_rest(
    tmp_path,
):
    """一批里个别条目失败时二分重试救回其余条目，只报真正失败的那条。"""
    store = MemoryUnitFileStore(tmp_path)
    store.save(
        "group_a",
        [_unit("u%d" % i, embedding=[0.5] * 512) for i in range(4)],
    )

    class _Partial:
        model = "BAAI/bge-m3"
        dimension = 1024
        available = True

        def encode(self, texts):
            # 批量请求一律超时（线上就是这样），单条时可定位到具体是哪一条坏的。
            if len(texts) > 1:
                raise RuntimeError("timed out")
            if "summary u0" in texts[0]:
                raise RuntimeError("timed out")
            return [[0.25] * 1024 for _ in texts]

    total, failed = rebuild_memory_unit_embeddings(_Partial(), store)

    assert total == 4
    assert failed == 1
    reloaded = {u.unit_id: len(u.embedding or []) for u in store.load("group_a")}
    assert reloaded["u0"] == 512
    assert reloaded["u1"] == 1024
    assert reloaded["u3"] == 1024


def test_rebuild_memory_unit_embeddings_when_no_client_then_zero(tmp_path):
    store = MemoryUnitFileStore(tmp_path)
    store.save("group_a", [_unit("u1", embedding=[0.5] * 512)])

    assert rebuild_memory_unit_embeddings(None, store) == (0, 0)
