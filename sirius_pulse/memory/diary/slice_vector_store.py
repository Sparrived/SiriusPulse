"""Chroma-based persistent vector store for diary slice embeddings.

与 DiaryVectorStore 保持一致的架构，使用 ChromaDB 存储切片向量。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from sirius_pulse.memory.diary.slice_models import DiarySlice

logger = logging.getLogger(__name__)

try:
    import chromadb
    from chromadb.config import Settings

    _CHROMA_AVAILABLE = True
except Exception:
    _CHROMA_AVAILABLE = False


class DiarySliceVectorStore:
    """日记切片向量存储（ChromaDB）。

    每个群组一个 collection，与 DiaryVectorStore 架构一致。
    """

    COLLECTION_PREFIX: str = "diary_slice_"

    def __init__(self, persist_dir: Path | str, model_name: str = "") -> None:
        self._persist_dir = Path(persist_dir)
        self._model_name = model_name
        self._client: Any | None = None

        if not _CHROMA_AVAILABLE:
            logger.warning("chromadb 未安装，日记切片向量存储不可用")
            return

        self._persist_dir.mkdir(parents=True, exist_ok=True)
        try:
            self._client = chromadb.PersistentClient(
                path=str(self._persist_dir),
                settings=Settings(anonymized_telemetry=False),
            )
        except Exception as exc:
            logger.warning("ChromaDB 客户端初始化失败: %s", exc)
            self._client = None

    @property
    def available(self) -> bool:
        return self._client is not None

    def _collection_name(self, group_id: str) -> str:
        safe = "".join(c if c.isalnum() or c in "_-" else "_" for c in group_id)
        name = f"{self.COLLECTION_PREFIX}{safe}"
        if len(name) < 3:
            name = name + "_grp"
        if len(name) > 63:
            name = name[:63]
        return name

    def _get_collection(self, group_id: str) -> Any | None:
        if self._client is None:
            return None
        try:
            return self._client.get_or_create_collection(
                name=self._collection_name(group_id),
                metadata={"model": self._model_name},
            )
        except Exception as exc:
            logger.warning("获取 Chroma collection 失败: %s", exc)
            return None

    def add(self, slice: DiarySlice) -> None:
        """添加或更新一个切片。"""
        if self._client is None:
            return
        coll = self._get_collection(slice.group_id)
        if coll is None:
            return

        embedding = slice.embedding
        if not embedding:
            logger.debug("跳过无 embedding 的切片: %s", slice.slice_id)
            return

        try:
            coll.upsert(
                ids=[slice.slice_id],
                embeddings=[embedding],
                documents=[slice.content],
                metadatas=[{
                    "group_id": slice.group_id,
                    "diary_id": slice.diary_id,
                    "summary": slice.summary,
                    "topics": ",".join(slice.topics),
                    "keywords": ",".join(slice.keywords),
                    "triple_subjects": ",".join(slice.triple_subjects),
                    "participants": ",".join(slice.participants),
                    "time_range_start": slice.time_range_start,
                    "time_range_end": slice.time_range_end,
                    "index": slice.index,
                }],
            )
            logger.debug("切片向量已存储: %s", slice.slice_id)
        except Exception as exc:
            logger.warning("切片向量存储失败: %s | %s", slice.slice_id, exc)

    def add_many(self, slices: list[DiarySlice]) -> int:
        """批量添加切片。"""
        if self._client is None or not slices:
            return 0

        by_group: dict[str, list[DiarySlice]] = {}
        for s in slices:
            if not s.embedding:
                continue
            by_group.setdefault(s.group_id, []).append(s)

        total = 0
        for group_id, group_slices in by_group.items():
            coll = self._get_collection(group_id)
            if coll is None:
                continue
            try:
                coll.upsert(
                    ids=[s.slice_id for s in group_slices],
                    embeddings=[s.embedding for s in group_slices],
                    documents=[s.content for s in group_slices],
                    metadatas=[{
                        "group_id": s.group_id,
                        "diary_id": s.diary_id,
                        "summary": s.summary,
                        "topics": ",".join(s.topics),
                        "keywords": ",".join(s.keywords),
                        "triple_subjects": ",".join(s.triple_subjects),
                        "participants": ",".join(s.participants),
                        "time_range_start": s.time_range_start,
                        "time_range_end": s.time_range_end,
                        "index": s.index,
                    } for s in group_slices],
                )
                total += len(group_slices)
            except Exception as exc:
                logger.warning("切片向量批量存储失败 (group=%s): %s", group_id, exc)
        return total

    def search(
        self,
        query_embedding: list[float],
        group_id: str,
        top_k: int = 10,
    ) -> list[tuple[str, float]]:
        """语义检索，返回 (slice_id, score) 列表。"""
        if self._client is None:
            return []
        coll = self._get_collection(group_id)
        if coll is None:
            return []

        try:
            results = coll.query(
                query_embeddings=[query_embedding],
                n_results=min(top_k, max(1, coll.count())),
                include=["distances"],
            )
            ids = results.get("ids", [[]])[0]
            distances = results.get("distances", [[]])[0]
            scored: list[tuple[str, float]] = []
            for sid, dist in zip(ids, distances):
                if dist is None:
                    continue
                score = max(0.0, 1.0 - (float(dist) ** 2) / 2.0)
                scored.append((sid, score))
            return scored
        except Exception as exc:
            logger.warning("切片向量检索失败: %s", exc)
            return []

    def remove(self, group_id: str, slice_ids: list[str]) -> None:
        """删除切片。"""
        if self._client is None or not slice_ids:
            return
        coll = self._get_collection(group_id)
        if coll is None:
            return
        try:
            coll.delete(ids=slice_ids)
        except Exception as exc:
            logger.warning("切片向量删除失败: %s", exc)

    def clear_group(self, group_id: str) -> None:
        """清空某群组的所有切片。"""
        if self._client is None:
            return
        coll = self._get_collection(group_id)
        if coll is None:
            return
        try:
            coll.delete(where={"group_id": group_id})
        except Exception as exc:
            logger.warning("清空切片向量存储失败: %s", exc)

    def count(self, group_id: str) -> int:
        if self._client is None:
            return 0
        coll = self._get_collection(group_id)
        if coll is None:
            return 0
        try:
            return coll.count()
        except Exception:
            return 0

    def get_stats(self) -> dict[str, Any]:
        """返回向量存储统计。"""
        if self._client is None:
            return {"available": False, "total_slices": 0, "groups": [], "model": self._model_name}

        total = 0
        groups: list[dict[str, Any]] = []
        try:
            for coll_name in self._client.list_collections():
                name = coll_name.name if hasattr(coll_name, "name") else str(coll_name)
                if not name.startswith(self.COLLECTION_PREFIX):
                    continue
                try:
                    coll = self._client.get_collection(name)
                    cnt = coll.count()
                    total += cnt
                    gid = name[len(self.COLLECTION_PREFIX):]
                    groups.append({"group_id": gid, "count": cnt})
                except Exception:
                    continue
        except Exception as exc:
            logger.warning("向量存储统计失败: %s", exc)

        return {
            "available": True,
            "total_slices": total,
            "groups": groups,
            "model": self._model_name,
        }
