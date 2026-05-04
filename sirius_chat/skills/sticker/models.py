"""表情包数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class StickerRecord:
    """表情包记录，核心是学习"在什么情境下使用这个表情包"。

    同一个 sticker_id 可以对应多条记录（不同使用情境），
    向量存储和检索以 record_id 为主键。
    """

    sticker_id: str          # MD5 哈希（复用 NapCatBridge 的缓存哈希）
    file_path: str           # 本地路径（sticker_cache/ 下）
    caption: str             # 来自 cognition.image_caption 的描述（辅助理解）
    # --- 使用情境（核心） ---
    usage_context: str       # 发送时的前文上下文（前3-5条消息摘要）
    trigger_message: str     # 触发这条表情包的消息内容
    trigger_emotion: str     # 当时的情绪（从 cognition 获取）
    # --- 来源信息 ---
    source_user: str         # 发送者
    source_group: str        # 群号
    discovered_at: str       # ISO 时间（首次见到）
    last_used_at: str | None = None  # 上次被使用的时间
    usage_count: int = 0     # 被当前人格使用次数
    tags: list[str] = field(default_factory=list)  # LLM 提取的标签
    # --- 向量（核心：usage_context_embedding） ---
    usage_context_embedding: list[float] | None = None  # 使用情境的语义向量（检索核心）
    caption_embedding: list[float] | None = None        # 图片描述的语义向量（辅助）
    novelty_score: float = 1.0  # 新鲜度分数（0-1）
    # --- 场景概括（LLM 生成，跨记录共享） ---
    scene_summary: str = ""                                    # 概括性场景描述（100-200字）
    scene_summary_embedding: list[float] | None = None         # 场景描述的语义向量
    scene_generalize_count: int = 0                            # 已概括次数（上限 3）

    @property
    def record_id(self) -> str:
        """唯一记录 ID: sticker_id + usage_context 的 MD5 前 8 位。"""
        import hashlib
        ctx_hash = hashlib.md5(self.usage_context.encode()).hexdigest()[:8]
        return f"{self.sticker_id}_{ctx_hash}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "sticker_id": self.sticker_id,
            "file_path": self.file_path,
            "caption": self.caption,
            "usage_context": self.usage_context,
            "trigger_message": self.trigger_message,
            "trigger_emotion": self.trigger_emotion,
            "source_user": self.source_user,
            "source_group": self.source_group,
            "discovered_at": self.discovered_at,
            "last_used_at": self.last_used_at,
            "usage_count": self.usage_count,
            "tags": list(self.tags),
            "usage_context_embedding": list(self.usage_context_embedding) if self.usage_context_embedding else None,
            "caption_embedding": list(self.caption_embedding) if self.caption_embedding else None,
            "novelty_score": self.novelty_score,
            "scene_summary": self.scene_summary,
            "scene_summary_embedding": list(self.scene_summary_embedding) if self.scene_summary_embedding else None,
            "scene_generalize_count": self.scene_generalize_count,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StickerRecord":
        usage_emb = data.get("usage_context_embedding")
        cap_emb = data.get("caption_embedding")
        scene_emb = data.get("scene_summary_embedding")
        return cls(
            sticker_id=data.get("sticker_id", ""),
            file_path=data.get("file_path", ""),
            caption=data.get("caption", ""),
            usage_context=data.get("usage_context", ""),
            trigger_message=data.get("trigger_message", ""),
            trigger_emotion=data.get("trigger_emotion", ""),
            source_user=data.get("source_user", ""),
            source_group=data.get("source_group", ""),
            discovered_at=data.get("discovered_at", ""),
            last_used_at=data.get("last_used_at"),
            usage_count=int(data.get("usage_count", 0)),
            tags=list(data.get("tags", [])),
            usage_context_embedding=list(usage_emb) if isinstance(usage_emb, list) else None,
            caption_embedding=list(cap_emb) if isinstance(cap_emb, list) else None,
            novelty_score=float(data.get("novelty_score", 1.0)),
            scene_summary=str(data.get("scene_summary", "")),
            scene_summary_embedding=list(scene_emb) if isinstance(scene_emb, list) else None,
            scene_generalize_count=int(data.get("scene_generalize_count", 0)),
        )


@dataclass
class StickerPreference:
    """人格表情包偏好档案。"""

    # 由人格设定自动生成
    preferred_tags: list[str] = field(default_factory=list)
    avoided_tags: list[str] = field(default_factory=list)
    style_weights: dict[str, float] = field(default_factory=dict)

    # 运行时学习
    tag_success_rate: dict[str, float] = field(default_factory=dict)
    user_reactions: dict[str, Any] = field(default_factory=dict)
    novelty_preference: float = 0.5  # 喜新程度（0=恋旧，1=追新）

    # 情绪→标签映射（自动维护）
    emotion_tag_map: dict[str, list[str]] = field(default_factory=dict)

    # 近期高频使用记录（用于模拟"一段时间内偏爱某几个表情包"）
    recent_usage_window: list[dict[str, Any]] = field(default_factory=list)
    # 近期窗口大小（条数）
    recent_window_size: int = 20

    # 群聊学习：标签→成功率（基于群友反馈）
    group_tag_feedback: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "preferred_tags": list(self.preferred_tags),
            "avoided_tags": list(self.avoided_tags),
            "style_weights": dict(self.style_weights),
            "tag_success_rate": dict(self.tag_success_rate),
            "user_reactions": dict(self.user_reactions),
            "novelty_preference": self.novelty_preference,
            "emotion_tag_map": dict(self.emotion_tag_map),
            "recent_usage_window": list(self.recent_usage_window),
            "recent_window_size": self.recent_window_size,
            "group_tag_feedback": dict(self.group_tag_feedback),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StickerPreference":
        return cls(
            preferred_tags=list(data.get("preferred_tags", [])),
            avoided_tags=list(data.get("avoided_tags", [])),
            style_weights=dict(data.get("style_weights", {})),
            tag_success_rate=dict(data.get("tag_success_rate", {})),
            user_reactions=dict(data.get("user_reactions", {})),
            novelty_preference=float(data.get("novelty_preference", 0.5)),
            emotion_tag_map=dict(data.get("emotion_tag_map", {})),
            recent_usage_window=list(data.get("recent_usage_window", [])),
            recent_window_size=int(data.get("recent_window_size", 20)),
            group_tag_feedback=dict(data.get("group_tag_feedback", {})),
        )
