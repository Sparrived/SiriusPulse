"""表情包自动学习器：从群聊中学习"在什么情境下使用这个表情包"。"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sirius_chat.skills.sticker.models import StickerRecord
from sirius_chat.skills.sticker.indexer import StickerIndexer

logger = logging.getLogger(__name__)

_TAG_EXTRACTION_PROMPT = """根据以下表情包的使用情境，提取 3-5 个标签。

使用情境：{usage_context}
触发消息：{trigger_message}
发送者：{source_user}
情绪：{trigger_emotion}

要求：
- 标签应涵盖：情绪（如开心、无奈、生气）、场景（如上班、游戏、睡觉）、风格（如可爱、沙雕、正经）
- 用中文，每个标签 2-4 个字
- 只输出 JSON 对象，不要输出 markdown 代码块，不要输出任何其他文字
- 格式：{"tags": ["标签1", "标签2", ...]}"""


class StickerLearner:
    """从群聊消息中自动学习表情包的使用情境。

    核心：学习"在什么情境下人类会使用这个表情包"，
    而非"这个表情包描述了什么"。
    """

    def __init__(
        self,
        indexer: StickerIndexer,
        provider_async: Any | None = None,
        basic_memory: Any | None = None,
        model_name: str = "gpt-4o-mini",
        token_callback: Any | None = None,
    ) -> None:
        self._indexer = indexer
        self._provider_async = provider_async
        self._basic_memory = basic_memory
        self._model_name = model_name
        self._token_callback = token_callback
        self._learned_ids: set[str] = set()

    async def learn_from_message(
        self,
        sticker_id: str,
        file_path: str,
        caption: str,
        trigger_message: str,
        trigger_emotion: str,
        source_user: str,
        source_group: str,
    ) -> StickerRecord | None:
        """从一条消息中学习表情包的使用情境。

        同一个 sticker_id 的不同使用情境会作为独立记录存储，
        以 record_id（sticker_id + usage_context_hash）为键。

        Args:
            sticker_id: 表情包唯一标识（MD5 哈希）
            file_path: 本地文件路径
            caption: cognition 生成的图片描述（辅助理解）
            trigger_message: 触发这条表情包的消息内容
            trigger_emotion: 当时的情绪
            source_user: 发送者
            source_group: 群号

        Returns:
            学习到的 StickerRecord，如果该情境已存在则返回 None
        """
        logger.debug("表情包学习进入: sticker_id=%s file=%s", sticker_id, file_path)
        if sticker_id in self._learned_ids:
            logger.debug("表情包学习跳过: sticker_id=%s 已在本次运行中学习过", sticker_id)
            return None

        # 构建使用情境：前3条消息 + 当前触发消息
        usage_context = self._build_usage_context(source_group, trigger_message, source_user)
        logger.info("表情包学习: sticker_id=%s 使用情境构建完成, length=%d", sticker_id, len(usage_context))

        # 生成标签（基于使用情境而非图片描述）
        tags = await self._generate_tags(usage_context, trigger_message, source_user, trigger_emotion)
        logger.info("表情包学习: sticker_id=%s 标签生成完成, tags=%s", sticker_id, tags)

        record = StickerRecord(
            sticker_id=sticker_id,
            file_path=file_path,
            caption=caption,
            usage_context=usage_context,
            trigger_message=trigger_message,
            trigger_emotion=trigger_emotion,
            source_user=source_user,
            source_group=source_group,
            discovered_at=datetime.now(timezone.utc).isoformat(),
            tags=tags,
            novelty_score=1.0,
        )

        # 检查该情境是否已存在（以 record_id 去重）
        if self._indexer.get(record.record_id) is not None:
            logger.debug("表情包学习跳过: record_id=%s 已存在于索引中", record.record_id)
            self._learned_ids.add(sticker_id)
            return None

        self._indexer.add(record)
        self._learned_ids.add(sticker_id)

        logger.info(
            "学习新表情包: %s | tags=%s | 情境=%.30s...",
            sticker_id,
            tags,
            usage_context,
        )
        return record

    def _build_usage_context(
        self,
        group_id: str,
        trigger_message: str,
        source_user: str,
    ) -> str:
        """构建使用情境：前3条消息 + 当前触发消息。"""
        context_parts: list[str] = []

        # 获取前3条消息
        if self._basic_memory is not None:
            try:
                recent = self._basic_memory.get_recent_messages(group_id, limit=3)
                for msg in recent:
                    speaker = getattr(msg, "speaker", "") or ""
                    content = getattr(msg, "content", "") or ""
                    if content:
                        context_parts.append(f"{speaker}: {content}")
            except Exception:
                pass

        # 添加当前触发消息
        if trigger_message:
            context_parts.append(f"{source_user}: {trigger_message}")

        return "\n".join(context_parts) if context_parts else trigger_message

    async def _generate_tags(
        self,
        usage_context: str,
        trigger_message: str,
        source_user: str,
        trigger_emotion: str,
    ) -> list[str]:
        """使用 LLM 生成表情包标签（基于使用情境）。"""
        if self._provider_async is None:
            # 无 provider 时，从使用情境中提取简单关键词
            return self._extract_simple_tags(usage_context)

        try:
            prompt = _TAG_EXTRACTION_PROMPT.format(
                usage_context=usage_context or "",
                trigger_message=trigger_message or "",
                source_user=source_user or "",
                trigger_emotion=trigger_emotion or "",
            )

            from sirius_chat.providers.base import GenerationRequest

            request = GenerationRequest(
                model=self._model_name,
                system_prompt=prompt,
                messages=[{"role": "user", "content": "提取标签"}],
                temperature=0.2,
                max_tokens=128,
                purpose="sticker_tag_extract",
            )

            import time

            t0 = time.perf_counter()
            raw = await self._provider_async.generate_async(request)
            duration_ms = round((time.perf_counter() - t0) * 1000, 2)

            if self._token_callback is not None:
                try:
                    self._token_callback(
                        task_name="sticker_tag_extract",
                        model_name=self._model_name,
                        group_id=source_user or "",
                        request=request,
                        duration_ms=duration_ms,
                    )
                except Exception:
                    pass

            if "```json" in raw:
                raw = raw.split("```json")[1].split("```")[0]
            elif "```" in raw:
                raw = raw.split("```")[1].split("```")[0]
            data = json.loads(raw.strip())
            if not isinstance(data, dict):
                raise ValueError(f"LLM 返回的不是 JSON 对象: {type(data).__name__}")
            tags = list(data.get("tags", []))
            if not tags:
                raise ValueError("LLM 返回的 tags 为空")
            return tags[:5]
        except Exception as exc:
            logger.warning("生成表情包标签失败: %s", exc)
            raise

    @staticmethod
    def _extract_simple_tags(text: str) -> list[str]:
        """从文本中提取简单标签（无 LLM 时的降级方案）。"""
        # 简单的关键词映射
        emotion_keywords = {
            "开心": "开心", "高兴": "开心", "快乐": "开心", "哈哈": "开心",
            "难过": "难过", "伤心": "难过", "悲伤": "难过", "哭": "难过",
            "生气": "生气", "愤怒": "生气", "恼火": "生气",
            "无奈": "无奈", "无语": "无奈", "尴尬": "尴尬",
            "累": "疲惫", "疲惫": "疲惫", "困": "疲惫",
            "惊讶": "惊讶", "震惊": "惊讶", "哇": "惊讶",
        }
        style_keywords = {
            "猫": "猫咪", "狗": "狗狗", "动物": "动物",
            "萌": "可爱", "可爱": "可爱", "Q": "可爱",
            "沙雕": "沙雕", "搞笑": "沙雕", "逗": "沙雕",
            "正经": "正经", "严肃": "正经",
        }

        tags: list[str] = []
        text_lower = text.lower()

        for kw, tag in emotion_keywords.items():
            if kw in text_lower and tag not in tags:
                tags.append(tag)

        for kw, tag in style_keywords.items():
            if kw in text_lower and tag not in tags:
                tags.append(tag)

        # 如果标签太少，添加一些通用标签
        if len(tags) < 2:
            tags.append("日常")

        return tags[:5]

    def batch_learn_from_cognition_cache(
        self,
        image_caption_cache: dict[str, str],
        image_path_resolver: Any,
    ) -> list[StickerRecord]:
        """从 cognition analyzer 的缓存中批量学习。

        这是一个同步方法，用于后台任务定期调用。
        实际的添加操作是同步的（embedding 计算可能较重）。
        """
        records: list[StickerRecord] = []
        for cache_key, caption in image_caption_cache.items():
            if cache_key in self._learned_ids:
                continue
            if self._indexer.get(cache_key) is not None:
                self._learned_ids.add(cache_key)
                continue

            # 解析文件路径
            file_path = image_path_resolver(cache_key) if image_path_resolver else ""
            if not file_path:
                continue

            record = StickerRecord(
                sticker_id=cache_key,
                file_path=file_path,
                caption=caption,
                usage_context="",
                trigger_message="",
                trigger_emotion="",
                source_user="",
                source_group="",
                discovered_at=datetime.now(timezone.utc).isoformat(),
                tags=self._extract_simple_tags(caption),
                novelty_score=1.0,
            )

            # 同步添加（embedding 计算）
            self._indexer.add(record)
            self._learned_ids.add(cache_key)
            records.append(record)

        if records:
            logger.info("批量学习 %d 个表情包", len(records))
        return records
