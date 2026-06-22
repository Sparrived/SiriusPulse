p = r"D:\Code\sirius_chat\sirius_pulse\memory\diary\clusterer.py"
with open(p, "r", encoding="utf-8") as f:
    content = f.read()

old_method = '''    async def cluster(
        self,
        *,
        candidates: list[Any],
        persona_name: str,
        brain: Any,
        model_name: str,
        temperature: float = 0.3,
        max_tokens: int = 1024,
    ) -> list[TopicCluster]:
        """Cluster candidates by topic using a lightweight LLM call.

        Returns a list of TopicCluster objects. On failure, falls back
        to a single cluster containing all candidates.
        """
        if len(candidates) <= self.min_cluster_size:
            return [TopicCluster(label="对话记录", entries=list(candidates))]

        try:
            raw = await self._call_llm(
                candidates=candidates,
                persona_name=persona_name,
                brain=brain,
                model_name=model_name,
                temperature=temperature,
                max_tokens=max_tokens,
            )
            clusters = self._parse_response(raw, candidates)
            if clusters:
                logger.info(
                    "话题聚类完成: %d 条消息分为 %d 组",
                    len(candidates),
                    len(clusters),
                )
                return clusters
        except Exception as exc:
            logger.warning("话题聚类 LLM 调用失败: %s", exc)

        # Fallback: single cluster
        logger.info("话题聚类失败，回退为单组处理 (%d 条消息)", len(candidates))
        return [TopicCluster(label="对话记录", entries=list(candidates))]'''

new_method = '''    async def cluster(
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
        return [TopicCluster(label="对话记录", entries=list(candidates))]'''

content = content.replace(old_method, new_method)
with open(p, "w", encoding="utf-8") as f:
    f.write(content)
print("done")
