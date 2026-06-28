"""Topic clustering for diary generation candidates.

Splits a large batch of conversation messages into topic-based clusters
so that each cluster can be independently summarized into a diary entry.
This prevents information loss that occurs when too many messages are
crammed into a single diary generation call.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sirius_pulse.memory.basic.models import BasicMemoryEntry

logger = logging.getLogger(__name__)

_CLUSTER_SYSTEM_PROMPT = (
    "你是群聊对话分析助手。请将以下对话记录按话题分组。\n"
    "\n"
    "【分组要求】\n"
    "- 每条消息属于且仅属于一个话题组\n"
    "- 每组应包含语义相关的消息（同一事件、同一讨论、同一情感脉络）\n"
    "- 每组至少 3 条消息；如果某条消息与所有组都不相关，归入最近的组\n"
    "- 最多不超过 6 个组\n"
    "- 每组用一个简短标签概括（不超过 15 字）\n"
    "\n"
    "严格输出 JSON，格式如下（不要加 markdown 代码块）：\n"
    '{"clusters": [{"label": "话题标签", "indices": [0, 1, 2]}, ...]}'
)


@dataclass
class TopicCluster:
    """A topic-based grouping of conversation messages."""

    label: str
    entries: list[Any] = field(default_factory=list)


class TopicClusterer:
    """Splits candidate messages into topic-based clusters via LLM."""

    def __init__(self, max_clusters: int = 6, min_cluster_size: int = 3) -> None:
        self.max_clusters = max_clusters
        self.min_cluster_size = min_cluster_size

    async def cluster(
        self,
        *,
        candidates: list[Any],
        persona_name: str,
        brain: Any,
        model_name: str,
        temperature: float = 0.3,
        max_tokens: int = 1024,
        max_retries: int = 2,
    ) -> list[TopicCluster]:
        """Cluster candidates by topic using a lightweight LLM call.

        Retries up to *max_retries* times on LLM failure or JSON parse
        failure.  Only falls back to a single cluster after all retries
        are exhausted.
        """
        if len(candidates) <= self.min_cluster_size:
            return [TopicCluster(label="对话记录", entries=list(candidates))]

        system_prompt = _CLUSTER_SYSTEM_PROMPT
        for attempt in range(max_retries + 1):
            try:
                raw = await self._call_llm(
                    candidates=candidates,
                    persona_name=persona_name,
                    brain=brain,
                    model_name=model_name,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    system_override=system_prompt if attempt > 0 else None,
                )
            except Exception as exc:
                logger.warning(
                    "话题聚类 LLM 调用失败 (attempt %d/%d): %s",
                    attempt + 1,
                    max_retries + 1,
                    exc,
                )
                if attempt < max_retries:
                    continue
                break

            clusters = self._parse_response(raw, candidates)
            if clusters:
                logger.info(
                    "话题聚类完成: %d 条消息分为 %d 组 (attempt %d)",
                    len(candidates),
                    len(clusters),
                    attempt + 1,
                )
                return clusters

            # Parse failed — strengthen prompt for next attempt
            if attempt < max_retries:
                logger.warning(
                    "话题聚类 JSON 解析失败 (attempt %d/%d)，准备重试",
                    attempt + 1,
                    max_retries + 1,
                )
                system_prompt = (
                    _CLUSTER_SYSTEM_PROMPT
                    + "\n\n【重要提醒】上一次输出不是合法 JSON，"
                    "请确保本次输出是严格合法的 JSON 对象，不要包含任何其他文字。"
                )

        # All retries exhausted — fall back to single cluster
        logger.info("话题聚类重试耗尽，回退为单组处理 (%d 条消息)", len(candidates))
        return [TopicCluster(label="对话记录", entries=list(candidates))]

    def _time_based_split(
        self, candidates: list[Any], batch_size: int = 15
    ) -> list[TopicCluster]:
        """Split candidates into fixed-size batches by time order as a fallback.

        Each batch gets a generic label based on position (e.g. "对话片段 1").
        """
        batches: list[TopicCluster] = []
        for i in range(0, len(candidates), batch_size):
            chunk = candidates[i : i + batch_size]
            label = f"对话片段 {len(batches) + 1}"
            batches.append(TopicCluster(label=label, entries=list(chunk)))
        return batches

    async def _call_llm(
        self,
        *,
        candidates: list[Any],
        persona_name: str,
        brain: Any,
        model_name: str,
        temperature: float,
        max_tokens: int,
        system_override: str | None = None,
    ) -> str:
        from sirius_pulse.core.brain import RawRequest

        lines: list[str] = []
        for i, e in enumerate(candidates):
            name = e.speaker_name if e.speaker_name else e.user_id
            lines.append(f"[{i}] [{name}] {e.content}")
        conversation = "\n".join(lines)

        user_prompt = (
            f"以下是对话记录，格式：[序号] [发言人] 内容\n\n"
            f"{conversation}\n\n"
            f"请按话题分组。"
        )

        raw_request = RawRequest(
            model=model_name,
            system_prompt=system_override or _CLUSTER_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
            temperature=temperature,
            max_tokens=max_tokens,
            purpose="topic_cluster",
            response_format={"type": "json_object"},
        )
        return await brain.raw_call(raw_request)

    def _parse_response(
        self, raw: str, candidates: list[Any]
    ) -> list[TopicCluster] | None:
        """Parse LLM JSON response into TopicCluster list."""
        data = self._extract_json(raw)
        if not data or "clusters" not in data:
            return None

        raw_clusters = data["clusters"]
        if not isinstance(raw_clusters, list) or not raw_clusters:
            return None

        clusters: list[TopicCluster] = []
        assigned: set[int] = set()

        for item in raw_clusters:
            if not isinstance(item, dict):
                continue
            label = str(item.get("label", "")).strip()[:15] or "其他"
            indices = item.get("indices", [])
            if not isinstance(indices, list):
                continue

            valid_indices = []
            for idx in indices:
                try:
                    idx_int = int(idx)
                except (ValueError, TypeError):
                    continue
                if 0 <= idx_int < len(candidates) and idx_int not in assigned:
                    valid_indices.append(idx_int)
                    assigned.add(idx_int)

            if valid_indices:
                entries = [candidates[i] for i in valid_indices]
                clusters.append(TopicCluster(label=label, entries=entries))

        # Assign any unassigned messages to the nearest cluster by index
        for i in range(len(candidates)):
            if i not in assigned:
                # Put into the last cluster
                if clusters:
                    clusters[-1].entries.append(candidates[i])
                else:
                    clusters.append(TopicCluster(label="其他", entries=[candidates[i]]))

        # Merge clusters that are too small
        clusters = self._merge_small_clusters(clusters)

        return clusters if len(clusters) > 1 else None

    def _merge_small_clusters(
        self, clusters: list[TopicCluster]
    ) -> list[TopicCluster]:
        """Merge clusters smaller than min_cluster_size into neighbors."""
        if len(clusters) <= 1:
            return clusters

        merged: list[TopicCluster] = []
        for cluster in clusters:
            if len(cluster.entries) < self.min_cluster_size and merged:
                # Merge into the previous cluster
                merged[-1].entries.extend(cluster.entries)
            else:
                merged.append(cluster)

        # If the first cluster was too small and has no predecessor,
        # merge it into the next one
        if merged and len(merged[0].entries) < self.min_cluster_size and len(merged) > 1:
            merged[1].entries = merged[0].entries + merged[1].entries
            merged.pop(0)

        return merged

    @staticmethod
    def _extract_json(raw: str) -> dict[str, Any] | None:
        """Extract JSON from LLM output, tolerating markdown fences."""
        text = raw.strip()
        if "```" in text:
            parts = text.split("```")
            if len(parts) >= 3:
                text = parts[1]
                if text.startswith("json"):
                    text = text[4:]
                text = text.strip()
        try:
            result = json.loads(text)
        except (json.JSONDecodeError, ValueError):
            logger.warning("话题聚类 JSON 解析失败")
            return None
        return result if isinstance(result, dict) else None
