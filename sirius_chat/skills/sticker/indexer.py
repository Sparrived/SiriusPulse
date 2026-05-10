"""表情包语义索引：检索 + 多维度加权随机选择。

Embedding 是强依赖：所有语义计算均通过 EmbeddingClient 调用远程微服务，
不再内置 SentenceTransformer 本地模型加载与 fallback 逻辑。
"""

from __future__ import annotations

import logging
import math
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sirius_chat.embedding.client import EmbeddingClient
from sirius_chat.skills.sticker.models import StickerPreference, StickerRecord
from sirius_chat.skills.sticker.vector_store import StickerVectorStore

logger = logging.getLogger(__name__)


class StickerIndexer:
    """表情包语义索引，支持多维度加权检索。

    所有 embedding 计算均通过 EmbeddingClient 调用远程微服务。
    ``enable_semantic=False`` 时仅使用关键词检索（供单元测试使用）。
    """

    def __init__(
        self,
        work_path: Path | str,
        persona_name: str,
        enable_semantic: bool = True,
        embedding_client: EmbeddingClient | None = None,
    ) -> None:
        self._work_path = Path(work_path)
        self._persona_name = persona_name
        self._enable_semantic = enable_semantic
        self._embedding_client = embedding_client

        self._records: dict[str, StickerRecord] = {}
        self._tag_embedding_cache: dict[str, list[float]] = {}
        self._vector_store = StickerVectorStore(
            persist_dir=self._work_path / "vector_store",
            persona_name=persona_name,
        )

        if not enable_semantic:
            logger.debug("表情包语义索引已禁用")
        elif embedding_client is None:
            logger.warning("未提供 EmbeddingClient，表情包语义检索不可用")

    @property
    def semantic_available(self) -> bool:
        """语义能力是否可用（需要 enable_semantic 且 EmbeddingClient 可用）。"""
        if not self._enable_semantic:
            return False
        return self._embedding_client is not None and self._embedding_client.available

    def encode_texts(self, texts: list[str]) -> list[list[float]]:
        """通过远程 Embedding 服务编码文本列表。

        Raises:
            RuntimeError: 无 EmbeddingClient 或服务调用失败。
        """
        if self._embedding_client is None:
            raise RuntimeError("EmbeddingClient 未初始化，无法计算 embedding")
        return self._embedding_client.encode(texts)

    def encode_single(self, text: str) -> list[float]:
        """编码单条文本，返回嵌入向量。"""
        return self._embedding_client.encode_single(text) if self._embedding_client else []

    def add(self, record: StickerRecord) -> bool:
        """添加表情包到索引，自动计算 embedding。

        当 semantic_available 为 True 时，自动计算
        usage_context_embedding / caption_embedding / scene_summary_embedding。
        """
        logger.debug(
            "表情包索引添加: record_id=%s sticker_id=%s file=%s",
            record.record_id, record.sticker_id, record.file_path,
        )
        recomputed = False
        if self.semantic_available:
            if not record.usage_context_embedding:
                vec = self.encode_single(record.usage_context)
                if vec:
                    record.usage_context_embedding = vec
                    recomputed = True
                    logger.debug(
                        "表情包索引: record_id=%s usage_context_embedding 计算完成",
                        record.record_id,
                    )

            if not record.caption_embedding and record.caption:
                cap_vec = self.encode_single(record.caption)
                if cap_vec:
                    record.caption_embedding = cap_vec

            if record.scene_summary and not record.scene_summary_embedding:
                scene_vec = self.encode_single(record.scene_summary)
                if scene_vec:
                    record.scene_summary_embedding = scene_vec

        if self._vector_store.available:
            self._vector_store.add(record)
            logger.debug("表情包索引: record_id=%s 已存入向量存储", record.record_id)

        self._records[record.record_id] = record
        logger.info(
            "表情包索引添加完成: record_id=%s 总记录数=%d",
            record.record_id, len(self._records),
        )
        return recomputed

    def search(
        self,
        current_context: str,
        preference: StickerPreference,
        emotion_hint: str = "neutral",
        top_k: int = 20,
        similarity_threshold: float = 0.6,
        scene_query: str = "",
    ) -> StickerRecord | None:
        """按当前情境检索最匹配的表情包，返回加权随机选择的结果。

        支持双路检索：
        - Query A（current_context）: 匹配 usage_context_embedding（精确情境）
        - Query B（scene_query）: 匹配 scene_summary_embedding（场景概括）
        """
        if not self._records:
            logger.debug("表情包库为空")
            return None

        # 1. 语义检索：Query A → usage_context
        context_scores: dict[str, float] = {}
        if self.semantic_available:
            if self._vector_store.available:
                try:
                    query_vec = self.encode_single(current_context)
                    if query_vec:
                        for rid, score in self._vector_store.search(
                            query_vec, top_k=top_k * 2
                        ):
                            context_scores[rid] = score
                except Exception as exc:
                    logger.warning("向量存储检索失败，回退到内存检索: %s", exc)
                    context_scores = self._semantic_search(current_context, top_k * 2)
            else:
                context_scores = self._semantic_search(current_context, top_k * 2)

        # 2. 语义检索：Query B → scene_summary
        scene_scores: dict[str, float] = {}
        if scene_query and self.semantic_available:
            scene_scores = self._scene_search(scene_query, top_k * 2)

        # 3. 关键词检索
        keyword_scores = self._keyword_search(current_context, top_k * 2)

        # 4. 合并候选（按 record_id）
        candidate_rids = set(context_scores) | set(scene_scores) | set(keyword_scores)
        candidates: dict[str, StickerRecord] = {}
        for rid in candidate_rids:
            if rid in self._records:
                candidates[rid] = self._records[rid]

        if not candidates:
            return None

        # 5. 多维度评分
        scored: list[tuple[StickerRecord, float]] = []
        for rid, record in candidates.items():
            score = self._score_sticker(
                record,
                context_scores.get(rid, 0.0),
                keyword_scores.get(rid, 0.0),
                preference,
                emotion_hint,
                scene_scores.get(rid, 0.0),
            )
            if score >= similarity_threshold:
                scored.append((record, score))

        if not scored:
            return None

        scored.sort(key=lambda x: x[1], reverse=True)

        # 6. 按 sticker_id 去重
        seen_stickers: set[str] = set()
        deduped: list[tuple[StickerRecord, float]] = []
        for record, score in scored:
            if record.sticker_id not in seen_stickers:
                seen_stickers.add(record.sticker_id)
                deduped.append((record, score))

        top_candidates = deduped[:top_k]

        # 7. 加权随机选择
        weights = [s ** 2 for _, s in top_candidates]
        total = sum(weights)
        if total == 0:
            return None

        chosen = random.choices(
            [r for r, _ in top_candidates],
            weights=[w / total for w in weights],
            k=1,
        )[0]

        logger.info(
            "表情包检索: context=%.20s... | 候选=%d | 选中=%s | 分数=%.3f",
            current_context,
            len(top_candidates),
            chosen.sticker_id,
            next(s for r, s in top_candidates if r.sticker_id == chosen.sticker_id),
        )
        return chosen

    def _semantic_search(
        self, current_context: str, top_k: int
    ) -> dict[str, float]:
        """内存中的 usage_context 语义检索。"""
        query_vec = self.encode_single(current_context)
        if not query_vec:
            return {}

        scores: dict[str, float] = {}
        for rid, record in self._records.items():
            if not record.usage_context_embedding:
                continue
            score = self._cosine_sim(query_vec, record.usage_context_embedding)
            if score > 0.25:
                scores[rid] = score
        return scores

    def _keyword_search(
        self, current_context: str, top_k: int
    ) -> dict[str, float]:
        query_lower = current_context.lower()
        scores: dict[str, float] = {}
        for rid, record in self._records.items():
            score = 0.0
            if query_lower in record.usage_context.lower():
                score += 1.0
            for tag in record.tags:
                if query_lower in tag.lower():
                    score += 0.8
            if query_lower in record.trigger_message.lower():
                score += 0.5
            if score > 0:
                scores[rid] = score
        return scores

    def _scene_search(
        self, scene_query: str, top_k: int
    ) -> dict[str, float]:
        """场景概括检索：Query B → scene_summary_embedding。"""
        query_vec = self.encode_single(scene_query)
        if not query_vec:
            return {}

        scores: dict[str, float] = {}
        for rid, record in self._records.items():
            if not record.scene_summary_embedding:
                continue
            score = self._cosine_sim(query_vec, record.scene_summary_embedding)
            if score > 0.25:
                scores[rid] = score
        return scores

    def _tag_matches(
        self,
        record_tag: str,
        target_tags: list[str],
        threshold: float = 0.75,
    ) -> bool:
        """精确匹配优先，失败时用 embedding 相似度兜底。"""
        if record_tag in target_tags:
            return True
        if not target_tags or not self.semantic_available:
            return False
        tag_emb = self._get_tag_embedding(record_tag)
        if tag_emb is None:
            return False
        for target in target_tags:
            target_emb = self._get_tag_embedding(target)
            if target_emb is not None:
                if self._cosine_sim(tag_emb, target_emb) >= threshold:
                    return True
        return False

    def _best_tag_match(
        self, record_tag: str, target_tags: list[str]
    ) -> str | None:
        """返回最匹配的目标标签，无匹配返回 None。"""
        if record_tag in target_tags:
            return record_tag
        if not target_tags or not self.semantic_available:
            return None
        tag_emb = self._get_tag_embedding(record_tag)
        if tag_emb is None:
            return None
        best_target, best_sim = None, 0.0
        for target in target_tags:
            target_emb = self._get_tag_embedding(target)
            if target_emb is not None:
                sim = self._cosine_sim(tag_emb, target_emb)
                if sim > best_sim:
                    best_sim = sim
                    best_target = target
        return best_target if best_sim >= 0.75 else None

    def _get_tag_embedding(self, tag: str) -> list[float] | None:
        cached = self._tag_embedding_cache.get(tag)
        if cached is not None:
            return cached
        try:
            emb = self.encode_single(tag)
            if not emb:
                return None
            self._tag_embedding_cache[tag] = emb
            return emb
        except Exception:
            return None

    def _score_sticker(
        self,
        record: StickerRecord,
        context_score: float,
        keyword_score: float,
        preference: StickerPreference,
        emotion_hint: str,
        scene_score: float = 0.0,
    ) -> float:
        """多维度评分。

        权重分配：
        - usage_context 语义：0.3（精确情境匹配）
        - scene_summary 语义：0.45（LLM 概括，可信度最高）
        - 关键词匹配：0.15（辅助）
        - 其余维度独立叠加
        """
        base_score = (
            0.3 * context_score
            + 0.45 * scene_score
            + 0.15 * min(keyword_score / 2.0, 1.0)
        )

        # 人格偏好匹配
        tag_bonus = 0.0
        tag_penalty = 0.0
        for tag in record.tags:
            if self._tag_matches(tag, preference.preferred_tags):
                tag_bonus += 0.12
            if self._tag_matches(tag, preference.avoided_tags):
                tag_penalty += 0.2

        # 标签成功率加权
        tag_success_bonus = 0.0
        for tag in record.tags:
            matched = self._best_tag_match(
                tag, list(preference.tag_success_rate.keys())
            )
            if matched is not None:
                success_rate = preference.tag_success_rate[matched]
            else:
                success_rate = 0.5
            tag_success_bonus += (success_rate - 0.5) * 0.1

        # 情绪匹配
        emotion_bonus = 0.0
        emotion_tags = preference.emotion_tag_map.get(emotion_hint, [])
        if emotion_tags:
            matching_count = sum(
                1 for tag in record.tags if self._tag_matches(tag, emotion_tags)
            )
            emotion_bonus = matching_count * 0.15

        # 新鲜度
        novelty_boost = (
            record.novelty_score * preference.novelty_preference * 0.25
        )

        # 使用频率惩罚
        overuse_penalty = min(0.35, record.usage_count * 0.06)

        # 近期使用惩罚
        recent_penalty = 0.0
        recent_usage = sum(
            1 for u in preference.recent_usage_window
            if u.get("sticker_id") == record.sticker_id
        )
        if recent_usage >= 3:
            recent_penalty = min(0.3, (recent_usage - 2) * 0.08)

        # 群聊反馈加权
        group_feedback_bonus = 0.0
        for tag in record.tags:
            matched = self._best_tag_match(
                tag, list(preference.group_tag_feedback.keys())
            )
            if matched is not None:
                feedback = preference.group_tag_feedback[matched]
            else:
                feedback = 0.5
            group_feedback_bonus += (feedback - 0.5) * 0.08

        final_score = (
            base_score
            + tag_bonus
            - tag_penalty
            + tag_success_bonus
            + emotion_bonus
            + novelty_boost
            - overuse_penalty
            - recent_penalty
            + group_feedback_bonus
        )
        return max(0.0, min(1.0, final_score))

    @staticmethod
    def _cosine_sim(a: list[float], b: list[float]) -> float:
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a == 0.0 or norm_b == 0.0:
            return 0.0
        return dot / (norm_a * norm_b)

    def get(self, record_id: str) -> StickerRecord | None:
        """按 record_id 获取记录。"""
        return self._records.get(record_id)

    def get_by_sticker_id(self, sticker_id: str) -> StickerRecord | None:
        """按 sticker_id 获取记录（返回第一条匹配）。"""
        for record in self._records.values():
            if record.sticker_id == sticker_id:
                return record
        return None

    def list_all(self) -> list[StickerRecord]:
        return list(self._records.values())

    def update_record(self, record: StickerRecord) -> None:
        """更新记录（如使用次数、新鲜度等）。"""
        self._records[record.record_id] = record
        if self._vector_store.available:
            self._vector_store.add(record)

    def load_from_disk(self) -> int:
        """从磁盘加载所有表情包记录。

        支持两种历史文件格式：
        - 旧格式：按 sticker_id 保存的单条记录（sticker_id.json）
        - 新格式：按 record_id 保存的单条记录（record_id.json）
        加载时统一使用 record_id 作为内部字典键。
        """
        sticker_dir = self._work_path / "records"
        if not sticker_dir.exists():
            return 0

        count = 0
        for file_path in sticker_dir.glob("*.json"):
            try:
                import json
                data = json.loads(file_path.read_text(encoding="utf-8"))
                record = StickerRecord.from_dict(data)
                # 统一使用 record_id 作为键，避免与 add() 方法不一致
                self._records[record.record_id] = record
                count += 1
            except Exception as exc:
                logger.warning("加载表情包记录失败 %s: %s", file_path, exc)

        logger.info("从磁盘加载 %d 条表情包记录", count)
        return count

    def save_to_disk(self) -> None:
        """保存所有表情包记录到磁盘。

        按 record_id 分文件保存，确保同一 sticker_id 的不同使用情境不会互相覆盖。
        """
        import json

        sticker_dir = self._work_path / "records"
        sticker_dir.mkdir(parents=True, exist_ok=True)

        # 清理旧格式的 sticker_id 命名文件（避免残留）
        existing_files = {f.name for f in sticker_dir.glob("*.json")}
        current_record_files = {f"{record.record_id}.json" for record in self._records.values()}
        stale_files = existing_files - current_record_files
        for stale in stale_files:
            try:
                (sticker_dir / stale).unlink()
            except Exception:
                pass

        for record in self._records.values():
            file_path = sticker_dir / f"{record.record_id}.json"
            try:
                file_path.write_text(
                    json.dumps(record.to_dict(), ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except Exception as exc:
                logger.warning("保存表情包记录失败 %s: %s", record.record_id, exc)

    def remove(self, record_id: str) -> None:
        """删除指定记录。"""
        if record_id in self._records:
            del self._records[record_id]
            if self._vector_store.available:
                self._vector_store.remove([record_id])
