"""工具拿到的引擎上下文：按领域拆成的小接口。

被动工具的 ``create_background_tasks(ctx)`` / ``create_triggers(ctx)``，以及内置工具的
``engine_context`` 参数，拿到的都是同一个实现了 :class:`ToolEngineContext` 的对象。
:class:`ToolEngineContext` 是下面各个领域接口的合集，方法集合不变；工具应当只依赖
自己真正用到的那几个领域，例如只读写自己数据的工具注解成 :class:`WorkspaceAccess`，
需要排期后台回合的注解成 ``AgentTurnAccess``。

实现见 :mod:`sirius_pulse.core.tool_engine_context`（每个领域一个 mixin）。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Callable, Protocol

if TYPE_CHECKING:
    from sirius_pulse.life.clock import DaySchedule
    from sirius_pulse.tools.models import ToolInvocationContext


class WorkspaceAccess(Protocol):
    """她自己的工作区：人格目录、配置、数据存储、内心日志与事件。"""

    def get_work_path(self) -> str:
        """获取当前人格的工作目录，用于把产出收敛到她自己的工作区。"""
        ...

    def get_config_value(self, key: str, default: Any = None) -> Any:
        """读取引擎配置项。"""
        ...

    def get_data_store(self, tool_name: str) -> Any:
        """获取指定 TOOL 的持久化数据存储。"""
        ...

    def get_persona(self) -> Any:
        """获取当前人格实例。"""
        ...

    def get_expressiveness(self) -> float:
        """获取当前人格的表达性（0-1），用于调整自主阈值。"""
        ...

    def log_inner_thought(self, text: str) -> None:
        """记录引擎内部日志（内心活动）。"""
        ...

    async def emit_event(self, event_type: str, data: dict[str, Any]) -> bool:
        """通过引擎事件总线发送事件，返回是否交给事件总线。"""
        ...


class ToolingAccess(Protocol):
    """工具系统本身：注册表、执行器、给提示词用的工具描述。"""

    @property
    def tool_registry(self) -> Any:
        """当前 ToolRegistry 实例。"""
        ...

    @property
    def tool_executor(self) -> Any:
        """当前 ToolExecutor 实例。"""
        ...

    def get_tool_descriptions(
        self, caller_is_developer: bool = False, adapter_type: str | None = None
    ) -> str:
        """获取当前可用的 TOOL 描述文本（用于注入 prompt）。"""
        ...


class GenerationAccess(Protocol):
    """直接调模型生成一段文本或一张图，不经过工具循环。"""

    async def generate_text(
        self,
        system_prompt: str,
        messages: list[dict[str, Any]],
        group_id: str,
        task_name: str = "passive_tool",
        **kwargs: Any,
    ) -> str:
        """调用 LLM 生成文本。"""
        ...

    async def generate_image(
        self,
        *,
        prompt: str,
        reference_images: list[tuple[str, bytes]] | None = None,
        timeout_seconds: float = 300.0,
    ) -> dict[str, Any]:
        """后台生图：返回 ``{"image_bytes": bytes, "mime_type": str}``。"""
        ...


class AgentTurnAccess(Protocol):
    """让她自己再跑一轮带工具的回合（按工作模式）：定时任务、后台结果回调、自主回合、日程里的工作。"""

    async def generate_scheduled_message(
        self,
        *,
        job: dict[str, Any],
        command_output: str,
        group_id: str,
        user_id: str,
        user_name: str,
        adapter_type: str,
        caller_is_developer: bool = False,
    ) -> dict[str, Any]:
        """Generate a proactive message with the normal multi-round tool loop."""
        ...

    async def handle_background_tool_result(
        self,
        *,
        callback_id: str,
        tool_name: str,
        outcome: str,
        request_summary: str = "",
        deliver_hint: str = "",
        artifact_path: str = "",
        error: str = "",
        output: str = "",
        group_id: str,
        user_id: str = "",
        user_name: str = "",
        adapter_type: str = "",
        caller_is_developer: bool = False,
        self_initiated: bool = False,
        artifact_delivery_blocked: bool = False,
        before_tool: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        """把结果交回 Agent 的内部工具回合，不自动向聊天发送产物或正文。

        回合按工作模式跑：产物用 ``group_file_exec`` 显式发送，对群里说话用
        ``send_midway_msg``（``self_initiated=True`` 的回合不提供该入口）。
        返回 status=completed/deferred；before_tool 在任何工具执行前保存重放边界。
        """
        ...

    async def run_autonomous_turn(
        self,
        *,
        kind: str,
        seed: str,
        group_id: str,
        task_name: str = "autonomy_generate",
        why: str = "",
        intention_id: str = "",
        resolution: str = "",
        notebook_enabled: bool = True,
    ) -> dict[str, Any]:
        """执行一次无会话上下文的自主回合，返回人格自述的产出。"""
        ...

    async def run_life_work_turn(
        self,
        *,
        activity: str,
        block_end: str,
        project: dict[str, str],
        recent_steps: list[str],
        work_tail: str,
        state_line: str,
        group_id: str,
        task_name: str = "autonomy_generate",
    ) -> dict[str, Any]:
        """在她自己排的日程段里推进一个项目，返回人格自述的产出。"""
        ...


class DeliveryAccess(Protocol):
    """对外说话：往聊天里投递消息、分享与表情包，以及她能说给谁听。"""

    async def dispatch_proactive_message(
        self,
        *,
        group_id: str,
        text: str,
        adapter_type: str = "",
        event_id: str = "",
        image_path: str = "",
        reply_references: list[dict[str, Any]] | None = None,
        sticker_names: list[str] | None = None,
        poke_user_ids: list[str] | None = None,
        record_delivery: bool = False,
    ) -> bool:
        """Return True only for confirmed delivery; optionally record ACKed text in history."""
        ...

    async def deliver_share(
        self,
        *,
        audience: str,
        text: str,
        event_id: str = "",
        adapter_type: str = "",
    ) -> bool:
        """把一条自主分享投递到指定会话；不可达时返回 False。"""
        ...

    def queue_pending_message(self, group_id: str, text: str, adapter_type: str = "") -> None:
        """将待发送消息放入引擎的待处理队列。"""
        ...

    def list_audiences(self) -> list[Any]:
        """列出她可以主动说给谁听的候选会话。"""
        ...

    async def send_sticker_by_names(self, group_id: str, names: list[str]) -> dict[str, Any]:
        """从候选名字里挑一个表情包发到指定会话。"""
        ...

    def list_sticker_names(self) -> list[str]:
        """当前人格可用的表情包名称。"""
        ...

    def get_current_adapter_type(self) -> str:
        """获取当前活跃的适配器类型。"""
        ...

    def is_qq_bot_group_admin(self, group_id: str) -> bool:
        """Bot 是否是这个 QQ 群的管理员（admin_required 工具据此放行）。"""
        ...

    def activate_private_group(self, group_id: str) -> None:
        """将私聊群组标记为活跃（以便延迟队列轮询）。"""
        ...


class ConversationAccess(Protocol):
    """会话与记忆：活跃的聊天、最近的消息、写入记忆与群状态。"""

    def get_active_groups(self) -> list[str]:
        """获取当前活跃的群组 ID 列表。"""
        ...

    def get_recent_messages(self, group_id: str, n: int = 10) -> list[dict[str, Any]]:
        """只读获取某群最近消息。"""
        ...

    async def add_memory_unit(self, unit: Any) -> bool:
        """把一条记忆单元写入人格记忆。"""
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

    def schedule_notebook_maintenance(self, group_id: str, *, force: bool = False) -> None:
        """请后台稍后整理某个聊天的小本（暂存待整理或明确要求整理时）。"""
        ...


class LifeAccess(Protocol):
    """她自己的生活：心情的起伏与回落，以及她认识的人。"""

    def nudge_mood(
        self, valence_delta: float, arousal_delta: float = 0.0, reason: str = ""
    ) -> None:
        """让她自己生活里的事改变心情（完成一步、被吵醒……），并持久化。"""
        ...

    def settle_mood(self, baseline_shift: float = 0.0) -> None:
        """心情向基线回落；可选地把基线本身微调一点（仅夜间回顾使用）。"""
        ...

    def mood_label(self) -> str:
        """她此刻的心情，一个中文词（如「满足」）。"""
        ...

    def list_acquaintances(self) -> list[Any]:
        """她认识的人（``life.missing.Acquaintance``）：最后出现时间、亲近程度与聊过的会话。"""
        ...

    def get_day_schedule(self) -> DaySchedule:
        """她的作息（几点睡、几点起）。睡着的时段就是不对外发消息的时段。"""
        ...


class PeopleAccess(Protocol):
    """人物与视觉资产：统一人物服务、受管视觉资产服务，以及按调用来源创建的权限上下文。"""

    def get_person_service(self) -> Any:
        """人格内统一人物领域服务。"""
        ...

    def get_visual_service(self) -> Any:
        """人格内受管视觉资产服务。"""
        ...

    def get_person_access(self, invocation_context: ToolInvocationContext | None) -> Any:
        """从可信逐调用来源创建权限上下文，不接受模型自报操作者。"""
        ...


class UserLookupAccess(Protocol):
    """平台用户查询：按平台账号或名字找人，以及 Bot 自己在各平台的身份。"""

    @property
    def user_lookup(self) -> Any:
        """用户查找服务本身。"""
        ...

    def find_user_by_platform_uid(
        self, platform: str, platform_uid: str, group_id: str = ""
    ) -> dict[str, Any] | None:
        """通过平台 UID 查找用户。"""
        ...

    def find_user_by_name(
        self, name: str, group_id: str = "", *, fuzzy: bool = True
    ) -> dict[str, Any] | None:
        """通过显示名或别名查找用户。"""
        ...

    def get_user_info(self, user_id: str, group_id: str = "") -> dict[str, Any] | None:
        """获取用户详细信息。"""
        ...

    def list_users(self, group_id: str = "") -> list[dict[str, Any]]:
        """列出群组中的所有用户。"""
        ...

    def get_bot_id(self) -> str:
        """Bot 自身的 user_id。"""
        ...

    def get_bot_info(self, group_id: str = "") -> dict[str, Any] | None:
        """Bot 自身的详细信息。"""
        ...

    def get_bot_platform_uid(self, platform: str = "") -> str | None:
        """Bot 在指定平台的 UID（如 QQ 号）；为空时取当前活跃平台。"""
        ...

    def get_bot_platform_uids(self) -> dict[str, str]:
        """Bot 在所有平台的 UID。"""
        ...


class ToolEngineContext(
    WorkspaceAccess,
    ToolingAccess,
    GenerationAccess,
    AgentTurnAccess,
    DeliveryAccess,
    ConversationAccess,
    LifeAccess,
    PeopleAccess,
    UserLookupAccess,
    Protocol,
):
    """被动/内置工具与引擎交互的完整上下文：各领域接口的合集。

    由引擎层实现并注入；工具通过它访问引擎能力，而无需直接依赖引擎类。
    新代码请按需依赖具体的领域接口。
    """
