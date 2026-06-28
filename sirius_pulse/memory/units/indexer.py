"""In-memory retrieval for checkpoint memory units."""

from __future__ import annotations

import logging
import math
import re

from sirius_pulse.embedding.client import EmbeddingClient
from sirius_pulse.memory.units.models import MemoryUnit

logger = logging.getLogger(__name__)


class MemoryUnitIndexer:
    """Hybrid semantic/keyword index for memory units."""

    def __init__(self, embedding_client: EmbeddingClient | None = None) -> None:
        self._units: list[MemoryUnit] = []
        self._embedding_client = embedding_client

    @property
    def semantic_available(self) -> bool:
        return self._embedding_client is not None and self._embedding_client.available

    def add(self, unit: MemoryUnit) -> bool:
        recomputed = False
        if self.semantic_available and not unit.embedding:
            text = self._unit_text(unit)
            vec = self._embedding_client.encode_single(text) if self._embedding_client else []
            if vec:
                unit.embedding = vec
                recomputed = True
        self._units.append(unit)
        return recomputed

    def search(
        self,
        query: str,
        *,
        group_id: str = "",
        top_k: int = 5,
    ) -> list[tuple[MemoryUnit, float]]:
        units = [u for u in self._units if u.should_prompt]
        if group_id:
            units = [u for u in units if u.group_id == group_id]
        if not units:
            return []

        semantic_scores: dict[str, float] = {}
        if self.semantic_available:
            try:
                query_vec = self._embedding_client.encode_single(query) if self._embedding_client else []
            except Exception as exc:
                logger.warning("Memory unit semantic search failed: %s", exc)
                query_vec = []
            if query_vec:
                for unit in units:
                    if unit.embedding:
                        semantic_scores[unit.unit_id] = self._cosine_sim(query_vec, unit.embedding)

        keyword_scores: dict[str, float] = {}
        for unit in units:
            keyword_scores[unit.unit_id] = self._keyword_score(query, unit)

        scored: list[tuple[MemoryUnit, float]] = []
        for unit in units:
            semantic = semantic_scores.get(unit.unit_id, 0.0)
            keyword = keyword_scores.get(unit.unit_id, 0.0)
            quality = max(0.0, min(1.0, unit.salience)) * max(0.0, min(1.0, unit.confidence))
            score = (0.6 * semantic) + (0.4 * min(keyword / 3.0, 1.0)) + (0.2 * quality)
            if score > 0.08:
                scored.append((unit, score))

        scored.sort(key=lambda item: item[1], reverse=True)
        return scored[:top_k]

    def list_all(self) -> list[MemoryUnit]:
        return list(self._units)

    def clear_group(self, group_id: str) -> None:
        self._units = [u for u in self._units if u.group_id != group_id]

    @staticmethod
    def _unit_text(unit: MemoryUnit) -> str:
        return " ".join(
            [
                unit.summary,
                " ".join(unit.participants),
                " ".join(unit.topics),
                " ".join(unit.keywords),
            ]
        ).strip()

    @classmethod
    def _keyword_score(cls, query: str, unit: MemoryUnit) -> float:
        query_lower = query.lower()
        text = cls._unit_text(unit).lower()
        score = 0.0
        if query_lower and query_lower in text:
            score += 1.2
        query_terms = [t for t in re.split(r"\s+", query_lower) if len(t) >= 2]
        for term in query_terms:
            if term in text:
                score += 0.4
        for keyword in unit.keywords + unit.topics + unit.participants:
            if keyword and keyword.lower() in query_lower:
                score += 0.8
        return score

    @staticmethod
    def _cosine_sim(a: list[float], b: list[float]) -> float:
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
    ) -> list[MemoryUnit]:
        results = self._indexer.search(query, group_id=group_id, top_k=top_k)
        if not results:
            return []

        selected: list[MemoryUnit] = []
        total_chars = 0
        char_budget = int(max_tokens_budget * 1.5)
        for unit, _score in results:
            added_chars = len(unit.summary) + sum(len(x) for x in unit.keywords[:5])
            if total_chars + added_chars > char_budget and selected:
                break
            selected.append(unit)
            total_chars += added_chars
        return selected
