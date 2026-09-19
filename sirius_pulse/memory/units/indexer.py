"""In-memory hybrid retrieval for checkpoint memory units."""

from __future__ import annotations

import logging
import math
import re
import unicodedata
from datetime import datetime, timezone

from sirius_pulse.embedding.client import EmbeddingClient
from sirius_pulse.memory.units.deduplicator import same_boundary
from sirius_pulse.memory.units.models import MemoryUnit

logger = logging.getLogger(__name__)

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
        candidates = [
            (unit, self._cosine_sim(incoming.embedding, unit.embedding))
            for unit in self._units
            if unit.embedding and unit.unit_id != incoming.unit_id and same_boundary(unit, incoming)
        ]
        candidates = [item for item in candidates if item[1] >= min_similarity]
        candidates.sort(key=lambda item: item[1], reverse=True)
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
    ) -> list[tuple[MemoryUnit, float]]:
        """Search with semantic, lexical, identity, scope, and time signals."""
        units = [
            unit
            for unit in self._units
            if unit.should_prompt
            and not self._is_expired(unit)
            and self._scope_allowed(
                unit,
                query=query,
                group_id=group_id,
                user_id=user_id,
                identity_aliases=[*(identity_aliases or []), *(mentioned_user_ids or [])],
                cross_group_enabled=cross_group_enabled,
            )
        ]
        if not units:
            return []

        queries = self._query_variants(query)
        semantic_scores: dict[str, float] = {}
        if self.semantic_available and queries:
            try:
                vectors = self._encode_queries(queries)
            except Exception as exc:
                logger.warning("Memory unit semantic search failed: %s", exc)
                vectors = []
            for unit in units:
                if not unit.embedding:
                    continue
                scores = [self._cosine_sim(vector, unit.embedding) for vector in vectors]
                if scores:
                    semantic_scores[unit.unit_id] = max(scores)

        keyword_scores = {
            unit.unit_id: max((self._keyword_score(item, unit) for item in queries), default=0.0)
            for unit in units
        }
        semantic_rank = self._rank(semantic_scores, reverse=True)
        keyword_rank = self._rank(keyword_scores, reverse=True)
        temporal_query = any(term in query.casefold() for term in _TEMPORAL_TERMS)

        scored: list[tuple[MemoryUnit, float]] = []
        for unit in units:
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
        # ponytail: in-memory O(n) scoring is enough for the current unit volume;
        # add a persistent ANN index only when this scan becomes measurable.
        return scored[:top_k]

    def list_all(self) -> list[MemoryUnit]:
        return list(self._units)

    def reset(self) -> None:
        """丢弃全部已索引单元，供向量重建后强制重新加载。"""
        self._units = []

    def clear_group(self, group_id: str) -> None:
        self._units = [u for u in self._units if u.group_id != group_id]

    def replace_group(self, group_id: str, units: list[MemoryUnit]) -> None:
        """Replace every indexed unit in a group after persistence changes."""
        self.clear_group(group_id)
        for unit in units:
            self.add(unit)

    def _ensure_embedding(self, unit: MemoryUnit) -> bool:
        if not self.semantic_available or unit.embedding:
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
            vectors = self._embedding_client.encode(texts)
        except Exception as exc:
            logger.warning("Memory unit embedding failed: %s", exc)
            return []
        if len(vectors) != len(texts):
            logger.warning("Embedding 返回数量与请求不一致，跳过本次重算")
            return []
        return vectors

    def _encode_queries(self, queries: list[str]) -> list[list[float]]:
        if not self._embedding_client:
            return []
        if hasattr(self._embedding_client, "encode"):
            return [vector for vector in self._embedding_client.encode(queries) if vector]
        return [self._embedding_client.encode_single(query) for query in queries]

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
        return " ".join(
            [
                unit.summary,
                " ".join(unit.participants),
                " ".join(unit.topics),
                " ".join(unit.keywords),
                " ".join(unit.retrieval_terms),
                " ".join(unit.identity_aliases),
                unit.status,
                unit.event_time,
            ]
        ).strip()

    @classmethod
    def _keyword_score(cls, query: str, unit: MemoryUnit) -> float:
        query_text = cls._normalize_text(query)
        text = cls._normalize_text(cls._unit_text(unit))
        if not query_text or not text:
            return 0.0
        score = 1.5 if query_text in text else 0.0
        query_tokens = cls._tokens(query_text)
        text_tokens = cls._tokens(text)
        if query_tokens:
            score += min(len(query_tokens & text_tokens) * 0.18, 1.2)
        for field in (
            unit.identity_aliases,
            unit.keywords,
            unit.retrieval_terms,
            unit.topics,
            unit.participants,
        ):
            for value in field:
                value = cls._normalize_text(value)
                if value and value in query_text:
                    score += 1.0 if value in unit.identity_aliases else 0.65
        return score

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
    ) -> list[MemoryUnit]:
        results = self._indexer.search(
            query,
            group_id=group_id,
            top_k=top_k,
            user_id=user_id,
            identity_aliases=identity_aliases,
            mentioned_user_ids=mentioned_user_ids,
            cross_group_enabled=cross_group_enabled,
        )
        if not results:
            return []

        selected: list[MemoryUnit] = []
        total_chars = 0
        char_budget = int(max_tokens_budget * 1.5)
        for unit, _score in results:
            added_chars = len(unit.summary)
            if total_chars + added_chars > char_budget and selected:
                continue
            selected.append(unit)
            total_chars += added_chars
        return selected
