from __future__ import annotations

import uuid
from dataclasses import dataclass, field, fields, MISSING
from typing import Any

from sirius_pulse.developer_profiles import metadata_declares_developer
from sirius_pulse.mixins import JsonSerializable
from sirius_pulse.memory.user.unified_models import UnifiedUser
from sirius_pulse.memory.user.unified_manager import UnifiedUserManager
from sirius_pulse.config import TokenUsageRecord


@dataclass(slots=True)
class Message(JsonSerializable):
    role: str
    content: str
    speaker: str | None = None
    nickname: str | None = None
    channel: str | None = None
    channel_user_id: str | None = None
    group_id: str | None = None
    multimodal_inputs: list[dict[str, str]] = field(default_factory=list)
    # 回复策略：always(默认总是回复) / never(只记忆不回复) / auto(自动判断是否需要回复)
    reply_mode: str = "always"
    # Adapter 类型，用于按来源过滤可用 Skill（如 "napcat"）
    adapter_type: str | None = None
    # 发送者类型：human / self_ai / other_ai / system
    sender_type: str = "human"

    @staticmethod
    def _trim_content_tail(content: str) -> str:
        if not content:
            return content
        end = len(content)
        while end > 0 and content[end - 1] in (" ", "\n"):
            end -= 1
        return content[:end]

    def __post_init__(self) -> None:
        self.content = self._trim_content_tail(self.content)


@dataclass(slots=True)
class ReplyRuntimeState(JsonSerializable):
    # 按用户记录最近一次发言时间（ISO 8601）
    user_last_turn_at: dict[str, str] = field(default_factory=dict)
    # 群聊窗口内消息时间序列（ISO 8601）
    group_recent_turn_timestamps: list[str] = field(default_factory=list)
    # 最近一次 AI 回复时间（ISO 8601）
    last_assistant_reply_at: str = ""
    # 滑动窗口内 AI 回复时间序列（用于频率限制）
    assistant_reply_timestamps: list[str] = field(default_factory=list)





@dataclass(slots=True)
class Transcript:
    messages: list[Message] = field(default_factory=list)
    user_memory: UnifiedUserManager = field(default_factory=UnifiedUserManager)
    reply_runtime: ReplyRuntimeState = field(default_factory=ReplyRuntimeState)
    session_summary: str = ""
    orchestration_stats: dict[str, dict[str, int]] = field(default_factory=dict)
    token_usage_records: list[TokenUsageRecord] = field(default_factory=list)

    def add(self, message: Message) -> None:
        message.content = Message._trim_content_tail(message.content)
        self.messages.append(message)

    def add_token_usage_record(self, record: TokenUsageRecord) -> None:
        self.token_usage_records.append(record)

    def remember_participant(
        self,
        *,
        participant: UnifiedUser,
        content: str = "",
        max_recent_messages: int = 5,
        channel: str | None = None,
        channel_user_id: str | None = None,
        group_id: str = "default",
    ) -> None:
        self.user_memory.register_user(participant, group_id=group_id)

    def find_user_by_channel_uid(self, *, channel: str, uid: str, group_id: str = "default") -> UnifiedUser | None:
        user_id = self.user_memory.resolve_user_id(platform=channel, external_uid=uid)
        if user_id is None:
            return None
        return self.user_memory.get_user(user_id, group_id=group_id)

    def _generate_summary(self, archived_messages: list[Message], max_items: int = 8) -> str:
        from sirius_pulse.core.prompt_factory import PromptFactory

        items: list[str] = []
        for message in archived_messages:
            if not message.speaker:
                continue
            text = message.content.replace("\n", " ").strip()
            if not text:
                continue
            items.append(PromptFactory.render_speaker_line(message.speaker, text[:60]))
            if len(items) >= max_items:
                break
        return PromptFactory.render_speaker_lines_summary(items)

    def compress_for_budget(self, *, max_messages: int, max_chars: int) -> None:
        if max_messages <= 0 or max_chars <= 0:
            return

        if len(self.messages) > max_messages:
            archived = self.messages[:-max_messages]
            summary_piece = self._generate_summary(archived)
            if summary_piece:
                if self.session_summary:
                    self.session_summary = f"{self.session_summary} || {summary_piece}"
                else:
                    self.session_summary = summary_piece
            self.messages = self.messages[-max_messages:]

        def _total_chars() -> int:
            # Exclude system messages: they are moved to system_prompt at request-build
            # time and must not inflate the chat-history budget, otherwise large skill
            # results can evict the current user message and cause API errors.
            return sum(
                len(item.content)
                for item in self.messages
                if str(item.role or "").strip().lower() != "system"
            ) + len(self.session_summary)

        while len(self.messages) > 2 and _total_chars() > max_chars:
            archived = [self.messages.pop(0)]
            summary_piece = self._generate_summary(archived, max_items=1)
            if summary_piece:
                if self.session_summary:
                    self.session_summary = f"{self.session_summary} || {summary_piece}"
                else:
                    self.session_summary = summary_piece

        if len(self.session_summary) > max_chars:
            self.session_summary = self.session_summary[-max_chars:]

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dict. Complex fields use custom logic; all other simple
        fields on Transcript are auto-included via reflection so any future
        addition is persisted without touching this method."""
        _CUSTOM = frozenset({"messages", "user_memory", "reply_runtime", "token_usage_records"})
        result: dict[str, Any] = {
            "messages": [msg.to_dict() for msg in self.messages],
            "user_memory": self.user_memory.to_dict(),
            "reply_runtime": self.reply_runtime.to_dict(),
            "token_usage_records": [r.to_dict() for r in self.token_usage_records],
        }
        # Auto-include any simple fields not handled above (forward-compatible)
        for f in fields(self):
            if f.name not in _CUSTOM:
                result[f.name] = getattr(self, f.name)
        return result

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Transcript":
        """Deserialize from dict. Simple fields are loaded reflectively so any
        future field with a default value is picked up automatically."""
        _CUSTOM = frozenset({"messages", "user_memory", "reply_runtime", "token_usage_records"})

        # Auto-load simple fields using reflection
        simple_kwargs: dict[str, Any] = {}
        for f in fields(cls):
            if f.name in _CUSTOM:
                continue
            if f.name in payload:
                simple_kwargs[f.name] = payload[f.name]
            elif f.default is not MISSING:
                simple_kwargs[f.name] = f.default
            elif f.default_factory is not MISSING:  # type: ignore[misc]
                simple_kwargs[f.name] = f.default_factory()  # type: ignore[misc]

        rrt_data = payload.get("reply_runtime", {})
        reply_runtime = (
            ReplyRuntimeState.from_dict(rrt_data)
            if isinstance(rrt_data, dict)
            else ReplyRuntimeState()
        )

        transcript = cls(
            messages=[Message.from_dict(item) for item in payload.get("messages", [])],
            reply_runtime=reply_runtime,
            token_usage_records=[
                TokenUsageRecord.from_dict(item)
                for item in payload.get("token_usage_records", [])
            ],
            **simple_kwargs,
        )

        if "user_memory" in payload:
            transcript.user_memory = UnifiedUserManager.from_dict(payload.get("user_memory", {}))
        else:
            # Backward compatibility for old state files.
            raw_memories = payload.get("participant_memories", {})
            for name, item in raw_memories.items():
                participant = UnifiedUser(
                    name=item.get("name", name),
                    user_id=name,
                    persona=item.get("persona", ""),
                )
                for text in list(item.get("recent_messages", [])):
                    transcript.remember_participant(
                        participant=participant,
                        content=text,
                        max_recent_messages=64,
                    )

        return transcript

    def as_chat_history(self) -> list[dict[str, str]]:
        from sirius_pulse.core.prompt_factory import PromptFactory

        history: list[dict[str, str]] = []
        for message in self.messages:
            if message.speaker:
                content = PromptFactory.render_speaker_line(message.speaker, message.content)
            else:
                content = message.content
            if message.multimodal_inputs:
                content = PromptFactory.append_multimodal_descriptions(content, message.multimodal_inputs)
            history.append({"role": message.role, "content": content})
        return history
