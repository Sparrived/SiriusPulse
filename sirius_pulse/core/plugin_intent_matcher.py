"""轻量级插件意图匹配器。

使用嵌入向量相似度检测用户消息是否可能是自然语言插件请求。
用于管线短路合并场景：在跳过 LLM 认知之前，快速判断消息是否需要走完整管线。

依赖：
    - EmbeddingClient（嵌入向量微服务，~0.1ms/次）
    - PluginRegistry（插件注册表，提供 description 和 NL examples）
"""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from sirius_pulse.embedding.client import EmbeddingClient
    from sirius_pulse.plugins.registry import PluginRegistry

logger = logging.getLogger(__name__)

# 默认相似度阈值：超过此值认为可能是插件请求
DEFAULT_SIMILARITY_THRESHOLD = 0.65


def _cosine_similarity(a: list[float], b: list[float]) -> float:
    """计算两个向量的余弦相似度。"""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(x * x for x in b))
    if norm_a == 0 or norm_b == 0:
        return 0.0
    return dot / (norm_a * norm_b)


class PluginIntentMatcher:
    """基于嵌入向量的轻量插件意图匹配器。

    工作流程：
        1. 首次调用时，从 PluginRegistry 收集所有插件的 description 和 NL examples
        2. 批量计算这些文本的嵌入向量（缓存）
        3. 对用户消息计算嵌入向量，与插件向量比较余弦相似度
        4. 相似度超过阈值 → 认为可能是插件请求

    线程安全：无状态修改，仅读取缓存，可安全并发调用。
    """

    def __init__(
        self,
        embedding_client: EmbeddingClient,
        plugin_registry: PluginRegistry,
        threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    ) -> None:
        self._embedding_client = embedding_client
        self._plugin_registry = plugin_registry
        self._threshold = threshold

        # 缓存：(plugin_name, text_embedding) 列表
        self._plugin_embeddings: list[tuple[str, list[float]]] | None = None
        self._built = False

    def _build_index(self) -> None:
        """从 PluginRegistry 收集插件文本并计算嵌入向量。"""
        if not self._embedding_client.available:
            logger.debug("Embedding 服务不可用，跳过插件意图索引构建")
            self._built = True
            return

        import re

        # 占位符通用替换映射（保持语义完整性）
        _SLOT_DEFAULTS = {
            "city": "某个城市",
            "query": "某个问题",
            "text": "一些文本",
            "url": "一个链接",
            "name": "某个人",
            "date": "某个日期",
            "time": "某个时间",
            "keyword": "关键词",
            "topic": "某个话题",
        }

        def _clean_slot(s: str) -> str:
            """将 {slot} 替换为通用词而非删除，保持语义完整。"""

            def _replace(m: re.Match) -> str:
                slot = m.group(1).lower()
                return _SLOT_DEFAULTS.get(slot, "")

            return re.sub(r"\{([^}]+)\}", _replace, s).strip()

        texts: list[str] = []
        names: list[str] = []

        for name in self._plugin_registry.plugin_names:
            definition = self._plugin_registry.get(name)
            if definition is None:
                continue
            # 跳过对意图识别隐藏的插件
            if definition.permissions.hidden_from_intent:
                continue

            # 收集 description
            if definition.description:
                texts.append(definition.description)
                names.append(name)

            # 收集 NL examples
            if definition.natural_language and definition.natural_language.examples:
                for example in definition.natural_language.examples:
                    clean = _clean_slot(example)
                    if clean:
                        texts.append(clean)
                        names.append(name)

            # 收集 command descriptions 和 examples
            for cmd in definition.commands:
                if cmd.hidden_from_intent:
                    continue
                if cmd.description:
                    texts.append(cmd.description)
                    names.append(name)
                for ex in cmd.examples:
                    clean = _clean_slot(ex)
                    if clean:
                        texts.append(clean)
                        names.append(name)

        if not texts:
            logger.debug("未收集到任何插件文本，跳过索引构建")
            self._built = True
            return

        try:
            embeddings = self._embedding_client.encode(texts)
            self._plugin_embeddings = list(zip(names, embeddings))
            logger.info(
                "插件意图索引已构建: %d 个文本片段（%d 个插件）",
                len(texts),
                len(set(names)),
            )
        except Exception as exc:
            logger.warning("构建插件意图索引失败: %s", exc)
            self._plugin_embeddings = []

        self._built = True

    def match_plugin_candidates(self, message: str) -> list[str]:
        """检测用户消息并返回可能匹配的插件名称列表。

        Args:
            message: 用户输入文本

        Returns:
            匹配的插件名称列表（按相似度降序）。
            空列表表示不太可能是插件请求。
        """
        if not self._built:
            self._build_index()

        # 无插件向量或服务不可用 → 返回空列表
        if not self._plugin_embeddings:
            return []

        if not self._embedding_client.available:
            return []

        try:
            msg_embedding = self._embedding_client.encode_single(message)
        except Exception as exc:
            logger.debug("消息嵌入计算失败: %s", exc)
            return []

        # 收集所有超过阈值的插件（去重，保留最高相似度）
        plugin_sims: dict[str, float] = {}
        for plugin_name, plugin_emb in self._plugin_embeddings:
            sim = _cosine_similarity(msg_embedding, plugin_emb)
            if sim >= self._threshold:
                if plugin_name not in plugin_sims or sim > plugin_sims[plugin_name]:
                    plugin_sims[plugin_name] = sim

        # 按相似度降序排列
        candidates = sorted(plugin_sims.keys(), key=lambda x: plugin_sims[x], reverse=True)

        if candidates:
            logger.debug(
                "插件意图匹配: 候选=%s, similarities=%s",
                candidates,
                {k: f"{v:.3f}" for k, v in plugin_sims.items()},
            )

        return candidates

    def is_plugin_intent(self, message: str) -> bool:
        """检测用户消息是否可能是自然语言插件请求。

        Args:
            message: 用户输入文本

        Returns:
            True 表示可能是插件请求（应走完整管线），
            False 表示不太可能是插件请求（可安全短路合并）。
        """
        return len(self.match_plugin_candidates(message)) > 0
