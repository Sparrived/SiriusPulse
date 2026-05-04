"""根据对话语境检索并发送匹配的表情包。"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

SKILL_META = {
    "name": "send_sticker",
    "description": (
        "根据当前对话语境和情绪，从人格专属表情包库中检索最匹配的表情包并发送。"
        "日常对话随时可以调用，表情包可丰富你的情感输出。"
    ),
    "version": "1.0.0",
    "tags": ["image", "emotion", "napcat"],
    "adapter_types": ["napcat"],
    "silent": True,
    "parameters": {
        "emotion_hint": {
            "type": "str",
            "description": "当前情绪倾向（joy/anger/sadness/anxiety/neutral）",
            "required": False,
            "default": "neutral",
        },
    },
}


def _get_sticker_system(data_store: Any) -> Any | None:
    """从 data_store 中获取 sticker 系统实例。"""
    return data_store.get("_sticker_system")


def _set_sticker_system(data_store: Any, system: Any) -> None:
    """将 sticker 系统实例存入 data_store。"""
    data_store.set("_sticker_system", system)


async def run(
    emotion_hint: str = "neutral",
    data_store: Any = None,
    bridge: Any = None,
    chat_context: dict[str, Any] | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    """执行表情包检索和发送。

    从当前聊天上下文中提取情境，检索最匹配的表情包。

    Args:
        emotion_hint: 情绪倾向
        data_store: SKILL 数据存储
        bridge: 平台桥接器（用于发送图片）
        chat_context: 聊天上下文

    Returns:
        {"success": bool, "sticker_id": str, "file_path": str, "caption": str}
    """
    if data_store is None:
        return {"success": False, "error": "data_store 未提供"}

    sticker_system = _get_sticker_system(data_store)
    if sticker_system is None:
        return {"success": False, "error": "表情包系统未初始化"}

    indexer = sticker_system.get("indexer")
    preference_manager = sticker_system.get("preference_manager")
    feedback_observer = sticker_system.get("feedback_observer")

    if indexer is None or preference_manager is None:
        return {"success": False, "error": "表情包系统组件不完整"}

    preference = preference_manager.load()

    # 构建当前情境
    current_context = _build_current_context(chat_context, kwargs)

    # 检索表情包
    record = indexer.search(
        current_context=current_context,
        preference=preference,
        emotion_hint=emotion_hint,
        top_k=20,
        similarity_threshold=0.5,
    )

    if record is None:
        return {"success": False, "error": "未找到匹配的表情包"}

    # 检查文件是否存在
    file_path = Path(record.file_path)
    if not file_path.exists():
        # 尝试在 image_cache 目录下查找
        work_path = sticker_system.get("work_path")
        if work_path:
            alt_path = Path(work_path).parent / "image_cache" / file_path.name
            if alt_path.exists():
                file_path = alt_path
            else:
                return {"success": False, "error": f"表情包文件不存在: {record.file_path}"}

    # 发送图片
    if bridge is None:
        return {"success": False, "error": "bridge 未提供，无法发送图片"}

    try:
        # 复用 send_image 的逻辑
        from sirius_chat.skills.builtin.send_image import run as send_image_run

        result = await send_image_run(
            image_path=str(file_path),
            caption="",
            sub_type="1",
            data_store=data_store,
            bridge=bridge,
            chat_context=chat_context,
            **kwargs,
        )

        if result.get("success"):
            # 记录使用
            preference_manager.record_usage(record.sticker_id, record.tags, emotion_hint)

            # 启动反馈观察（15 秒后）
            if feedback_observer is not None and chat_context:
                group_id = chat_context.get("group_id", "")
                sent_at = datetime.now(timezone.utc).isoformat()
                import asyncio
                asyncio.create_task(
                    feedback_observer.observe(record.sticker_id, group_id, sent_at, wait_seconds=15.0)
                )

            return {
                "success": True,
                "sticker_id": record.sticker_id,
                "file_path": str(file_path),
                "caption": record.caption,
                "tags": record.tags,
            }
        else:
            return {"success": False, "error": result.get("error", "发送失败")}
    except Exception as exc:
        logger.warning("发送表情包失败: %s", exc)
        return {"success": False, "error": str(exc)}


def _build_current_context(
    chat_context: dict[str, Any] | None,
    kwargs: dict[str, Any],
) -> str:
    """从聊天上下文构建当前情境字符串。"""
    parts: list[str] = []

    # 1. 从 kwargs 中提取最近消息
    recent_messages = kwargs.get("recent_messages")
    if recent_messages is None and chat_context is not None:
        recent_messages = chat_context.get("recent_messages")

    if recent_messages:
        for msg in recent_messages:
            speaker = msg.get("speaker", "")
            content = msg.get("content", "")
            if speaker and content:
                parts.append(f"{speaker}: {content}")

    # 2. 当前待回复消息
    current_message = kwargs.get("current_message")
    if current_message is None and chat_context is not None:
        current_message = chat_context.get("current_message")

    if current_message:
        speaker = current_message.get("speaker", "")
        content = current_message.get("content", "")
        if speaker and content:
            parts.append(f"{speaker}: {content}")
        elif content:
            parts.append(content)

    # 3. 意图信息
    intent = kwargs.get("intent")
    if intent is None and chat_context is not None:
        intent = chat_context.get("intent")

    if intent and isinstance(intent, dict):
        intent_type = intent.get("type", "")
        if intent_type:
            parts.append(f"意图: {intent_type}")

    # 4. 情绪信息
    emotion = kwargs.get("emotion")
    if emotion is None and chat_context is not None:
        emotion = chat_context.get("emotion")

    if emotion and isinstance(emotion, dict):
        emotion_label = emotion.get("label", "")
        if emotion_label:
            parts.append(f"情绪: {emotion_label}")

    return "\n".join(parts) if parts else ""


def init_sticker_system(
    work_path: Path | str,
    persona_name: str,
    provider_async: Any | None = None,
    basic_memory: Any | None = None,
    model_name: str = "gpt-4o-mini",
    token_callback: Any | None = None,
) -> dict[str, Any]:
    """初始化表情包系统。

    在 Engine 初始化时调用，创建所有必要的组件。

    Args:
        work_path: 工作目录
        persona_name: 人格名称
        provider_async: LLM provider
        basic_memory: 基础记忆管理器（用于反馈观察）
        model_name: 标签提取和偏好生成使用的模型

    Returns:
        包含所有组件的字典
    """
    from sirius_chat.skills.sticker.indexer import StickerIndexer
    from sirius_chat.skills.sticker.preference import StickerPreferenceManager
    from sirius_chat.skills.sticker.learner import StickerLearner
    from sirius_chat.skills.sticker.feedback import StickerFeedbackObserver

    sticker_work_path = Path(work_path) / "stickers"
    sticker_work_path.mkdir(parents=True, exist_ok=True)

    indexer = StickerIndexer(
        work_path=sticker_work_path,
        persona_name=persona_name,
    )
    indexer.load_from_disk()

    preference_manager = StickerPreferenceManager(
        work_path=sticker_work_path,
        persona_name=persona_name,
        model_name=model_name,
        token_callback=token_callback,
    )

    learner = StickerLearner(
        indexer=indexer,
        provider_async=provider_async,
        model_name=model_name,
        token_callback=token_callback,
    )

    feedback_observer = StickerFeedbackObserver(
        indexer=indexer,
        preference_manager=preference_manager,
        basic_memory=basic_memory,
    )

    return {
        "work_path": str(sticker_work_path),
        "indexer": indexer,
        "preference_manager": preference_manager,
        "learner": learner,
        "feedback_observer": feedback_observer,
    }
