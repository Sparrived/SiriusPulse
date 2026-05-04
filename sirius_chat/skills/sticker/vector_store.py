"""Chroma-based persistent vector store for sticker embeddings."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from sirius_chat.skills.sticker.models import StickerRecord

logger = logging.getLogger(__name__)

try:
    import chromadb
    from chromadb.config import Settings

    _CHROMA_AVAILABLE = True
except Exception:  # pragma: no cover
    _CHROMA_AVAILABLE = False


class StickerVectorStore:
    """Persistent vector store for stickers using ChromaDB.

    Each persona gets its own collection. Embeddings are stored alongside
    metadata for hybrid retrieval (semantic + keyword).
    """

    COLLECTION_PREFIX: str = "sticker_"
    MODEL_NAME: str = "BAAI/bge-small-zh"

    def __init__(self, persist_dir: Path | str, persona_name: str, model_name: str | None = None) -> None:
        self._persist_dir = Path(persist_dir)
        self._persona_name = persona_name
        self._model_name = model_name or self.MODEL_NAME
        self._client: Any | None = None

        if not _CHROMA_AVAILABLE:
            logger.warning("chromadb 未安装，表情包向量存储不可用")
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

    def _collection_name(self) -> str:
        import hashlib
        # ChromaDB requires names matching [a-zA-Z0-9._-]{3,512}
        # Hash the persona name to ensure ASCII-only and stability.
        hashed = hashlib.md5(self._persona_name.encode("utf-8")).hexdigest()[:12]
        name = f"{self.COLLECTION_PREFIX}{hashed}"
        if len(name) < 3:
            name = name + "_sticker"
        if len(name) > 63:
            name = name[:63]
        return name

    def _get_collection(self) -> Any | None:
        if self._client is None:
            return None
        try:
            return self._client.get_or_create_collection(
                name=self._collection_name(),
                metadata={"model": self._model_name},
            )
        except Exception as exc:
            logger.warning("获取 Chroma collection 失败: %s", exc)
            return None

    def add(self, record: StickerRecord) -> None:
        if self._client is None:
            return
        coll = self._get_collection()
        if coll is None:
            return

        # 核心：使用 usage_context_embedding 进行向量存储
        embedding = record.usage_context_embedding
        if not embedding:
            logger.debug("跳过无 usage_context_embedding 的表情包: %s", record.sticker_id)
            return

        try:
            coll.upsert(
                ids=[record.record_id],
                embeddings=[embedding],
                documents=[record.usage_context],  # 文档存储使用情境（用于关键词检索）
                metadatas=[{
                    "sticker_id": record.sticker_id,
                    "file_path": record.file_path,
                    "caption": record.caption,
                    "usage_context": record.usage_context,
                    "trigger_message": record.trigger_message,
                    "trigger_emotion": record.trigger_emotion,
                    "source_user": record.source_user,
                    "source_group": record.source_group,
                    "discovered_at": record.discovered_at,
                    "last_used_at": record.last_used_at or "",
                    "usage_count": record.usage_count,
                    "tags": ",".join(record.tags),
                    "novelty_score": record.novelty_score,
                }],
            )
            logger.debug("表情包向量已存储: record_id=%s sticker_id=%s", record.record_id, record.sticker_id)
        except Exception as exc:
            logger.warning("表情包向量存储失败: %s | %s", record.record_id, exc)

    def add_many(self, records: list[StickerRecord]) -> int:
        if self._client is None or not records:
            return 0
        coll = self._get_collection()
        if coll is None:
            return 0

        valid = [r for r in records if r.usage_context_embedding]
        if not valid:
            return 0

        try:
            coll.upsert(
                ids=[r.record_id for r in valid],
                embeddings=[r.usage_context_embedding for r in valid],
                documents=[r.usage_context for r in valid],
                metadatas=[{
                    "sticker_id": r.sticker_id,
                    "file_path": r.file_path,
                    "caption": r.caption,
                    "usage_context": r.usage_context,
                    "trigger_message": r.trigger_message,
                    "trigger_emotion": r.trigger_emotion,
                    "source_user": r.source_user,
                    "source_group": r.source_group,
                    "discovered_at": r.discovered_at,
                    "last_used_at": r.last_used_at or "",
                    "usage_count": r.usage_count,
                    "tags": ",".join(r.tags),
                    "novelty_score": r.novelty_score,
                } for r in valid],
            )
            logger.debug("表情包向量批量存储: %d 条", len(valid))
            return len(valid)
        except Exception as exc:
            logger.warning("表情包向量批量存储失败: %s", exc)
            return 0

    def search(
        self,
        query_embedding: list[float],
        top_k: int = 20,
    ) -> list[tuple[str, float]]:
        """Semantic search by usage_context_embedding returning (record_id, score) tuples.

        Score is cosine similarity in [0, 1].
        """
        if self._client is None:
            return []
        coll = self._get_collection()
        if coll is None:
            return []

        try:
            count = coll.count()
            if count == 0:
                return []
            results = coll.query(
                query_embeddings=[query_embedding],
                n_results=min(top_k, max(1, count)),
                include=["distances"],
            )
            ids = results.get("ids", [[]])[0]
            distances = results.get("distances", [[]])[0]
            scored: list[tuple[str, float]] = []
            for rid, dist in zip(ids, distances):
                if dist is None:
                    continue
                score = max(0.0, 1.0 - (float(dist) ** 2) / 2.0)
                scored.append((rid, score))
            return scored
        except Exception as exc:
            logger.warning("表情包向量检索失败: %s", exc)
            return []

    def remove(self, record_ids: list[str]) -> None:
        if self._client is None or not record_ids:
            return
        coll = self._get_collection()
        if coll is None:
            return
        try:
            coll.delete(ids=record_ids)
        except Exception as exc:
            logger.warning("表情包向量删除失败: %s", exc)

    def count(self) -> int:
        if self._client is None:
            return 0
        coll = self._get_collection()
        if coll is None:
            return 0
        try:
            return coll.count()
        except Exception:
            return 0

    def get_all_ids(self) -> list[str]:
        if self._client is None:
            return []
        coll = self._get_collection()
        if coll is None:
            return []
        try:
            result = coll.get(include=[])
            return list(result.get("ids", []))
        except Exception as exc:
            logger.warning("获取表情包 ID 列表失败: %s", exc)
            return []
