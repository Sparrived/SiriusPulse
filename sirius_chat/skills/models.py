"""Data models for the skill system."""

from __future__ import annotations

import asyncio
import enum
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

logger = logging.getLogger(__name__)

from sirius_chat.config.models import ConfigParameter
from sirius_chat.memory import UserProfile

# Pre-compiled regex for skill-chain template placeholders (${skill_name} / ${skill_name.field})
_TEMPLATE_RE = re.compile(r"\$\{([^}]+)\}")


@dataclass(slots=True)
class SkillContentBlock:
    """Internal content block returned by a skill for model-side consumption."""

    type: str
    value: str
    mime_type: str = ""
    label: str = ""


@dataclass(slots=True)
class SkillParameter(ConfigParameter):
    """Skill 参数定义 —— 继承 ConfigParameter，完全复用公共字段。

    参数结构由 SKILL_META["parameters"] 在 SkillRegistry 中解析生成。
    """

    pass


@dataclass(slots=True)
class SkillResult:
    """Result returned from skill execution."""

    success: bool
    data: Any = None
    error: str = ""
    text_blocks: list[SkillContentBlock] = field(default_factory=list)
    multimodal_blocks: list[SkillContentBlock] = field(default_factory=list)
    internal_metadata: dict[str, Any] = field(default_factory=dict)

    def to_display_text(self) -> str:
        """Convert result to a human-readable text for AI consumption."""
        if not self.success:
            return f"【SKILL执行失败】{self.error}"
        if self.text_blocks:
            lines = [block.value.strip() for block in self.text_blocks if block.value.strip()]
            if lines:
                return "\n".join(lines)
        if isinstance(self.data, dict):
            lines: list[str] = []
            for key, value in self.data.items():
                if key in {
                    "_meta",
                    "metadata",
                    "internal_metadata",
                    "text_blocks",
                    "multimodal_blocks",
                    "multimodal",
                    "attachments",
                }:
                    continue
                if isinstance(value, dict):
                    lines.append(f"{key}:")
                    for k, v in value.items():
                        lines.append(f"  {k}: {v}")
                elif isinstance(value, list):
                    lines.append(f"{key}: {', '.join(str(v) for v in value)}")
                else:
                    lines.append(f"{key}: {value}")
            if lines:
                return "\n".join(lines)
        return str(self.data) if self.data is not None else "执行完成（无返回数据）"

    def to_internal_payload(self) -> dict[str, Any]:
        """Build a structured internal payload for prompt injection."""
        return {
            "success": self.success,
            "text_blocks": [
                {
                    "type": block.type,
                    "value": block.value,
                    "mime_type": block.mime_type,
                    "label": block.label,
                }
                for block in self.text_blocks
            ],
            "multimodal_blocks": [
                {
                    "type": block.type,
                    "value": block.value,
                    "mime_type": block.mime_type,
                    "label": block.label,
                }
                for block in self.multimodal_blocks
            ],
            "internal_metadata": dict(self.internal_metadata),
        }

    @staticmethod
    def from_raw_result(value: Any) -> "SkillResult":
        """Normalize a raw skill return value into SkillResult."""
        if isinstance(value, SkillResult):
            return value
        if not isinstance(value, dict):
            return SkillResult(success=True, data=value)

        text_blocks = SkillResult._extract_content_blocks(
            value.get("text_blocks") or value.get("text") or value.get("texts"),
            default_type="text",
        )
        multimodal_blocks = SkillResult._extract_content_blocks(
            value.get("multimodal_blocks") or value.get("multimodal") or value.get("attachments"),
            default_type="image",
        )
        internal_metadata = value.get("internal_metadata")
        if not isinstance(internal_metadata, dict):
            internal_metadata = {}

        return SkillResult(
            success=bool(value.get("success", True)),
            data=value,
            error=str(value.get("error", "")).strip(),
            text_blocks=text_blocks,
            multimodal_blocks=multimodal_blocks,
            internal_metadata=dict(internal_metadata),
        )

    @staticmethod
    def _extract_content_blocks(raw: Any, *, default_type: str) -> list[SkillContentBlock]:
        blocks: list[SkillContentBlock] = []
        if isinstance(raw, str):
            value = raw.strip()
            if value:
                blocks.append(SkillContentBlock(type=default_type, value=value))
            return blocks
        if not isinstance(raw, list):
            return blocks
        for item in raw:
            if isinstance(item, str):
                value = item.strip()
                if value:
                    blocks.append(SkillContentBlock(type=default_type, value=value))
                continue
            if not isinstance(item, dict):
                continue
            value = str(item.get("value", "")).strip()
            if not value:
                continue
            blocks.append(
                SkillContentBlock(
                    type=str(item.get("type", default_type)).strip() or default_type,
                    value=value,
                    mime_type=str(item.get("mime_type", "")).strip(),
                    label=str(item.get("label", "")).strip(),
                )
            )
        return blocks

    def get_field(self, key: str, default: Any = None) -> Any:
        """Extract a field from dict/list data by key or index."""
        if isinstance(self.data, dict):
            return self.data.get(key, default)
        if isinstance(self.data, list):
            try:
                return self.data[int(key)]
            except (ValueError, IndexError):
                return default
        return default


@dataclass(slots=True)
class SkillDefinition:
    """Complete definition of a loadable skill."""

    name: str
    description: str
    parameters: list[SkillParameter] = field(default_factory=list)
    version: str = "1.0.0"
    developer_only: bool = False
    silent: bool = False
    tags: list[str] = field(default_factory=list)
    adapter_types: list[str] = field(default_factory=list)
    source_path: Path | None = None
    _run_func: Callable[..., Any] | None = field(default=None, repr=False)
    _background_task_factory: Callable[..., Any] | None = field(default=None, repr=False)
    _trigger_factory: Callable[..., Any] | None = field(default=None, repr=False)
    _on_load_factory: Callable[..., Any] | None = field(default=None, repr=False)
    _on_unload_factory: Callable[..., Any] | None = field(default=None, repr=False)

    def get_parameter_schema(self) -> list[dict[str, Any]]:
        """Return parameter definitions as dicts for prompt rendering."""
        schema: list[dict[str, Any]] = []
        for param in self.parameters:
            entry: dict[str, Any] = {
                "name": param.name,
                "type": param.type,
                "description": param.description,
                "required": param.required,
            }
            if not param.required and param.default is not None:
                entry["default"] = param.default
            schema.append(entry)
        return schema

    @property
    def is_passive(self) -> bool:
        """是否为被动 SKILL（拥有后台任务或触发器，不由模型直接调用）。"""
        return self._background_task_factory is not None or self._trigger_factory is not None


class SkillPassiveType(enum.Enum):
    """被动 SKILL 的子类型。"""

    PERIODIC = "periodic"
    TRIGGER = "trigger"
    BOTH = "both"


@dataclass(slots=True)
class BackgroundTaskSpec:
    """描述一个由被动 SKILL 注册的后台定时任务。"""

    name: str
    interval_seconds: float
    task_func: Callable[..., Awaitable[None]]

    async def run_loop(self, running_check: Callable[[], bool]) -> None:
        """在循环中周期性执行 task_func，直到 running_check() 返回 False。"""
        while running_check():
            await asyncio.sleep(self.interval_seconds)
            if not running_check():
                break
            try:
                await self.task_func()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.exception(
                    "BackgroundTaskSpec '%s' 运行异常: %s",
                    self.name,
                    exc,
                )


@dataclass(slots=True)
class TriggerSpec:
    """描述一个由被动 SKILL 注册的事件触发器。

    trigger_func 在每次收到对应事件时被调用，接收事件数据字典。
    """

    name: str
    event_type: str
    trigger_func: Callable[..., Awaitable[None]]


class SkillEngineContext(Protocol):
    """被动 SKILL 与引擎交互的上下文接口。

    由引擎层实现，注入到被动 SKILL 的 create_background_tasks / create_triggers 中。
    被动 SKILL 通过此接口访问引擎能力，而无需直接依赖引擎类。
    """

    @property
    def skill_registry(self) -> Any:
        """当前 SkillRegistry 实例。"""
        ...

    @property
    def skill_executor(self) -> Any:
        """当前 SkillExecutor 实例。"""
        ...

    def get_data_store(self, skill_name: str) -> Any:
        """获取指定 SKILL 的持久化数据存储。"""
        ...

    async def generate_text(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        group_id: str,
        task_name: str = "passive_skill",
        **kwargs: Any,
    ) -> str:
        """调用 LLM 生成文本。"""
        ...

    def queue_pending_message(self, group_id: str, text: str, adapter_type: str = "") -> None:
        """将待发送消息放入引擎的待处理队列。"""
        ...

    async def emit_event(self, event_type: str, data: dict[str, Any]) -> None:
        """通过引擎事件总线发送事件。"""
        ...

    def get_active_groups(self) -> list[str]:
        """获取当前活跃的群组 ID 列表。"""
        ...

    def get_config_value(self, key: str, default: Any = None) -> Any:
        """读取引擎配置项。"""
        ...

    def get_persona(self) -> Any:
        """获取当前人格实例。"""
        ...

    def log_inner_thought(self, text: str) -> None:
        """记录引擎内部日志（内心活动）。"""
        ...

    def add_memory_entry(
        self, group_id: str, user_id: str, role: str, content: str, speaker_name: str = ""
    ) -> None:
        """向基础记忆追加一条记录。"""
        ...

    def record_reply_timestamp(self, group_id: str) -> None:
        """记录回复时间戳，用于冷却追踪。"""
        ...

    def persist_group_state(self, group_id: str) -> None:
        """持久化指定群组的运行时状态。"""
        ...

    def get_skill_descriptions(self, caller_is_developer: bool = False) -> str:
        """获取当前可用的 SKILL 描述文本（用于注入 prompt）。"""
        ...

    def get_current_adapter_type(self) -> str:
        """获取当前活跃的适配器类型。"""
        ...

    def activate_private_group(self, group_id: str) -> None:
        """将私聊群组标记为活跃（以便延迟队列轮询）。"""
        ...


@dataclass(slots=True)
class SkillInvocationContext:
    """Per-call context injected into skills for authorization and auditing."""

    caller: UserProfile | None = None
    developer_profiles: list[UserProfile] = field(default_factory=list)

    @property
    def caller_is_developer(self) -> bool:
        if self.caller is None:
            return False
        return bool(self.caller.metadata.get("is_developer"))

    @property
    def has_declared_developer(self) -> bool:
        return bool(self.developer_profiles)

    @property
    def caller_name(self) -> str:
        if self.caller is None:
            return ""
        return str(self.caller.name).strip()

    @property
    def caller_user_id(self) -> str:
        if self.caller is None:
            return ""
        return str(self.caller.user_id).strip()


class SkillChainContext:
    """Mutable context passed through a single-round skill chain.

    Stores the result of every skill executed in the current round so that
    subsequent skills can reference earlier results via ``${skill_name}`` or
    ``${skill_name.field}`` template placeholders in their parameters.
    """

    def __init__(self) -> None:
        self._results: dict[str, SkillResult] = {}

    def store(self, skill_name: str, result: SkillResult) -> None:
        """Record ``result`` under ``skill_name`` for later template lookup."""
        self._results[skill_name] = result

    def resolve_templates(self, params: dict[str, Any]) -> dict[str, Any]:
        """Return a copy of *params* with ``${...}`` placeholders substituted.

        Supported template formats (case-sensitive skill name):

        * ``${skill_name}`` — replaced with the skill's full display text.
        * ``${skill_name.field}`` — replaced with a single field of a dict
          or list result (list: ``field`` is a 0-based integer index).

        Placeholders that cannot be resolved are left unchanged.
        """

        def _sub(value: str) -> str:
            def _replace(m: re.Match[str]) -> str:
                expr = m.group(1)
                if "." in expr:
                    skill_name, field = expr.split(".", 1)
                else:
                    skill_name, field = expr, None
                result = self._results.get(skill_name)
                if result is None:
                    return m.group(0)  # unresolved — leave as-is
                if field is None:
                    return result.to_display_text()
                v = result.get_field(field)
                return str(v) if v is not None else m.group(0)

            return _TEMPLATE_RE.sub(_replace, value)

        resolved: dict[str, Any] = {}
        for k, v in params.items():
            resolved[k] = _sub(v) if isinstance(v, str) else v
        return resolved

    @property
    def results(self) -> dict[str, SkillResult]:
        return dict(self._results)
