"""Chroma-based persistent vector store for diary embeddings."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from sirius_pulse.memory.diary.models import DiaryEntry

logger = logging.getLogger(__name__)

# Optional chromadb
try:
    import chromadb
    from chromadb.config import Settings

    _CHROMA_AVAILABLE = True
except Exception:  # pragma: no cover
    _CHROMA_AVAILABLE = False


class DiaryVectorStore:
    """Persistent vector store for diary entries using ChromaDB.

    Each group gets its own collection. Embeddings are stored alongside
    metadata for hybrid retrieval (semantic + keyword).
    """

    COLLECTION_PREFIX: str = "diary_"
    MODEL_NAME: str = "BAAI/bge-small-zh"

    def __init__(self, persist_dir: Path | str, model_name: str | None = None) -> None:
        self._persist_dir = Path(persist_dir)
        self._model_name = model_name or self.MODEL_NAME
        self._client: Any | None = None
        self._embedding_fn: Any | None = None

        if not _CHROMA_AVAILABLE:
            logger.warning("chromadb 未安装，日记向量存储不可用")
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
        # Chroma collection names must be 3-63 chars, alphanumeric + underscore + hyphen
        safe = "".join(c if c.isalnum() or c in "_-" else "_" for c in group_id)
        name = f"{self.COLLECTION_PREFIX}{safe}"
        # Ensure valid length
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

    def add(self, entry: DiaryEntry) -> None:
        """Add or update a diary entry in the vector store."""
        if self._client is None:
            return
        coll = self._get_collection(entry.group_id)
        if coll is None:
            return

        embedding = entry.embedding
        if not embedding:
            logger.debug("跳过无 embedding 的日记条目: %s", entry.entry_id)
            return

        try:
            # Upsert by entry_id
            coll.upsert(
                ids=[entry.entry_id],
                embeddings=[embedding],
                documents=[entry.content],
                metadatas=[
                    {
                        "group_id": entry.group_id,
                        "summary": entry.summary,
                        "keywords": ",".join(entry.keywords),
                        "created_at": entry.created_at,
                    }
                ],
            )
            logger.debug("日记向量已存储: %s", entry.entry_id)
        except Exception as exc:
            logger.warning("日记向量存储失败: %s | %s", entry.entry_id, exc)

    def remove(self, group_id: str, entry_ids: list[str]) -> None:
        """Remove entries from the vector store."""
        if self._client is None or not entry_ids:
            return
        coll = self._get_collection(group_id)
        if coll is None:
            return
        try:
            coll.delete(ids=entry_ids)
        except Exception as exc:
            logger.warning("日记向量删除失败: %s", exc)

    def search(
        self,
        query_embedding: list[float],
        group_id: str,
        top_k: int = 5,
    ) -> list[tuple[str, float]]:
        """Semantic search returning (entry_id, score) tuples.

        Score is cosine similarity in [0, 1].
        """
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
            # Chroma returns L2 distance; convert to similarity score
            scored: list[tuple[str, float]] = []
            for eid, dist in zip(ids, distances):
                if dist is None:
                    continue
                # L2 distance to cosine similarity approximation
                # cos_sim = 1 - (l2^2)/2 for unit vectors
                score = max(0.0, 1.0 - (float(dist) ** 2) / 2.0)
                scored.append((eid, score))
            return scored
        except Exception as exc:
            logger.warning("日记向量检索失败: %s", exc)
            return []

    def add_many(self, entries: list[DiaryEntry]) -> int:
        """Bulk-add entries to the vector store.

        Returns number of entries actually stored.
        """
        if self._client is None or not entries:
            return 0

        # Group by group_id
        by_group: dict[str, list[DiaryEntry]] = {}
        for entry in entries:
            if not entry.embedding:
                continue
            by_group.setdefault(entry.group_id, []).append(entry)

        total = 0
        for group_id, group_entries in by_group.items():
            coll = self._get_collection(group_id)
            if coll is None:
                continue
            try:
                coll.upsert(
                    ids=[e.entry_id for e in group_entries],
                    embeddings=[e.embedding for e in group_entries],
                    documents=[e.content for e in group_entries],
                    metadatas=[
                        {
                            "group_id": e.group_id,
                            "summary": e.summary,
                            "keywords": ",".join(e.keywords),
                            "created_at": e.created_at,
                        }
                        for e in group_entries
                    ],
                )
                total += len(group_entries)
            except Exception as exc:
                logger.warning("日记向量批量存储失败 (group=%s): %s", group_id, exc)
        return total

    def clear_group(self, group_id: str) -> None:
        """Remove all entries for a group."""
        if self._client is None:
            return
        coll = self._get_collection(group_id)
        if coll is None:
            return
        try:
            coll.delete(where={"group_id": group_id})
        except Exception as exc:
            logger.warning("清空日记向量存储失败: %s", exc)

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
        """Return vector store statistics.

        Returns dict with:
        - available: bool
        - total_entries: int
        - groups: list of {group_id, count}
        - model: str
        """
        if self._client is None:
            return {"available": False, "total_entries": 0, "groups": [], "model": self._model_name}

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
                    # Derive group_id from collection name
                    gid = name[len(self.COLLECTION_PREFIX) :]
                    groups.append({"group_id": gid, "count": cnt})
                except Exception:
                    continue
        except Exception as exc:
            logger.warning("向量存储统计失败: %s", exc)

        return {
            "available": True,
            "total_entries": total,
            "groups": groups,
            "model": self._model_name,
        }
