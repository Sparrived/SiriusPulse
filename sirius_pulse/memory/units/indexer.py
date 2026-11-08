"""In-memory hybrid retrieval for checkpoint memory units."""

from __future__ import annotations

import logging
import math
import re
import threading
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import datetime, timezone
from typing import Any, Callable

import numpy as np

from sirius_pulse.embedding.client import EmbeddingClient
from sirius_pulse.memory.units.deduplicator import same_boundary
from sirius_pulse.memory.units.models import MemoryUnit, embedding_text

logger = logging.getLogger(__name__)

#: 聊天回合里给查询编码的超时。查询向量在回合开始时于后台线程预取，超时只会让
#: 这一轮退化成关键词检索，不该把整轮回复拖住 30 秒。
QUERY_ENCODE_TIMEOUT_SECONDS = 8.0
_QUERY_CACHE_SIZE = 256
#: 上下文变体（最近几条消息 + 当前消息）的权重：它补足「那后来呢」这类短追问的
#: 指代，但不能盖过当前消息本身。
CONTEXT_SEMANTIC_WEIGHT = 0.85
CONTEXT_KEYWORD_WEIGHT = 0.6
#: 冷层（超额退休的单元）参与检索的门槛：只有语义上足够近才捞回来，且同分时让位
#: 给热层，免得几千条旧单元把当前活跃的记忆挤出去。
COLD_MIN_SEMANTIC = 0.5
COLD_SCORE_FACTOR = 0.9
COLD_SEMANTIC_CANDIDATES = 20
#: 相对截断：分数低于本轮最高分这个比例的单元不注入。top_k 只是上限，真正决定
#: 注入几条的是与问题的相关度。
RELATIVE_SCORE_CUTOFF = 0.6
#: 两条单元的余弦达到这个值就视为同一件事的不同说法，注入时只留一条。
#: 依据线上活跃单元实测：改写之间的最近邻余弦中位数 0.948，其余单元 0.802。
PARAPHRASE_SIMILARITY = 0.88
#: 不会被冷层捞回的退休原因：模型或真人明确说过「不要进提示词」，以及已过期。
_NEVER_COLD_REASONS = frozenset({"model", "manual", "expired"})


#: 单元的关键词特征：(归一化文本, 字符 n-gram 集合, [(检索词, 权重)])。
_UnitFeatures = tuple[str, set[str], list[tuple[str, float]]]


@dataclass(slots=True)
class ColdVectors:
    """某个群冷层单元的向量矩阵（已归一化、float16 存放以省内存）。"""

    unit_ids: list[str]
    matrix: np.ndarray
    rows: dict[str, int] = dataclass_field(default_factory=dict)
    #: 与矩阵行一一对应的单元对象；该群单元一被替换，整份缓存就作废重建。
    units: list[MemoryUnit] = dataclass_field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.rows:
            self.rows = {unit_id: row for row, unit_id in enumerate(self.unit_ids)}


def is_cold_eligible(unit: MemoryUnit) -> bool:
    """超额退休、未过期、且没被明确禁止进提示词的单元属于冷层。"""
    if unit.should_prompt:
        return False
    metadata = unit.metadata if isinstance(unit.metadata, dict) else {}
    if str(metadata.get("retired") or "") in _NEVER_COLD_REASONS:
        return False
    return not MemoryUnitIndexer._is_expired(unit)


def normalize_rows(matrix: np.ndarray) -> np.ndarray:
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0.0] = 1.0
    return matrix / norms


def call_encode(
    client: Any, texts: list[str], *, timeout: float | None = None
) -> list[list[float]]:
    """兼容只实现了 ``encode_single`` 或不接受 ``timeout`` 的客户端。"""
    if not texts:
        return []
    encode = getattr(client, "encode", None)
    if callable(encode):
        if timeout is not None:
            try:
                return encode(texts, timeout=timeout)
            except TypeError:
                pass
        return encode(texts)
    return [client.encode_single(text) for text in texts]


_QUERY_SPLIT_RE = re.compile(r"[\r\n]+|(?<=[。！？!?])")
_TAG_RE = re.compile(r"<[^>]+>")
_WORD_RE = re.compile(r"[0-9a-z_]+", re.IGNORECASE)
_CJK_RE = re.compile(r"[\u4e00-\u9fff]")
_TEMPORAL_TERMS = (
    "现在",
    "目前",
    "当前",
    "最近",
    "上次",
    "之前",
    "后来",
    "之后",
    "计划",
    "完成",
    "还在",
    "还没",
    "多久",
    "何时",
    "什么时候",
    "today",
    "current",
    "latest",
    "recent",
    "before",
    "after",
    "plan",
    "done",
)
_STATUS_TERMS = {
    "planned": ("计划", "打算", "准备", "将要"),
    "active": ("正在", "进行", "当前", "还在"),
    "completed": ("完成", "已经", "做过", "上线", "部署"),
    "cancelled": ("取消", "放弃", "不做了"),
}


class MemoryUnitIndexer:
    """Hybrid semantic/keyword index for memory units.

    The index stays in memory, so a larger candidate pool is cheap. Semantic
    and lexical routes produce independent rankings before final fusion.
    """

    def __init__(self, embedding_client: EmbeddingClient | None = None) -> None:
        self._units: list[MemoryUnit] = []
        self._embedding_client = embedding_client
        #: 查询文本 → 归一化向量。回合开始时在后台线程预取，检索时直接命中。
        self._query_cache: OrderedDict[tuple[str, str], np.ndarray] = OrderedDict()
        #: 预取在后台线程写缓存，检索在事件循环里读，读写都过这把锁。
        self._query_lock = threading.Lock()
        #: unit_id → (原始向量列表, 归一化 numpy 向量)。用列表的身份判断是否失效。
        self._vector_cache: dict[str, tuple[list[float], np.ndarray]] = {}
        #: unit_id → (单元文本, 关键词特征)。文本变了就重算，免得每轮对每条单元重新切词。
        self._feature_cache: dict[str, tuple[str, _UnitFeatures]] = {}
        #: 冷层向量来源，由 MemoryUnitManager 注入；为 None 时不检索冷层。
        self.cold_source: Callable[[str], ColdVectors | None] | None = None

    @property
    def semantic_available(self) -> bool:
        return self._embedding_client is not None and self._embedding_client.available

    def add(self, unit: MemoryUnit) -> bool:
        recomputed = self._ensure_embedding(unit)
        self._units.append(unit)
        return recomputed

    def semantic_candidates(
        self,
        incoming: MemoryUnit,
        *,
        top_k: int = 5,
        min_similarity: float = 0.8,
    ) -> list[tuple[MemoryUnit, float]]:
        """Return boundary-scoped semantic candidates for an incoming unit."""
        self._ensure_embedding(incoming)
        if not incoming.embedding:
            return []
        query = self._unit_vector(incoming)
        pool = [
            unit
            for unit in self._units
            if unit.embedding
            and len(unit.embedding) == len(incoming.embedding)
            and unit.unit_id != incoming.unit_id
            and same_boundary(unit, incoming)
        ]
        if not pool or query is None:
            return []
        sims = self._unit_matrix(pool) @ query
        order = np.argsort(-sims)
        candidates = [
            (pool[index], float(sims[index])) for index in order if sims[index] >= min_similarity
        ]
        return candidates[:top_k]

    def search(
        self,
        query: str,
        *,
        group_id: str = "",
        top_k: int = 5,
        user_id: str = "",
        identity_aliases: list[str] | None = None,
        mentioned_user_ids: list[str] | None = None,
        cross_group_enabled: bool = False,
        context: str = "",
    ) -> list[tuple[MemoryUnit, float]]:
        """Search with semantic, lexical, identity, scope, and time signals.

        ``context`` 是当前消息之前的几句对话，作为降权的额外查询变体，用来补全
        「那后来呢」这类短追问的指代。热层之外，语义足够接近的冷层（超额退休）
        单元也会被捞回来，但同分时让位给热层。
        """
        aliases = [*(identity_aliases or []), *(mentioned_user_ids or [])]

        def allowed(unit: MemoryUnit) -> bool:
            return self._scope_allowed(
                unit,
                query=query,
                group_id=group_id,
                user_id=user_id,
                identity_aliases=aliases,
                cross_group_enabled=cross_group_enabled,
            )

        units = [
            unit
            for unit in self._units
            if unit.should_prompt and not self._is_expired(unit) and allowed(unit)
        ]
        variants = self.query_variants(query, context)
        query_matrix, weights = self._query_matrix(variants)
        cold_hits = self._cold_hits(group_id, query_matrix, weights, allowed)
        if not units and not cold_hits:
            return []

        semantic_scores: dict[str, float] = {}
        if query_matrix is not None:
            pool = [
                unit
                for unit in units
                if unit.embedding and len(unit.embedding) == query_matrix.shape[1]
            ]
            if pool:
                sims = (self._unit_matrix(pool) @ query_matrix.T) * weights
                for unit, best in zip(pool, sims.max(axis=1)):
                    semantic_scores[unit.unit_id] = float(best)
        cold_ids = set()
        cold_vectors: dict[str, np.ndarray] = {}
        for unit, similarity, vector in cold_hits:
            semantic_scores[unit.unit_id] = similarity
            cold_ids.add(unit.unit_id)
            cold_vectors[unit.unit_id] = vector
        candidates = [*units, *(unit for unit, _, _ in cold_hits)]

        keyword_queries = self._keyword_queries(variants)
        keyword_scores = {
            unit.unit_id: self._weighted_keyword_score(keyword_queries, unit) for unit in candidates
        }
        semantic_rank = self._rank(semantic_scores, reverse=True)
        keyword_rank = self._rank(keyword_scores, reverse=True)
        temporal_query = any(term in query.casefold() for term in _TEMPORAL_TERMS)

        scored: list[tuple[MemoryUnit, float]] = []
        for unit in candidates:
            semantic = semantic_scores.get(unit.unit_id, 0.0)
            keyword = keyword_scores.get(unit.unit_id, 0.0)
            if semantic <= 0.12 and keyword <= 0.0:
                continue
            keyword_norm = min(keyword / 3.0, 1.0)
            rank_fusion = self._rrf(semantic_rank.get(unit.unit_id), keyword_rank.get(unit.unit_id))
            quality = max(0.0, min(1.0, unit.salience)) * max(0.0, min(1.0, unit.confidence))
            temporal = self._temporal_score(query, unit) if temporal_query else 0.0
            score = (
                0.55 * semantic
                + 0.25 * keyword_norm
                + 0.10 * rank_fusion
                + 0.05 * quality
                + 0.05 * temporal
            )
            if unit.unit_id in cold_ids:
                score *= COLD_SCORE_FACTOR
            scored.append((unit, score))

        scored.sort(
            key=lambda item: (item[1], self._parse_time(item[0].event_time or item[0].created_at)),
            reverse=True,
        )
        # 冲突对里旧的那条让位，避免同一事实槽位的新旧值同时进入提示词，让模型
        # 自己去猜哪个才是当前状态。
        present = {unit.unit_id: unit for unit, _score in scored}
        scored = [
            (unit, score) for unit, score in scored if not self._superseded_within(unit, present)
        ]
        if scored:
            floor = scored[0][1] * RELATIVE_SCORE_CUTOFF
            scored = [item for item in scored if item[1] >= floor]
        return self._drop_paraphrases(scored, cold_vectors, top_k)

    def _drop_paraphrases(
        self,
        scored: list[tuple[MemoryUnit, float]],
        cold_vectors: dict[str, np.ndarray],
        top_k: int,
    ) -> list[tuple[MemoryUnit, float]]:
        """同一件事的几种说法只留分数最高的那条，把名额让给不同的事。

        线上同一条「谁是她的主人」被反复提炼成几十种说法，一次检索能占掉四五个
        名额。改写之间的余弦中位数约 0.95，而不同事实之间多在 0.80 左右，
        ``PARAPHRASE_SIMILARITY`` 取在两者之间。没有向量的单元不参与比较，照常保留。
        """
        kept: list[tuple[MemoryUnit, float]] = []
        kept_vectors: list[np.ndarray] = []
        for unit, score in scored:
            if len(kept) >= top_k:
                break
            vector = cold_vectors.get(unit.unit_id)
            if vector is None:
                vector = self._unit_vector(unit)
            if vector is not None and any(
                other.shape == vector.shape and float(other @ vector) >= PARAPHRASE_SIMILARITY
                for other in kept_vectors
            ):
                continue
            kept.append((unit, score))
            if vector is not None:
                kept_vectors.append(vector)
        return kept

    # ------------------------------------------------------------------
    # 向量与查询
    # ------------------------------------------------------------------

    @classmethod
    def query_variants(cls, query: str, context: str = "") -> list[tuple[str, float]]:
        """检索用的查询文本及其权重。预取与检索必须用同一份，才能命中缓存。"""
        variants = [(text, 1.0) for text in cls._query_variants(query)]
        context_text = re.sub(
            r"\s+", " ", _TAG_RE.sub(" ", unicodedata.normalize("NFKC", str(context or "")))
        ).strip()
        if context_text and variants:
            combined = f"{context_text[-300:]} {variants[0][0]}"
            variants.append((combined, CONTEXT_SEMANTIC_WEIGHT))
        return variants

    def prepare_queries(self, query: str, context: str = "") -> None:
        """在后台线程里预先编码查询变体，聊天主循环里的检索就不再发网络请求。"""
        variants = self.query_variants(query, context)
        if not variants or not self.semantic_available:
            return
        model = self._model_key()
        with self._query_lock:
            missing = [text for text, _ in variants if (model, text) not in self._query_cache]
        if missing:
            self._encode_into_cache(missing)

    def _model_key(self) -> str:
        return str(getattr(self._embedding_client, "model", "") or "")

    def _encode_into_cache(self, texts: list[str]) -> None:
        try:
            vectors = call_encode(
                self._embedding_client, texts, timeout=QUERY_ENCODE_TIMEOUT_SECONDS
            )
        except Exception as exc:
            logger.warning("Memory unit semantic search failed: %s", exc)
            return
        if len(vectors) != len(texts):
            return
        model = self._model_key()
        with self._query_lock:
            for text, vector in zip(texts, vectors):
                if not vector:
                    continue
                array = np.asarray(vector, dtype=np.float32)
                norm = float(np.linalg.norm(array))
                self._query_cache[(model, text)] = array / norm if norm else array
                self._query_cache.move_to_end((model, text))
            while len(self._query_cache) > _QUERY_CACHE_SIZE:
                self._query_cache.popitem(last=False)

    def _query_matrix(
        self, variants: list[tuple[str, float]]
    ) -> tuple[np.ndarray | None, np.ndarray]:
        """返回 (查询向量矩阵, 各变体权重)；语义不可用时矩阵为 None。"""
        if not variants or not self.semantic_available:
            return None, np.zeros(0, dtype=np.float32)
        model = self._model_key()
        with self._query_lock:
            missing = [text for text, _ in variants if (model, text) not in self._query_cache]
        if missing:
            # 没有预取（例如直接调用检索的路径）时才同步编码。
            self._encode_into_cache(missing)
        rows, weights = [], []
        with self._query_lock:
            for text, weight in variants:
                vector = self._query_cache.get((model, text))
                if vector is not None:
                    rows.append(vector)
                    weights.append(weight)
        if not rows or len({row.shape[0] for row in rows}) != 1:
            return None, np.zeros(0, dtype=np.float32)
        return np.vstack(rows), np.asarray(weights, dtype=np.float32)

    def _unit_vector(self, unit: MemoryUnit) -> np.ndarray | None:
        embedding = unit.embedding
        if not embedding:
            return None
        cached = self._vector_cache.get(unit.unit_id)
        if cached is not None and cached[0] is embedding:
            return cached[1]
        array = np.asarray(embedding, dtype=np.float32)
        norm = float(np.linalg.norm(array))
        vector = array / norm if norm else array
        self._vector_cache[unit.unit_id] = (embedding, vector)
        return vector

    def _unit_matrix(self, units: list[MemoryUnit]) -> np.ndarray:
        return np.vstack([self._unit_vector(unit) for unit in units])

    def _cold_hits(
        self,
        group_id: str,
        query_matrix: np.ndarray | None,
        weights: np.ndarray,
        allowed: Callable[[MemoryUnit], bool],
    ) -> list[tuple[MemoryUnit, float, np.ndarray]]:
        """在冷层里找语义足够接近的单元；冷层向量没准备好时这一轮直接跳过。"""
        if query_matrix is None or self.cold_source is None or not group_id:
            return []
        cold = self.cold_source(group_id)
        if cold is None or not cold.unit_ids or cold.matrix.shape[1] != query_matrix.shape[1]:
            return []
        sims = (cold.matrix.astype(np.float32) @ query_matrix.T) * weights
        best = sims.max(axis=1)
        hits: list[tuple[MemoryUnit, float, np.ndarray]] = []
        for row in np.argsort(-best):
            similarity = float(best[row])
            if similarity < COLD_MIN_SEMANTIC or len(hits) >= COLD_SEMANTIC_CANDIDATES:
                break
            unit = cold.units[row] if row < len(cold.units) else None
            # 只对排在前面的几条复查资格（可能刚过期）与可见范围，不扫整组。
            if unit is not None and is_cold_eligible(unit) and allowed(unit):
                hits.append((unit, similarity, cold.matrix[row].astype(np.float32)))
        return hits

    def list_all(self) -> list[MemoryUnit]:
        return list(self._units)

    def reset(self) -> None:
        """丢弃全部已索引单元，供向量重建后强制重新加载。"""
        self._units = []
        self._vector_cache.clear()
        self._feature_cache.clear()
        with self._query_lock:
            self._query_cache.clear()

    def clear_group(self, group_id: str) -> None:
        for unit in self._units:
            if unit.group_id == group_id:
                self._vector_cache.pop(unit.unit_id, None)
                self._feature_cache.pop(unit.unit_id, None)
        self._units = [u for u in self._units if u.group_id != group_id]

    def encode_missing(self, units: list[MemoryUnit]) -> int:
        """一次请求补齐一批可注入单元的向量（逐条请求会在加载时发出几百次网络调用）。"""
        if not self.semantic_available:
            return 0
        pending = [unit for unit in units if unit.should_prompt and not unit.embedding]
        if not pending:
            return 0
        vectors = self._encode_texts([self._unit_text(unit) for unit in pending])
        done = 0
        for unit, vector in zip(pending, vectors):
            if vector:
                unit.embedding = vector
                done += 1
        return done

    def replace_group(self, group_id: str, units: list[MemoryUnit]) -> None:
        """Replace every indexed unit in a group after persistence changes."""
        self.clear_group(group_id)
        for unit in units:
            self.add(unit)

    def _ensure_embedding(self, unit: MemoryUnit) -> bool:
        # 退休单元永远不进提示词，给它们算向量是纯浪费（线上单群 7547/8047 条是退休的）。
        # 需要它们的向量时由去重路径显式从磁盘补齐（见 MemoryUnitManager.reconcile_units）。
        if not self.semantic_available or unit.embedding or not unit.should_prompt:
            return False
        try:
            vec = self._embedding_client.encode_single(self._unit_text(unit))
        except Exception as exc:
            logger.warning("Memory unit embedding failed: %s", exc)
            return False
        if not vec:
            return False
        unit.embedding = vec
        return True

    def ensure_model_current(self, units: list[MemoryUnit], *, batch_size: int = 64) -> bool:
        """把维度与当前 embedding 模型不一致的向量重算一遍；返回是否有改动。

        换 embedding 模型必然换维度（``bge-small-zh`` 512 维、``bge-m3`` 1024 维）。
        内存单元的向量是内联存在 JSON 里的，旧向量留着不但没用，还会因为维度不同被
        判为不相似，让语义检索静默退化成纯关键词检索，所以必须重算。

        当前维度未知时先编码第一条文本，用它的结果同时拿到维度与向量，避免为探测多花
        一次请求。仍然失败的批次会在日志里点名，调用方可据此如实汇报，而不是把部分
        失败当成功。
        """
        if not self.semantic_available or not units:
            return False

        expected = self._embedding_client.dimension
        changed = False
        if expected is None:
            probed = self._encode_texts([self._unit_text(units[0])])
            if not probed:
                return False
            units[0].embedding = probed[0]
            expected = len(probed[0])
            changed = True
            rest = units[1:]
        else:
            rest = units

        stale = [unit for unit in rest if not unit.embedding or len(unit.embedding) != expected]
        if not stale:
            return changed

        recomputed = 0
        for start in range(0, len(stale), batch_size):
            chunk = stale[start : start + batch_size]
            recomputed += self._recompute_chunk(chunk, expected)

        failed = [unit for unit in stale if not unit.embedding or len(unit.embedding) != expected]
        if recomputed:
            changed = True
            logger.info(
                "已按 %s 重算 %d/%d 条记忆单元向量（原维度与当前模型不一致）",
                self._embedding_client.model or "当前模型",
                recomputed,
                len(units),
            )
        if failed:
            logger.warning(
                "仍有 %d 条记忆单元向量未能重算（embedding 请求失败），它们会被判为不相似",
                len(failed),
            )
        return changed

    def _recompute_chunk(self, chunk: list[MemoryUnit], expected: int) -> int:
        """重算一批向量；失败时二分重试，返回成功的条数。

        整批超时会把整批都丢掉。线上 64 条一批时偶发超时，二分重试能把绝大多数救回来，
        比直接放弃整批更划算；最后仍失败的会在调用方被点名。
        """
        vectors = self._encode_texts([self._unit_text(unit) for unit in chunk])
        if len(vectors) == len(chunk):
            done = 0
            for unit, vector in zip(chunk, vectors):
                if vector and len(vector) == expected:
                    unit.embedding = vector
                    done += 1
            if done == len(chunk):
                return done

        if len(chunk) == 1:
            return 0
        middle = len(chunk) // 2
        return self._recompute_chunk(chunk[:middle], expected) + self._recompute_chunk(
            chunk[middle:], expected
        )

    def _encode_texts(self, texts: list[str]) -> list[list[float]]:
        """按位置返回向量；不做过滤，否则向量会与文本错配。"""
        if not self._embedding_client or not texts:
            return []
        try:
            vectors = call_encode(self._embedding_client, texts)
        except Exception as exc:
            logger.warning("Memory unit embedding failed: %s", exc)
            return []
        if len(vectors) != len(texts):
            logger.warning("Embedding 返回数量与请求不一致，跳过本次重算")
            return []
        return vectors

    @classmethod
    def _query_variants(cls, query: str) -> list[str]:
        text = _TAG_RE.sub(" ", unicodedata.normalize("NFKC", str(query or "")))
        text = re.sub(r"\s+", " ", text).strip()
        if not text:
            return []
        variants = [text]
        for part in _QUERY_SPLIT_RE.split(text):
            part = part.strip(" \t,，。！？!?;；")
            if part and part not in variants and len(part) >= 2:
                variants.append(part)
        return variants[:8]

    @classmethod
    def _unit_text(cls, unit: MemoryUnit) -> str:
        return embedding_text(unit)

    @classmethod
    def _keyword_queries(
        cls, variants: list[tuple[str, float]]
    ) -> list[tuple[str, set[str], float]]:
        """每个查询变体只归一化、切词一次，而不是对每条单元各做一遍。"""
        prepared = []
        for text, weight in variants:
            normalized = cls._normalize_text(text)
            factor = 1.0 if weight >= 1.0 else CONTEXT_KEYWORD_WEIGHT
            prepared.append((normalized, cls._tokens(normalized), factor))
        return prepared

    def _weighted_keyword_score(
        self, queries: list[tuple[str, set[str], float]], unit: MemoryUnit
    ) -> float:
        features = self._unit_features(unit)
        best = 0.0
        for query_text, query_tokens, factor in queries:
            best = max(best, factor * self._score_features(query_text, query_tokens, features))
        return best

    def _unit_features(self, unit: MemoryUnit) -> _UnitFeatures:
        text = self._unit_text(unit)
        cached = self._feature_cache.get(unit.unit_id)
        if cached is not None and cached[0] == text:
            return cached[1]
        features = self._build_features(text, unit)
        self._feature_cache[unit.unit_id] = (text, features)
        return features

    @classmethod
    def _build_features(cls, text: str, unit: MemoryUnit) -> _UnitFeatures:
        normalized = cls._normalize_text(text)
        terms: list[tuple[str, float]] = []
        for values in (
            unit.identity_aliases,
            unit.keywords,
            unit.retrieval_terms,
            unit.topics,
            unit.participants,
        ):
            for value in values:
                value = cls._normalize_text(value)
                if value:
                    terms.append((value, 1.0 if value in unit.identity_aliases else 0.65))
        return normalized, cls._tokens(normalized), terms

    @staticmethod
    def _score_features(query_text: str, query_tokens: set[str], features: _UnitFeatures) -> float:
        text, text_tokens, terms = features
        if not query_text or not text:
            return 0.0
        score = 1.5 if query_text in text else 0.0
        if query_tokens:
            score += min(len(query_tokens & text_tokens) * 0.18, 1.2)
        for value, weight in terms:
            if value in query_text:
                score += weight
        return score

    @classmethod
    def _keyword_score(cls, query: str, unit: MemoryUnit) -> float:
        query_text = cls._normalize_text(query)
        features = cls._build_features(cls._unit_text(unit), unit)
        return cls._score_features(query_text, cls._tokens(query_text), features)

    @staticmethod
    def _normalize_text(value: str) -> str:
        return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value).casefold()).strip()

    @staticmethod
    def _tokens(text: str) -> set[str]:
        tokens = set(_WORD_RE.findall(text))
        cjk = "".join(_CJK_RE.findall(text))
        for size in (2, 3, 4):
            tokens.update(cjk[index : index + size] for index in range(len(cjk) - size + 1))
        return {token for token in tokens if len(token) >= 2}

    @staticmethod
    def _rank(scores: dict[str, float], *, reverse: bool) -> dict[str, int]:
        ordered = sorted(scores.items(), key=lambda item: item[1], reverse=reverse)
        return {unit_id: index for index, (unit_id, score) in enumerate(ordered, 1) if score > 0}

    @staticmethod
    def _rrf(semantic_rank: int | None, keyword_rank: int | None) -> float:
        total = 0.0
        if semantic_rank:
            total += 1.0 / (20.0 + semantic_rank)
        if keyword_rank:
            total += 1.0 / (20.0 + keyword_rank)
        return min(1.0, total * 10.0)

    @classmethod
    def _scope_allowed(
        cls,
        unit: MemoryUnit,
        *,
        query: str,
        group_id: str,
        user_id: str,
        identity_aliases: list[str],
        cross_group_enabled: bool,
    ) -> bool:
        same_group = not group_id or unit.group_id == group_id
        if unit.scope in {"persona", "global"}:
            return same_group or cross_group_enabled
        if unit.scope != "user":
            return same_group
        keys = {cls._identity_key(value) for value in [user_id, *identity_aliases] if value}
        unit_keys = {
            cls._identity_key(value)
            for value in [unit.scope_id, *unit.identity_aliases, *unit.participants]
            if value
        }
        query_key = cls._normalize_text(query)
        alias_keys = {cls._identity_key(value) for value in identity_aliases if value}
        explicit_identity = any(
            key and (key in query_key or key in alias_keys) for key in unit_keys
        )
        if same_group and (keys & unit_keys or explicit_identity):
            return True
        if not cross_group_enabled or unit.scope not in {"user", "persona", "global"}:
            return False
        return bool(keys & unit_keys)

    @staticmethod
    def _identity_key(value: str) -> str:
        text = re.sub(
            r"[^0-9a-z\u4e00-\u9fff]+", "", unicodedata.normalize("NFKC", value).casefold()
        )
        return text[2:] if text.startswith("qq") and text[2:].isdigit() else text

    @classmethod
    def _is_expired(cls, unit: MemoryUnit) -> bool:
        """valid_until 已过的事实不再进入候选。

        这是硬闩而不是降权：过期事实一旦被注入上下文，模型就可能当成当前状态复述，
        而 LLM 给出的 valid_until 本身并不可靠，靠降分挡不住它。解析失败一律视为
        未过期，宁可多召回一条也不要因为脏数据把有效记忆判死。
        """
        if not unit.valid_until:
            return False
        deadline = cls._parse_time(unit.valid_until)
        return 0.0 < deadline < datetime.now(timezone.utc).timestamp()

    @classmethod
    def _superseded_within(cls, unit: MemoryUnit, present: dict[str, MemoryUnit]) -> bool:
        """冲突对中若更新的那条也在候选里，本条让位。

        只压掉「两条同时被召回」的情况：另一条没被召回到时，保留本条也比什么都不给
        更好。两条时间都无法解析时不比较，倾向于两条都留。
        """
        metadata = unit.metadata if isinstance(unit.metadata, dict) else {}
        for other_id in metadata.get("conflicts_with") or []:
            other = present.get(str(other_id))
            if other is None:
                continue
            if cls._parse_time(other.event_time or other.created_at) > cls._parse_time(
                unit.event_time or unit.created_at
            ):
                return True
        return False

    @classmethod
    def _temporal_score(cls, query: str, unit: MemoryUnit) -> float:
        query_text = cls._normalize_text(query)
        score = 0.0
        for status, terms in _STATUS_TERMS.items():
            if any(term.casefold() in query_text for term in terms):
                score = max(score, 1.0 if unit.status == status else 0.0)
        if (
            unit.valid_until
            and cls._parse_time(unit.valid_until) >= datetime.now(timezone.utc).timestamp()
        ):
            score = max(score, 0.5)
        return score

    @staticmethod
    def _parse_time(value: str) -> float:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return (
                parsed.replace(tzinfo=timezone.utc).timestamp()
                if parsed.tzinfo is None
                else parsed.timestamp()
            )
        except (TypeError, ValueError, AttributeError):
            return 0.0

    @staticmethod
    def _cosine_sim(a: list[float], b: list[float]) -> float:
        # 维度不一致时必须直接判为不相似：zip 会按短的那条截断，算出一个看似合理的
        # 分数。换 embedding 模型后旧向量与新查询向量维度不同，正是这种情况。
        if len(a) != len(b):
            return 0.0
        dot = sum(x * y for x, y in zip(a, b))
        norm_a = math.sqrt(sum(x * x for x in a))
        norm_b = math.sqrt(sum(x * x for x in b))
        if norm_a == 0.0 or norm_b == 0.0:
            return 0.0
        return dot / (norm_a * norm_b)


class MemoryUnitRetriever:
    """Retrieves memory units within an approximate token budget."""

    def __init__(self, indexer: MemoryUnitIndexer) -> None:
        self._indexer = indexer

    def retrieve(
        self,
        query: str,
        *,
        group_id: str = "",
        top_k: int = 5,
        max_tokens_budget: int = 800,
        user_id: str = "",
        identity_aliases: list[str] | None = None,
        mentioned_user_ids: list[str] | None = None,
        cross_group_enabled: bool = False,
        context: str = "",
    ) -> list[MemoryUnit]:
        from sirius_pulse.token.utils import estimate_tokens

        results = self._indexer.search(
            query,
            group_id=group_id,
            top_k=top_k,
            user_id=user_id,
            identity_aliases=identity_aliases,
            mentioned_user_ids=mentioned_user_ids,
            cross_group_enabled=cross_group_enabled,
            context=context,
        )
        if not results:
            return []

        selected: list[MemoryUnit] = []
        used = 0
        for unit, _score in results:
            # 每行注入时还带时间、类型和状态前缀，按约 12 token 计。
            cost = estimate_tokens(unit.summary) + 12
            if used + cost > max_tokens_budget and selected:
                continue
            selected.append(unit)
            used += cost
        return selected
