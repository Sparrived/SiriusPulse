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

        # 检查同一 sticker_id 下是否有语义相似的记录，有则合并
        merged = self._try_merge_on_add(record)
        if merged:
            logger.info("表情包学习时合并: %s -> %s", record.record_id, merged.record_id)
            # 更新被合并记录的向量存储
            if self._vector_store.available:
                self._vector_store.add(merged)
            return recomputed

        if self._vector_store.available:
            self._vector_store.add(record)
            logger.debug("表情包索引: record_id=%s 已存入向量存储", record.record_id)
        else:
            logger.debug("表情包索引: record_id=%s 向量存储不可用，仅存入内存", record.record_id)

        self._records[record.record_id] = record
        logger.info("表情包索引添加完成: record_id=%s 总记录数=%d", record.record_id, len(self._records))
        return recomputed

    def _try_merge_on_add(
        self,
        record: StickerRecord,
        similarity_threshold: float = 0.85,
        max_context_length: int = 800,
    ) -> StickerRecord | None:
        """尝试将新记录合并到同一 sticker_id 的相似记录中。

        遍历该 sticker_id 的所有已有记录，计算语义相似度，
        超过阈值则合并并返回被合并的目标记录，否则返回 None。
        """
        if self._model is None or not record.usage_context_embedding:
            return None

        for existing in self._records.values():
            if existing.sticker_id != record.sticker_id:
                continue
            if not existing.usage_context_embedding:
                continue
            sim = self._cosine_sim(record.usage_context_embedding, existing.usage_context_embedding)
            if sim >= similarity_threshold:
                # 合并到已有记录
                combined = existing.usage_context + "\n---\n" + record.usage_context
                if len(combined) > max_context_length:
                    combined = combined[:max_context_length]
                existing.usage_context = combined
                existing.tags = list(set(existing.tags + record.tags))
                existing.usage_count += record.usage_count
                # 重新计算 embedding
                try:
                    vec = self._model.encode(existing.usage_context, convert_to_tensor=False)
                    existing.usage_context_embedding = [float(v) for v in vec]
                except Exception as exc:
                    logger.warning("合并后 embedding 重算失败: %s", exc)
                logger.info(
                    "表情包记录合并: %s | %s -> %s (sim=%.3f)",
                    record.sticker_id,
                    record.record_id,
                    existing.record_id,
                    sim,
                )
                return existing

        return None

    def search(
        self,
        current_context: str,
        preference: StickerPreference,
        emotion_hint: str = "neutral",
        top_k: int = 20,
        similarity_threshold: float = 0.6,
    ) -> StickerRecord | None:
        """按当前情境检索最匹配的表情包，返回加权随机选择的结果。

        同一个 sticker_id 可能有多个使用情境记录，检索时会找到
        最匹配的情境记录，但最终按 sticker_id 去重，避免重复推荐
        同一个表情包的不同情境。
        """
        self._ensure_model_loaded()

        if not self._records:
            logger.debug("表情包库为空")
            return None

        # 1. 语义检索（按 usage_context 匹配）
        semantic_scores: dict[str, float] = {}
        if self._model is not None and self._vector_store.available:
            try:
                query_vec = self._model.encode(current_context, convert_to_tensor=False)
                for rid, score in self._vector_store.search(query_vec, top_k=top_k * 2):
                    semantic_scores[rid] = score
            except Exception as exc:
                logger.warning("向量存储检索失败，回退到内存检索: %s", exc)
                semantic_scores = self._semantic_search(current_context, top_k * 2)
        else:
            semantic_scores = self._semantic_search(current_context, top_k * 2)

        # 2. 关键词检索（作为补充）
        keyword_scores = self._keyword_search(current_context, top_k * 2)

        # 3. 合并候选（按 record_id）
        candidates: dict[str, StickerRecord] = {}
        for rid in set(semantic_scores) | set(keyword_scores):
            if rid in self._records:
                candidates[rid] = self._records[rid]

        if not candidates:
            return None

        # 4. 多维度评分（按 record_id）
        scored: list[tuple[StickerRecord, float]] = []
        for rid, record in candidates.items():
            score = self._score_sticker(
                record,
                semantic_scores.get(rid, 0.0),
                keyword_scores.get(rid, 0.0),
                preference,
                emotion_hint,
            )
            if score >= similarity_threshold:
                scored.append((record, score))

        if not scored:
            return None

        scored.sort(key=lambda x: x[1], reverse=True)

        # 5. 按 sticker_id 去重，保留每个表情包的最高分记录
        seen_stickers: set[str] = set()
        deduped: list[tuple[StickerRecord, float]] = []
        for record, score in scored:
            if record.sticker_id not in seen_stickers:
                seen_stickers.add(record.sticker_id)
                deduped.append((record, score))

        top_candidates = deduped[:top_k]

        # 6. 加权随机选择
        weights = [s ** 2 for _, s in top_candidates]  # 平方放大差异
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

    def _score_sticker(
        self,
        record: StickerRecord,
        semantic_score: float,
        keyword_score: float,
        preference: StickerPreference,
        emotion_hint: str,
    ) -> float:
        """多维度评分。"""
        # 1. 基础语义分（语义 60% + 关键词 40%）
        base_score = 0.6 * semantic_score + 0.4 * min(keyword_score / 2.0, 1.0)

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

    def merge_similar_records(
        self,
        similarity_threshold: float = 0.85,
        max_context_length: int = 800,
    ) -> int:
        """合并同一 sticker_id 下语义相似的使用情境记录。

        扫描所有记录，对同一表情包的不同情境计算语义相似度，
        相似度超过阈值的记录合并为一条，更新 embedding。

        Args:
            similarity_threshold: 相似度阈值，超过则合并
            max_context_length: 合并后 usage_context 的最大长度

        Returns:
            合并掉的记录数量
        """
        self._ensure_model_loaded()
        if self._model is None:
            logger.debug("模型未加载，跳过合并")
            return 0

        # 按 sticker_id 分组
        groups: dict[str, list[StickerRecord]] = {}
        for record in self._records.values():
            groups.setdefault(record.sticker_id, []).append(record)

        merged_count = 0
        to_remove: list[str] = []

        for sticker_id, records in groups.items():
            if len(records) <= 1:
                continue

            # 计算每对记录的相似度，贪婪合并
            merged: list[StickerRecord] = []
            for record in sorted(records, key=lambda r: r.discovered_at):
                if record.record_id in to_remove:
                    continue

                found_similar = False
                for target in merged:
                    if not record.usage_context_embedding or not target.usage_context_embedding:
                        continue
                    sim = self._cosine_sim(record.usage_context_embedding, target.usage_context_embedding)
                    if sim >= similarity_threshold:
                        # 合并到 target
                        combined = target.usage_context + "\n---\n" + record.usage_context
                        if len(combined) > max_context_length:
                            combined = combined[:max_context_length]
                        target.usage_context = combined
                        target.tags = list(set(target.tags + record.tags))
                        target.usage_count += record.usage_count
                        # 重新计算 embedding
                        try:
                            vec = self._model.encode(target.usage_context, convert_to_tensor=False)
                            target.usage_context_embedding = [float(v) for v in vec]
                        except Exception as exc:
                            logger.warning("合并后 embedding 重算失败: %s", exc)
                        to_remove.append(record.record_id)
                        merged_count += 1
                        found_similar = True
                        logger.info(
                            "表情包记录合并: %s | %s -> %s (sim=%.3f)",
                            sticker_id,
                            record.record_id,
                            target.record_id,
                            sim,
                        )
                        break

                if not found_similar:
                    merged.append(record)

        # 清理被合并的记录
        for rid in to_remove:
            self.remove(rid)

        # 重新保存向量存储
        if self._vector_store.available and to_remove:
            for record in self._records.values():
                self._vector_store.add(record)
            logger.info("表情包相似记录合并完成: 合并 %d 条，剩余 %d 条", merged_count, len(self._records))

        return merged_count
