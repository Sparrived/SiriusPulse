"""表情包语义索引：检索 + 多维度加权随机选择。"""

from __future__ import annotations

import logging
import math
import os
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sirius_chat.skills.sticker.models import StickerPreference, StickerRecord
from sirius_chat.skills.sticker.vector_store import StickerVectorStore

logger = logging.getLogger(__name__)

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

try:
    from sentence_transformers import SentenceTransformer

    _ST_AVAILABLE = True
except Exception:  # pragma: no cover
    _ST_AVAILABLE = False

_MODEL_SINGLETON: dict[str, Any] = {}


class StickerIndexer:
    """表情包语义索引，支持多维度加权检索。"""

    MODEL_NAME: str = "BAAI/bge-small-zh"

    def __init__(
        self,
        work_path: Path | str,
        persona_name: str,
        enable_semantic: bool = True,
    ) -> None:
        self._work_path = Path(work_path)
        self._persona_name = persona_name
        self._enable_semantic = enable_semantic
        self._model: Any | None = None
        self._embedding_dim: int | None = None

        self._records: dict[str, StickerRecord] = {}
        self._vector_store = StickerVectorStore(
            persist_dir=self._work_path / "vector_store",
            persona_name=persona_name,
        )

        if not enable_semantic:
            logger.debug("表情包语义索引已禁用")
        elif not _ST_AVAILABLE:
            logger.warning("sentence-transformers 未安装，表情包检索将退化为纯关键词匹配")

    def _ensure_model_loaded(self) -> None:
        if self._model is not None or not self._enable_semantic or not _ST_AVAILABLE:
            return
        cached = _MODEL_SINGLETON.get(self.MODEL_NAME)
        if cached is not None:
            self._model = cached
            self._embedding_dim = getattr(self._model, "get_embedding_dimension", lambda: None)()
            logger.info("表情包索引复用已缓存模型 %s", self.MODEL_NAME)
        else:
            self._model = self._load_model_local_first(self.MODEL_NAME)
            if self._model is not None:
                _MODEL_SINGLETON[self.MODEL_NAME] = self._model
                self._embedding_dim = getattr(self._model, "get_embedding_dimension", lambda: None)()
                logger.info("表情包索引已加载模型 %s", self.MODEL_NAME)

    @staticmethod
    def _load_model_local_first(model_name: str) -> Any | None:
        try:
            model = SentenceTransformer(model_name, local_files_only=True)
            logger.info("模型 %s 从本地缓存加载", model_name)
            return model
        except Exception as exc:
            logger.warning("表情包索引模型本地加载失败: %s", exc)
            return None

    @property
    def semantic_available(self) -> bool:
        return self._model is not None

    def add(self, record: StickerRecord) -> bool:
        """添加表情包到索引，计算 usage_context_embedding。

        同一个 sticker_id 的不同使用情境会作为独立记录存储，
        以 record_id 为键，支持多情境检索。

        添加时会自动检查同一 sticker_id 下是否有语义相似的
        已有记录，相似则合并，避免重复存储。
        """
        logger.debug("表情包索引添加: record_id=%s sticker_id=%s file=%s", record.record_id, record.sticker_id, record.file_path)
        self._ensure_model_loaded()
        recomputed = False
        if self._model is not None:
            if record.usage_context_embedding and self._embedding_dim is not None:
                if len(record.usage_context_embedding) != self._embedding_dim:
                    record.usage_context_embedding = None
                    record.caption_embedding = None

            if not record.usage_context_embedding:
                try:
                    vec = self._model.encode(record.usage_context, convert_to_tensor=False)
                    record.usage_context_embedding = [float(v) for v in vec]
                    recomputed = True
                    logger.debug("表情包索引: record_id=%s usage_context_embedding 计算完成", record.record_id)
                except Exception as exc:
                    logger.warning("表情包 usage_context embedding 计算失败: %s", exc)

            if not record.caption_embedding and record.caption:
                try:
                    cap_vec = self._model.encode(record.caption, convert_to_tensor=False)
                    record.caption_embedding = [float(v) for v in cap_vec]
                except Exception as exc:
                    logger.warning("表情包 caption embedding 计算失败: %s", exc)

        # 场景概括 embedding
        if self._model is not None and record.scene_summary and not record.scene_summary_embedding:
            try:
                scene_vec = self._model.encode(record.scene_summary, convert_to_tensor=False)
                record.scene_summary_embedding = [float(v) for v in scene_vec]
            except Exception as exc:
                logger.warning("场景概括 embedding 计算失败: %s", exc)

        if self._vector_store.available:
            self._vector_store.add(record)
            logger.debug("表情包索引: record_id=%s 已存入向量存储", record.record_id)
        else:
            logger.debug("表情包索引: record_id=%s 向量存储不可用，仅存入内存", record.record_id)

        self._records[record.record_id] = record
        logger.info("表情包索引添加完成: record_id=%s 总记录数=%d", record.record_id, len(self._records))
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

        同一个 sticker_id 按最高分去重。
        """
        self._ensure_model_loaded()

        if not self._records:
            logger.debug("表情包库为空")
            return None

        # 1. 语义检索：Query A → usage_context
        context_scores: dict[str, float] = {}
        if self._model is not None and self._vector_store.available:
            try:
                query_vec = self._model.encode(current_context, convert_to_tensor=False)
                for rid, score in self._vector_store.search(query_vec, top_k=top_k * 2):
                    context_scores[rid] = score
            except Exception as exc:
                logger.warning("向量存储检索失败，回退到内存检索: %s", exc)
                context_scores = self._semantic_search(current_context, top_k * 2)
        else:
            context_scores = self._semantic_search(current_context, top_k * 2)

        # 2. 语义检索：Query B → scene_summary
        scene_scores: dict[str, float] = {}
        if scene_query and self._model is not None:
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

        # 6. 按 sticker_id 去重，保留每个表情包的最高分记录
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

    def _semantic_search(self, current_context: str, top_k: int) -> dict[str, float]:
        if self._model is None:
            return {}
        try:
            query_vec = self._model.encode(current_context, convert_to_tensor=False)
        except Exception as exc:
            logger.warning("Context embedding 失败: %s", exc)
            return {}

        scores: dict[str, float] = {}
        for rid, record in self._records.items():
            if not record.usage_context_embedding:
                continue
            score = self._cosine_sim(query_vec, record.usage_context_embedding)
            if score > 0.25:
                scores[rid] = score
        return scores

    def _keyword_search(self, current_context: str, top_k: int) -> dict[str, float]:
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

    def _scene_search(self, scene_query: str, top_k: int) -> dict[str, float]:
        """场景概括检索：Query B → scene_summary_embedding。"""
        if self._model is None:
            return {}
        try:
            query_vec = self._model.encode(scene_query, convert_to_tensor=False)
        except Exception as exc:
            logger.warning("场景查询 embedding 失败: %s", exc)
            return {}

        scores: dict[str, float] = {}
        for rid, record in self._records.items():
            if not record.scene_summary_embedding:
                continue
            score = self._cosine_sim(query_vec, record.scene_summary_embedding)
            if score > 0.25:
                scores[rid] = score
        return scores

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

        # 2. 人格偏好匹配
        tag_bonus = 0.0
        tag_penalty = 0.0
        for tag in record.tags:
            if tag in preference.preferred_tags:
                tag_bonus += 0.12
            if tag in preference.avoided_tags:
                tag_penalty += 0.2

        # 3. 标签成功率加权
        tag_success_bonus = 0.0
        for tag in record.tags:
            success_rate = preference.tag_success_rate.get(tag, 0.5)
            tag_success_bonus += (success_rate - 0.5) * 0.1

        # 4. 情绪匹配
        emotion_bonus = 0.0
        emotion_tags = preference.emotion_tag_map.get(emotion_hint, [])
        if emotion_tags:
            matching = set(record.tags) & set(emotion_tags)
            emotion_bonus = len(matching) * 0.15

        # 5. 新鲜度（喜新厌旧）
        novelty_boost = record.novelty_score * preference.novelty_preference * 0.25

        # 6. 使用频率惩罚（避免过度使用）
        overuse_penalty = min(0.35, record.usage_count * 0.06)

        # 7. 近期使用惩罚（模拟"一段时间内偏爱某几个"后的倦怠）
        recent_penalty = 0.0
        recent_usage = sum(
            1 for u in preference.recent_usage_window
            if u.get("sticker_id") == record.sticker_id
        )
        if recent_usage >= 3:
            recent_penalty = min(0.3, (recent_usage - 2) * 0.08)

        # 8. 群聊反馈加权
        group_feedback_bonus = 0.0
        for tag in record.tags:
            feedback = preference.group_tag_feedback.get(tag, 0.5)
            group_feedback_bonus += (feedback - 0.5) * 0.08

        final_score = (
            base_score +
            tag_bonus - tag_penalty +
            tag_success_bonus +
            emotion_bonus +
            novelty_boost -
            overuse_penalty -
            recent_penalty +
            group_feedback_bonus
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

    def get(self, sticker_id: str) -> StickerRecord | None:
        return self._records.get(sticker_id)

    def list_all(self) -> list[StickerRecord]:
        return list(self._records.values())

    def update_record(self, record: StickerRecord) -> None:
        """更新记录（如使用次数、新鲜度等）。"""
        self._records[record.sticker_id] = record
        if self._vector_store.available:
            self._vector_store.add(record)

    def load_from_disk(self) -> int:
        """从磁盘加载所有表情包记录。"""
        sticker_dir = self._work_path / "records"
        if not sticker_dir.exists():
            return 0

        count = 0
        for file_path in sticker_dir.glob("*.json"):
            try:
                import json
                data = json.loads(file_path.read_text(encoding="utf-8"))
                record = StickerRecord.from_dict(data)
                self._records[record.sticker_id] = record
                count += 1
            except Exception as exc:
                logger.warning("加载表情包记录失败 %s: %s", file_path, exc)

        logger.info("从磁盘加载 %d 条表情包记录", count)
        return count

    def save_to_disk(self) -> None:
        """保存所有表情包记录到磁盘。"""
        import json

        sticker_dir = self._work_path / "records"
        sticker_dir.mkdir(parents=True, exist_ok=True)

        for record in self._records.values():
            file_path = sticker_dir / f"{record.sticker_id}.json"
            try:
                file_path.write_text(
                    json.dumps(record.to_dict(), ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except Exception as exc:
                logger.warning("保存表情包记录失败 %s: %s", file_path, exc)

    def remove(self, record_id: str) -> None:
        if record_id in self._records:
            del self._records[record_id]
        if self._vector_store.available:
            self._vector_store.remove([record_id])


