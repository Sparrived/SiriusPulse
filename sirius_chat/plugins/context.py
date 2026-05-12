"""Plugin 运行时上下文 —— PluginContext、EngineProxy。

Plugin 通过 PluginContext 安全地访问引擎和平台能力。
引擎能力通过 EngineProxy 代理，平台能力通过 adapter 直接访问
（adapter 是 BaseAdapter 实例，PluginExecutor 在运行时时注入）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


# ═══════════════════════════════════════════════════════════════════════
# EngineProxy —— 引擎能力的安全代理
# ═══════════════════════════════════════════════════════════════════════

class EngineProxy:
    """引擎代理，暴露 Plugin 可安全调用的引擎能力。

    注意：此代理由 PluginExecutor 在运行时注入，Plugin 不应自行创建。
    """

    def __init__(self) -> None:
        self._engine: Any = None                                 # EmotionalGroupChatEngine 引用
        self._plugin_name: str = ""

    def _bind(self, engine: Any, plugin_name: str) -> None:
        """绑定到实际的引擎实例。"""
        self._engine = engine
        self._plugin_name = plugin_name

    async def generate_text(self, prompt: str, *, group_id: str = "", **kwargs: Any) -> str:
        """调用引擎的 _generate() 生成人格化文本。

        走完整的框架生成链路：模型路由、token 记录、人格注入、语气对齐。
        """
        return await self._engine._generate(
            system_prompt=prompt,
            messages=[],
            group_id=group_id,
            task_name="plugin_generate",
        )

    def get_persona_name(self) -> str:
        """获取当前人格名称。"""
        if self._engine is None:
            return ""
        persona = getattr(self._engine, "persona", None)
        if persona is None:
            return ""
        return getattr(persona, "name", "") or ""

    def get_persona_info(self) -> dict[str, Any]:
        """获取当前人格基本信息。"""
        if self._engine is None:
            return {}
        persona = getattr(self._engine, "persona", None)
        if persona is None:
            return {}
        return {
            "name": getattr(persona, "name", ""),
            "persona_summary": getattr(persona, "persona_summary", ""),
            "personality_traits": getattr(persona, "personality_traits", []),
            "communication_style": getattr(persona, "communication_style", ""),
        }

    def get_engine(self) -> Any:
        """获取原始引擎引用（高级用法，谨慎使用）。"""
        return self._engine

    def emit_event(self, event_type: str, data: dict[str, Any]) -> None:
        """发射引擎事件（用于跨插件通信）。"""
        if self._engine is None:
            return
        try:
            # 依赖引擎内部的 event_bus
            event_bus = getattr(self._engine, "event_bus", None)
            if event_bus is not None:
                from sirius_chat.core.events import SessionEvent, SessionEventType

                try:
                    evt_type = SessionEventType(event_type)
                except ValueError:
                    evt_type = SessionEventType.CUSTOM
                event = SessionEvent(type=evt_type, data=data)
                # 使用同步方式发射（简化）
                import asyncio
                try:
                    loop = asyncio.get_running_loop()
                    loop.create_task(event_bus.emit(event))
                except RuntimeError:
                    pass
        except Exception as exc:
            logger.warning("Plugin %s 发射事件失败: %s", self._plugin_name, exc)


# ═══════════════════════════════════════════════════════════════════════
# PluginDataStore —— 插件独立数据存储
# ═══════════════════════════════════════════════════════════════════════

class PluginDataStore:
    """Plugin 独立的 JSON 文件数据存储。

    每个 Plugin 有独立的 JSON 文件，隔离存储。
    """

    def __init__(self, data_dir: Path, plugin_name: str) -> None:
        import json as _json
        from pathlib import Path as _Path

        self._data_dir = _Path(data_dir)
        self._plugin_name = plugin_name
        self._file = self._data_dir / f"_plugin_{plugin_name}_data.json"
        self._cache: dict[str, Any] = {}
        self._load()

    def _load(self) -> None:
        """从磁盘加载数据。"""
        import json as _json

        if self._file.exists():
            try:
                self._cache = _json.loads(self._file.read_text(encoding="utf-8"))
            except Exception:
                self._cache = {}

    def _save(self) -> None:
        """保存数据到磁盘。"""
        import json as _json

        self._data_dir.mkdir(parents=True, exist_ok=True)
        self._file.write_text(_json.dumps(self._cache, ensure_ascii=False, indent=2), encoding="utf-8")

    def get(self, key: str, default: Any = None) -> Any:
        """读取数据。"""
        return self._cache.get(key, default)

    def set(self, key: str, value: Any) -> None:
        """写入数据并持久化。"""
        self._cache[key] = value
        self._save()

    def delete(self, key: str) -> None:
        """删除数据。"""
        self._cache.pop(key, None)
        self._save()

    def all(self) -> dict[str, Any]:
        """获取所有数据。"""
        return dict(self._cache)


# ═══════════════════════════════════════════════════════════════════════
# PluginContext —— Plugin 执行上下文
# ═══════════════════════════════════════════════════════════════════════

@dataclass
class MessageContext:
    """消息上下文。"""

    group_id: str = ""
    user_id: str = ""
    channel: str = ""
    channel_user_id: str = ""
    message_id: str = ""
    content: str = ""
    speaker_name: str = ""


@dataclass
class PluginContext:
    """Plugin 执行时的完整上下文。

    由 PluginExecutor 在调用 Plugin.execute() 前注入。

    Attributes:
        engine: 引擎代理（EngineProxy），安全调用 _generate() / 事件发射
        adapter: 平台适配器实例（BaseAdapter），直接调用 send_message / API
        message: 当前消息上下文
        data_store: 插件独立数据存储
        config: 插件配置
        plugin_name: 插件名称
    """

    engine: EngineProxy = field(default_factory=EngineProxy)
    adapter: Any = None  # BaseAdapter 实例，由 PluginExecutor 运行时注入
    message: MessageContext = field(default_factory=MessageContext)
    data_store: PluginDataStore | None = None
    config: dict[str, Any] = field(default_factory=dict)
    plugin_name: str = ""

    @property
    def logger(self) -> logging.Logger:
        """获取 Plugin 专用 logger。"""
        return logging.getLogger(f"plugin.{self.plugin_name}")

    @staticmethod
    def create(
        *,
        engine: Any = None,
        adapter: Any = None,
        plugin_name: str = "",
        message: MessageContext | None = None,
        data_store: PluginDataStore | None = None,
        config: dict[str, Any] | None = None,
    ) -> PluginContext:
        """工厂方法：创建 PluginContext 并绑定引擎和适配器。"""
        ctx = PluginContext(
            plugin_name=plugin_name,
            message=message or MessageContext(),
            adapter=adapter,  # 直接赋值，不再通过代理
            data_store=data_store,
            config=config or {},
        )
        if engine is not None:
            ctx.engine._bind(engine, plugin_name)
        return ctx
