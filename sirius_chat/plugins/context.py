"""Plugin 运行时上下文 —— PluginContext、EngineProxy、AdapterProxy。

Plugin 通过 PluginContext 安全地访问引擎和平台能力，
不直接操作引擎内部状态或 WebSocket 连接。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Protocol

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
        不直接调用 provider，确保 Plugin 输出进入记忆链和 token 统计。
        """
        if self._engine is None:
            return f"[Engine 未绑定] {prompt[:100]}"
        try:
            return await self._engine._generate(
                system_prompt=prompt,
                messages=[],
                group_id=group_id,
                task_name="plugin_generate",
            )
        except Exception as exc:
            logger.error("Plugin %s 调用 _generate 失败: %s", self._plugin_name, exc)
            return f"[生成失败: {exc}]"

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
# AdapterProxy —— 平台能力的安全代理
# ═══════════════════════════════════════════════════════════════════════

class AdapterProxy:
    """平台适配器代理，暴露 NapCat / Discord 等平台的原生 API。

    Plugin 通过 self.ctx.adapter 直接调用这些方法发送消息、管理群成员等。
    无需通过 PluginResponse.text 间接输出——PluginResponse 仅用于告知框架
    指令已处理完毕（或需要人格引擎做风格化生成）。

    设计原则：
        - 每个方法都是轻量代理，直接委托给底层 Adapter
        - 参数签名与底层 Adapter 保持一致，便于跨平台适配
        - 新平台只需实现相同方法签名即可接入
    """

    def __init__(self) -> None:
        self._adapter: Any = None                            # NapCatAdapter 或其他适配器
        self._plugin_name: str = ""

    def _bind(self, adapter: Any, plugin_name: str) -> None:
        """绑定到实际的适配器实例。"""
        self._adapter = adapter
        self._plugin_name = plugin_name

    # ── 消息发送 ──

    async def send_group_msg(
        self, group_id: str, content: str, *, at_user: str | None = None
    ) -> dict[str, Any]:
        """发送群聊消息。

        Args:
            group_id: 群号
            content: 消息文本（支持 CQ 码，如 [CQ:at,qq=xxx]）
            at_user: 需要 @ 的用户 QQ 号（可选，自动拼 CQ 码）
        """
        if self._adapter is None:
            return {"status": "error", "message": "Adapter 未绑定"}
        if at_user:
            content = f"[CQ:at,qq={at_user}] {content}"
        return await self._adapter.send_group_msg(group_id, content)

    async def send_private_msg(
        self, user_id: str, content: str
    ) -> dict[str, Any]:
        """发送私聊消息。"""
        if self._adapter is None:
            return {"status": "error", "message": "Adapter 未绑定"}
        return await self._adapter.send_private_msg(user_id, content)

    async def send_group_image(
        self, group_id: str, file_path: str
    ) -> dict[str, Any]:
        """发送群聊图片。"""
        if self._adapter is None:
            return {"status": "error", "message": "Adapter 未绑定"}
        segment = f"[CQ:image,file=file://{file_path}]"
        return await self._adapter.send_group_msg(group_id, segment)

    async def send_private_image(
        self, user_id: str, file_path: str
    ) -> dict[str, Any]:
        """发送私聊图片。"""
        if self._adapter is None:
            return {"status": "error", "message": "Adapter 未绑定"}
        segment = f"[CQ:image,file=file://{file_path}]"
        return await self._adapter.send_private_msg(user_id, segment)

    # ── 消息操作 ──

    async def delete_msg(self, message_id: str) -> dict[str, Any]:
        """撤回消息。"""
        if self._adapter is None:
            return {"status": "error", "message": "Adapter 未绑定"}
        return await self._adapter.call_api("delete_msg", {"message_id": int(message_id)})

    # ── 群信息 ──

    async def get_group_member_list(self, group_id: str) -> list[dict[str, Any]]:
        """获取群成员列表。"""
        if self._adapter is None:
            return []
        return await self._adapter.get_group_member_list(group_id)

    async def get_group_member_info(
        self, group_id: str, user_id: str, no_cache: bool = False
    ) -> dict[str, Any]:
        """获取单个群成员信息（昵称、群名片、权限等）。"""
        if self._adapter is None:
            return {}
        return await self._adapter.get_group_member_info(group_id, user_id, no_cache=no_cache)

    async def get_group_info(self, group_id: str) -> dict[str, Any]:
        """获取群信息（群名称、成员数等）。"""
        if self._adapter is None:
            return {}
        return await self._adapter.get_group_info(group_id)

    async def get_stranger_info(self, user_id: str) -> dict[str, Any]:
        """获取陌生人信息（QQ昵称等）。"""
        if self._adapter is None:
            return {}
        return await self._adapter.get_stranger_info(user_id)

    # ── 群管理 ──

    async def set_group_kick(
        self, group_id: str, user_id: str, reject_add_request: bool = False
    ) -> dict[str, Any]:
        """踢出群成员。"""
        if self._adapter is None:
            return {"status": "error", "message": "Adapter 未绑定"}
        return await self._adapter.call_api(
            "set_group_kick",
            {"group_id": int(group_id), "user_id": int(user_id),
             "reject_add_request": reject_add_request},
        )

    async def set_group_ban(
        self, group_id: str, user_id: str, duration: int = 1800
    ) -> dict[str, Any]:
        """禁言群成员（duration 秒，0 表示解除）。"""
        if self._adapter is None:
            return {"status": "error", "message": "Adapter 未绑定"}
        return await self._adapter.call_api(
            "set_group_ban",
            {"group_id": int(group_id), "user_id": int(user_id), "duration": duration},
        )

    async def set_group_whole_ban(self, group_id: str, enable: bool = True) -> dict[str, Any]:
        """全员禁言。"""
        if self._adapter is None:
            return {"status": "error", "message": "Adapter 未绑定"}
        return await self._adapter.call_api(
            "set_group_whole_ban", {"group_id": int(group_id), "enable": enable}
        )

    async def set_group_admin(
        self, group_id: str, user_id: str, enable: bool = True
    ) -> dict[str, Any]:
        """设置/取消群管理员。"""
        if self._adapter is None:
            return {"status": "error", "message": "Adapter 未绑定"}
        return await self._adapter.call_api(
            "set_group_admin",
            {"group_id": int(group_id), "user_id": int(user_id), "enable": enable},
        )

    async def set_group_card(
        self, group_id: str, user_id: str, card: str = ""
    ) -> dict[str, Any]:
        """设置群成员名片。"""
        if self._adapter is None:
            return {"status": "error", "message": "Adapter 未绑定"}
        return await self._adapter.call_api(
            "set_group_card",
            {"group_id": int(group_id), "user_id": int(user_id), "card": card},
        )

    async def set_group_name(self, group_id: str, name: str) -> dict[str, Any]:
        """设置群名称。"""
        if self._adapter is None:
            return {"status": "error", "message": "Adapter 未绑定"}
        return await self._adapter.call_api(
            "set_group_name", {"group_id": int(group_id), "group_name": name}
        )

    # ── 文件 ──

    async def upload_group_file(
        self, group_id: str, file_path: str, name: str = "", folder: str = ""
    ) -> dict[str, Any]:
        """上传文件到群文件。"""
        if self._adapter is None:
            return {"status": "error", "message": "Adapter 未绑定"}
        return await self._adapter.upload_group_file(group_id, file_path, name)

    async def upload_private_file(
        self, user_id: str, file_path: str, name: str = ""
    ) -> dict[str, Any]:
        """上传文件到私聊。"""
        if self._adapter is None:
            return {"status": "error", "message": "Adapter 未绑定"}
        return await self._adapter.upload_private_file(user_id, file_path, name)

    # ── 通用 API ──

    async def call_api(self, action: str, params: dict[str, Any]) -> dict[str, Any]:
        """调用适配器的通用 API（用于未封装的 OneBot 动作）。"""
        if self._adapter is None:
            return {"status": "error", "message": "Adapter 未绑定"}
        return await self._adapter.call_api(action, params)


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
    """

    engine: EngineProxy = field(default_factory=EngineProxy)
    adapter: AdapterProxy = field(default_factory=AdapterProxy)
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
            data_store=data_store,
            config=config or {},
        )
        if engine is not None:
            ctx.engine._bind(engine, plugin_name)
        if adapter is not None:
            ctx.adapter._bind(adapter, plugin_name)
        return ctx
