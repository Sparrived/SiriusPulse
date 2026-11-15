"""平台层与引擎之间的类型化接口。

平台适配器（目前是 NapCat）只能通过 :class:`PlatformEngine` 里列出的成员使用引擎；
负责构建和启停引擎的运行时（``platforms/runtime.py``）额外可以用
:class:`ManagedEngine` 里的生命周期成员。

以前适配器大量 ``getattr(engine, "_私有属性", 默认值)``：引擎那边一改名，这边不报错，
只是悄悄拿到默认值——例如退役标记读不到，正在被替换的旧引擎会继续往群里发消息。
现在两头都有测试把关（``tests/test_platform_engine_port.py``）：真实引擎必须实现
这里的每个成员，平台代码也只能用这里声明过的成员。

可选钩子（``preview_dispatch``、``observe_message`` 等）仍允许适配器用 ``getattr``
加 ``callable`` 兜底：测试替身常常只实现其中一部分。但名字必须在这里登记过。
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from sirius_pulse.core.delayed_response_queue import DelayedResponseQueue
    from sirius_pulse.core.events import SessionEventBus


class RawModelCaller(Protocol):
    """适配器只用它做转发记录摘要这类一次性调用。"""

    async def raw_call(self, request: Any) -> str:
        ...


@runtime_checkable
class PlatformEngine(Protocol):
    """平台适配器可见的引擎能力。"""

    # ── 注册与状态 ──
    @property
    def runtime_retiring(self) -> bool:
        ...

    def is_ready(self) -> bool:
        ...

    def register_adapter(
        self,
        adapter: Any,
        *,
        adapter_type: str,
        group_ids: list[str] | None = None,
        private_user_ids: list[str] | None = None,
    ) -> None:
        ...

    def unregister_adapter(self, adapter: Any) -> None:
        ...

    def set_platform_self_id(self, platform_key: str, uid: str) -> None:
        ...

    def update_qq_group_members(self, group_id: str, members: list[dict[str, Any]]) -> None:
        ...

    def update_qq_bot_group_admin(self, group_id: str, is_admin: bool) -> None:
        ...

    # ── 消息处理 ──
    async def process_message(
        self, message: Any, participants: list[Any], group_id: str
    ) -> dict[str, Any]:
        ...

    async def observe_message(self, message: Any, participants: list[Any], group_id: str) -> None:
        ...

    def preview_dispatch(
        self, message: Any, participants: list[Any], group_id: str
    ) -> dict[str, Any]:
        ...

    def is_tool_chain_active(self, group_id: str) -> bool:
        ...

    async def inject_tool_chain_message(
        self, message: Any, participants: list[Any], group_id: str
    ) -> bool:
        ...

    brain: RawModelCaller

    # ── 延迟回复与投递 ──
    event_bus: SessionEventBus
    delayed_queue: DelayedResponseQueue

    def get_agent_turn_lock(self, group_id: str) -> asyncio.Lock:
        ...

    async def tick_delayed_queue(
        self,
        group_id: str,
        on_partial_reply: Any | None = None,
        *,
        adapter_type: str | None = None,
        adapter_route_id: str | None = None,
        item_id: str | None = None,
    ) -> list[dict[str, Any]]:
        ...

    def release_delayed_trigger(self, group_id: str, item_id: str) -> None:
        ...

    def record_assistant_message(self, **kwargs: Any) -> None:
        ...

    async def send_stickers_by_names(
        self, group_id: str, names: list[str], *, adapter: Any | None = None
    ) -> dict[str, Any]:
        ...


@runtime_checkable
class ManagedEngine(PlatformEngine, Protocol):
    """运行时（构建、热重建、关停引擎的一方）额外用到的生命周期能力。"""

    @property
    def runtime_retiring(self) -> bool:
        ...

    @runtime_retiring.setter
    def runtime_retiring(self, value: bool) -> None:
        ...

    @property
    def background_running(self) -> bool:
        ...

    token_store: Any
    cognition_store: Any

    def start_background_tasks(self) -> None:
        ...

    def stop_background_tasks(self) -> None:
        ...

    async def wait_background_closed(self) -> None:
        ...

    def save_state(self) -> None:
        ...

    def load_state(self) -> None:
        ...

    def set_tool_runtime(self, *, tool_registry: Any, tool_executor: Any) -> None:
        ...

    def set_plugin_runtime(
        self,
        *,
        plugin_registry: Any | None = None,
        plugin_executor: Any | None = None,
        plugin_dispatcher: Any | None = None,
    ) -> None:
        ...

    def bind_platform_bridge(self, adapter_type: str, bridge: Any) -> int:
        ...


def protocol_members(protocol: type) -> set[str]:
    """一个 Protocol 声明的全部成员名（方法、属性、带注解的字段，含继承来的）。"""
    names: set[str] = set()
    for klass in protocol.__mro__:
        if klass is object or klass is Protocol or getattr(klass, "_is_protocol", False) is False:
            continue
        names.update(getattr(klass, "__annotations__", {}))
        names.update(
            name
            for name, value in vars(klass).items()
            if not name.startswith("_") and (callable(value) or isinstance(value, property))
        )
    return names
