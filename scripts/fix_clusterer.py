import pathlib
p = pathlib.Path(r"D:\Code\sirius_chat\sirius_pulse\memory\diary\clusterer.py")
content = p.read_text(encoding="utf-8")

# 1. Fix _call_llm to accept system_override parameter
old_call_llm_sig = '''    async def _call_llm(
        self,
        *,
        candidates: list[Any],
        persona_name: str,
        brain: Any,
        model_name: str,
        temperature: float,
        max_tokens: int,
    ) -> str:'''

new_call_llm_sig = '''    async def _call_llm(
        self,
        *,
        candidates: list[Any],
        persona_name: str,
        brain: Any,
        model_name: str,
        temperature: float,
        max_tokens: int,
        system_override: str | None = None,
    ) -> str:'''

content = content.replace(old_call_llm_sig, new_call_llm_sig)

# 2. Use system_override in raw_request
old_raw_request = '            system_prompt=_CLUSTER_SYSTEM_PROMPT,'
new_raw_request = '            system_prompt=system_override or _CLUSTER_SYSTEM_PROMPT,'
content = content.replace(old_raw_request, new_raw_request)

# 3. Fix the retry loop: add continue on exception and change fallback to time-based batching
old_cluster_method = '''        # All retries exhausted - fall back to single cluster
        logger.info("话题聚类重试耗尽，回退为单组处理 (%d 条消息)", len(candidates))
        return [TopicCluster(label="对话记录", entries=list(candidates))]'''

new_cluster_method = '''        # All retries exhausted - fall back to time-based batching
        # to still produce multiple diary entries instead of one giant one.
        logger.warning("话题聚类重试耗尽，回退为按时间分批 (%d 条消息)", len(candidates))
        return self._time_based_split(candidates)'''

content = content.replace(old_cluster_method, new_cluster_method)

# 4. Add the _time_based_split method before _call_llm
insert_marker = '    async def _call_llm('
time_based_method = '''    def _time_based_split(
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

'''

content = content.replace(insert_marker, time_based_method + insert_marker)

p.write_text(content, encoding="utf-8")
print("done")
