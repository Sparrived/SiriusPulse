"""SiriusChat v1.0 EmotionalGroupChatEngine 运行时封装。

职责：
    - 根据环境变量/配置创建 provider
    - 创建并管理 EmotionalGroupChatEngine 实例
    - 加载/保存引擎状态
    - 支持延迟初始化（向导完成后再创建引擎）
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from pathlib import Path
from typing import Any, Iterable

from sirius_pulse.core.emotional_engine import EmotionalGroupChatEngine, create_emotional_engine
from sirius_pulse.core.persona_db import PersonaDatabase
from sirius_pulse.core.persona_store import PersonaStore
from sirius_pulse.embedding.client import EmbeddingClient, create_embedding_client
from sirius_pulse.memory.diary.vector_store import DiaryVectorStore
from sirius_pulse.persona_config import PersonaConfigPaths, PersonaExperienceConfig
from sirius_pulse.providers.amkr import AmkrSettings, load_amkr_settings, load_inference_keys
from sirius_pulse.providers.amkr_sync import (
    AmkrError,
    SyncResult,
    register_persona_tasks_async,
    workspace_for,
)
from sirius_pulse.providers.openai_compatible import OpenAICompatibleProvider
from sirius_pulse.token.token_store import TokenUsageStore
from sirius_pulse.tools.executor import ToolExecutor
from sirius_pulse.tools.mcp_client import MCPClientManager, load_mcp_config
from sirius_pulse.tools.registry import ToolRegistry

LOG = logging.getLogger("sirius.platforms.runtime")
MCP_STARTUP_TIMEOUT_SECONDS = 30.0
# ``EmbeddingClient`` is intentionally synchronous because it is also used by
# synchronous memory code.  Runtime startup must therefore isolate its health
# probe from the persona event loop and bound both a single probe and the full
# warmup window.
EMBEDDING_STARTUP_TIMEOUT_SECONDS = 30.0
EMBEDDING_HEALTH_CHECK_TIMEOUT_SECONDS = 3.0
EMBEDDING_HEALTH_RETRY_SECONDS = 0.5


async def _wait_for_embedding_health(
    client: EmbeddingClient,
    *,
    total_timeout_seconds: float = EMBEDDING_STARTUP_TIMEOUT_SECONDS,
    per_attempt_timeout_seconds: float = EMBEDDING_HEALTH_CHECK_TIMEOUT_SECONDS,
    retry_seconds: float = EMBEDDING_HEALTH_RETRY_SECONDS,
) -> bool:
    """Wait asynchronously for a synchronous embedding health endpoint.

    ``EmbeddingClient.check_health`` uses ``urllib`` and must not run on the
    worker's sole asyncio loop.  A timed-out thread cannot be forcefully killed
    by Python, so a probe timeout ends this warmup attempt instead of spawning
    more concurrent probes; the client's own HTTP timeout bounds the lingering
    thread in normal operation.  Cancellation is intentionally propagated
    immediately to let runtime shutdown/reload proceed.
    """
    total_timeout = max(0.0, float(total_timeout_seconds))
    attempt_timeout = max(0.001, float(per_attempt_timeout_seconds))
    retry_delay = max(0.0, float(retry_seconds))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + total_timeout

    while True:
        remaining = deadline - loop.time()
        if remaining <= 0:
            return False
        try:
            healthy = await asyncio.wait_for(
                asyncio.to_thread(client.check_health),
                timeout=min(attempt_timeout, remaining),
            )
        except asyncio.TimeoutError:
            LOG.warning("Embedding 健康检查超时，结束本次引擎预热")
            return False
        except Exception as exc:
            # The concrete client normally absorbs transport failures, but a
            # custom client must not crash the worker loop or block retry.
            LOG.warning("Embedding 健康检查失败: %s", type(exc).__name__)
            healthy = False
        if healthy:
            return True

        remaining = deadline - loop.time()
        if remaining <= 0:
            return False
        # This is an async cancellation point; do not use time.sleep() during
        # engine lifecycle work.
        await asyncio.sleep(min(retry_delay, remaining))


async def _await_cleanup(awaitable: Any) -> bool:
    """Finish one async teardown operation before propagating cancellation.

    Lifecycle cancellation must stop admission, but it must not abandon an
    already-created resource halfway through its close operation.  The inner
    task is shielded and cancellation is reported to the caller after it has
    finished.  The return value lets a larger teardown continue all remaining
    cleanup steps before raising ``CancelledError``.
    """
    task = asyncio.ensure_future(awaitable)
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    try:
        task.result()
    except asyncio.CancelledError:
        cancelled = True
    return cancelled


def _build_provider(
    settings: AmkrSettings,
    task_names: Iterable[str] = (),
    workspace: str = "",
    inference_key: str = "",
) -> OpenAICompatibleProvider | None:
    """按 AMKR 连接配置构建唯一的 provider。

    未配置凭据时返回 ``None``，由调用方决定如何报告不就绪。

    ``workspace`` 是该 provider 要写入的 AMKR 工作空间。调用方传人格级子空间
    （``<amkr_workspace>/<persona>``），必须与任务注册用的空间一致，否则请求里的
    任务名在 AMKR 侧查不到定义，会被当成普通模型名直连而失败。

    ``inference_key`` 是该空间的**推理 key**，也是这里真正发出去的凭据。它必须是
    作用域凭据而不是 ``settings.api_key``：后者是能增删供应商与 Key 的管理员凭据，
    把它放进每次对话补全的请求头，等于让模型调用路径随时可以升级成管理操作。
    推理 key 缺失时**不回落**到管理员凭据——那会把一个配置疏漏静默变成一次越权，
    宁可让调用方显式拿到未就绪。
    """
    if not settings.configured:
        return None
    if not inference_key.strip():
        return None
    return OpenAICompatibleProvider(
        base_url=settings.base_url,
        api_key=inference_key,
        timeout_seconds=settings.timeout_seconds,
        workspace=workspace or settings.workspace,
        task_names=task_names,
    )


class EngineRuntime:
    """EmotionalGroupChatEngine v1.0 的运行时封装，支持延迟初始化。"""

    def __init__(
        self,
        work_path: str | Path,
        plugin_config: dict[str, Any] | None = None,
        global_data_path: str | Path | None = None,
    ) -> None:
        self.work_path = Path(work_path).resolve()
        self.work_path.mkdir(parents=True, exist_ok=True)
        self.global_data_path = self._resolve_global_data_path(global_data_path)
        self.plugin_config = dict(plugin_config or {})
        self._engine: EmotionalGroupChatEngine | None = None
        self._running = False
        # stop() closes persistent stores, so no engine lifecycle operation may
        # publish a replacement after that terminal transition.
        self._closed = False
        self._embedding_build_failed: bool = False
        self._embedding_last_fail_at: float = 0.0
        self._embedding_fail_count: int = 0
        self._mcp_manager: MCPClientManager | None = None
        self._building_engine: EmotionalGroupChatEngine | None = None
        self._plugin_executor: Any | None = None
        self._plugin_scheduler: Any | None = None
        self._plugin_tasks_started = False
        # 最近一次 AMKR 任务注册的结果，供 WebUI 的「AMKR 运维」页展示。
        self._amkr_sync_result: SyncResult | None = None
        # Serialize lazy initialization and lifecycle transitions. Building an
        # engine starts resources, so concurrent builders must not discard one.
        self._engine_lock = asyncio.Lock()

        # 统一人格数据库：所有存储层共享同一连接
        self.persona_db = PersonaDatabase(self.work_path / "persona.db")
        self.token_store = TokenUsageStore(
            session_id="default",
            conn=self.persona_db.conn,
            batch_size=1,
        )

    def _resolve_global_data_path(self, global_data_path: str | Path | None) -> Path:
        if global_data_path is not None:
            return Path(global_data_path).resolve()
        if self.work_path.parent.name == "personas":
            return self.work_path.parent.parent.resolve()
        return self.work_path

    def has_provider_config(self) -> bool:
        """检查是否已配置有效的 AMKR 连接。"""
        return self._build_provider() is not None

    def has_persona(self) -> bool:
        """检查是否已保存人格配置到磁盘。"""
        return PersonaStore.load(self.work_path) is not None

    def get_persona_name(self) -> str:
        """Return the current persona name, or a fallback if not loaded."""
        try:
            profile = PersonaStore.load(self.work_path)
            if profile and profile.name:
                return profile.name
        except Exception:
            LOG.warning("读取人格 profile 获取 name 失败", exc_info=True)
            pass
        return "小星"

    def is_ready(self) -> bool:
        """检查引擎是否已就绪（AMKR 连接 + persona 均配置完成）。"""
        if not self.has_provider_config():
            LOG.warning(
                "引擎未就绪: 未配置 AMKR 连接。请在 WebUI 的「AMKR 运维」页面填写"
                "服务地址与本地授权 Key，或设置环境变量 SIRIUS_AMKR_BASE_URL / SIRIUS_AMKR_API_KEY。"
            )
            return False
        if not self.has_persona():
            LOG.warning(
                "引擎未就绪: 未找到人格配置。请在 WebUI 的「人格配置」页面保存人格，或检查 %s/engine_state/persona.json 是否存在。",
                self.work_path,
            )
            return False
        # embedding 服务在冷却期内 → 静默返回，避免每秒刷 WARNING
        if self._embedding_build_failed:
            now = time.monotonic()
            cooldown = min(300.0, 30.0 * (2**self._embedding_fail_count))
            if (now - self._embedding_last_fail_at) < cooldown:
                return False
        try:
            # 检查引擎是否已初始化
            if self._engine is None:
                LOG.warning("引擎未就绪: 引擎未初始化")
                return False
            return True
        except Exception as exc:
            LOG.warning("引擎未就绪: 引擎初始化失败: %s", exc)
            return False

    def _amkr_task_names(self) -> set[str]:
        """本框架会当作 AMKR 任务名发送的模型名集合。

        任务名与 AMKR 一致（``cognition_analyze``、``memory_extract`` …），因此
        直接取任务注册表的键。
        """
        from sirius_pulse.core.model_router import _DEFAULT_TASK_REGISTRY

        return set(_DEFAULT_TASK_REGISTRY)

    def _build_provider(self) -> OpenAICompatibleProvider | None:
        """按 AMKR 连接配置构建 provider。

        配置来自 ``global_config.json``，环境变量优先。任务名集合取自编排配置，
        用于决定采样参数是否交给 AMKR 的任务定义；工作空间取本 persona 的子空间，
        与 ``register_amkr_tasks`` 写入的空间保持一致。

        凭据用该空间的**推理 key**（不是 ``settings.api_key``）。这把 key 被 AMKR
        钉死在这个空间上，因此它在模型调用路径上即使泄漏也换不来别的空间，更做不了
        管理操作。空间还没有推理 key 时返回 ``None``（不就绪），不回落。
        """
        settings = load_amkr_settings(self.global_data_path)
        workspace = workspace_for(settings, self.work_path.name)
        inference_key = load_inference_keys(self.global_data_path).get(workspace, "")
        return _build_provider(settings, self._amkr_task_names(), workspace, inference_key)

    async def register_amkr_tasks(self) -> SyncResult:
        """建出本 persona 的工作空间并注册任务名。

        顺序不能反：**先建空间**才能拿到它的面板 key（AMKR 只在创建那一次返回），
        有了 key 运维页才能把该空间的面板嵌进来。

        只创建缺失的任务，已存在的一律不动——模型与采样参数由运维之后在 AMKR
        侧配置。失败不抛异常：注册不成功最多是 AMKR 侧还没有这些任务，而引擎仍
        可能靠直连模型名工作，因此这里只记录结果供 WebUI 展示。
        """
        settings = load_amkr_settings(self.global_data_path)
        if not settings.configured:
            result = SyncResult()
            result.failed["*"] = "尚未配置 AMKR 本地授权 Key（amkr_local_api_key）"
            return result
        try:
            result = await register_persona_tasks_async(
                settings,
                self.work_path.name,
                global_data_path=self.global_data_path,
            )
        except AmkrError as exc:
            result = SyncResult()
            result.failed["*"] = str(exc)
        if result.ok:
            LOG.info(
                "AMKR 任务注册完成: 新建 %d 个，已存在 %d 个",
                len(result.created),
                len(result.existing),
            )
            # 新建的任务名是没有模型的：模型是 AMKR 的权限，本框架从不预设。但
            # 「注册成功」不等于「能用」——没绑模型的任务在推理时会让 AMKR 拿任务
            # 名去找同名模型，然后每个回合回 404。自主行为整整两天什么都没做，
            # 就是因为 autonomy_generate 曾停在这个状态，而日志里只有 404。这里
            # 明确告诉运维还要做什么，别让它看起来像已经配好了。
            if result.created:
                LOG.warning(
                    "AMKR 新建任务尚未绑定模型，在绑定前调用它们会 404: %s。" "请到 AMKR 面板为这些任务各指定一个真实模型。",
                    ", ".join(result.created),
                )
        else:
            LOG.warning("AMKR 任务注册未全部成功: %s", result.failed)
        return result

    def _merge_plugin_config(self, definition: Any) -> None:
        """将 plugins/_config.json 中的运行时配置合并到 definition.permissions。"""
        import json

        plugins_dir = self._plugins_dir()
        config_path = plugins_dir / "_config.json"
        if not config_path.exists():
            return
        try:
            all_config = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception:
            LOG.warning("启动引擎失败", exc_info=True)
            return

        plugin_config = all_config.get(definition.name)
        if not plugin_config:
            return

        perms = definition.permissions
        # 权限字段在 _config.json 中嵌套在 permissions 子对象下
        perm_cfg = plugin_config.get("permissions", {})
        if not isinstance(perm_cfg, dict):
            perm_cfg = {}
        # 只同步 group_blacklist（白名单由主引擎统一管控）。清单的
        # developer_only / hidden_from_intent 是安全下限，持久化配置只能
        # 收紧，不能将其放宽。
        if isinstance(perm_cfg.get("group_blacklist"), list):
            perms.group_blacklist = [str(item) for item in perm_cfg["group_blacklist"]]
        if perm_cfg.get("developer_only") is True:
            perms.developer_only = True
        if perm_cfg.get("hidden_from_intent") is True:
            perms.hidden_from_intent = True
        configured_rate = perm_cfg.get("rate_limit_calls_per_minute")
        if type(configured_rate) is int and 1 <= configured_rate <= 1000:
            perms.rate_limit_calls_per_minute = configured_rate

        # 将用户自定义 settings 写入 definition，供 Executor 注入 ctx.config
        settings = plugin_config.get("settings")
        if isinstance(settings, dict) and settings:
            definition.user_settings = settings

    async def _setup_tool_runtime(self, engine: EmotionalGroupChatEngine) -> None:
        """Discover and attach TOOL registry + executor to the engine."""
        auto_install = bool(self.plugin_config.get("auto_install_tool_deps", True))
        registry = ToolRegistry()
        # Load built-in tools
        builtin_loaded = registry._load_builtin_tools(auto_install_deps=auto_install)
        if builtin_loaded:
            LOG.info("内置 TOOL 已加载 %d 个", builtin_loaded)

        # Load user-defined tools from workspace
        tools_dir = self.work_path / "tools"
        if tools_dir.exists():
            user_loaded = registry.load_from_directory(
                tools_dir,
                auto_install_deps=auto_install,
                include_builtin=False,
            )
            if user_loaded:
                LOG.info("用户 TOOL 已加载 %d 个", user_loaded)

        executor = ToolExecutor(self.work_path)
        mcp_manager = MCPClientManager(load_mcp_config(PersonaConfigPaths(self.work_path).mcp))
        self._mcp_manager = mcp_manager
        try:
            mcp_tools = await asyncio.wait_for(
                mcp_manager.load_tools(reserved_names=set(registry.tool_names)),
                timeout=MCP_STARTUP_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            LOG.warning(
                "MCP TOOL 初始化超时（%.0f 秒），跳过本次 MCP 工具加载: %s",
                MCP_STARTUP_TIMEOUT_SECONDS,
                self.work_path,
            )
            try:
                await mcp_manager.close()
            finally:
                self._mcp_manager = None
            mcp_tools = []
        for tool in mcp_tools:
            registry.register(tool)
        if mcp_tools:
            LOG.info("MCP TOOL 已加载 %d 个", len(mcp_tools))
        engine.set_tool_runtime(
            tool_registry=registry,
            tool_executor=executor,
        )
        LOG.info("TOOL runtime 已挂载，共 %d 个工具", len(registry.tool_names))

    def add_tool_bridge(self, adapter_type: str, bridge: Any) -> None:
        """Register a platform adapter so adapter-specific tools/plugins can call adapter APIs.

        The bridge IS the adapter itself (e.g. NapCatAdapter).
        Stored directly on the engine for plugin access.
        """
        if self._engine is None:
            return

        # Keep platform bridges and proactive-message routing in sync.  The
        # engine only knows platform-neutral destinations; an adapter exposes
        # its current allowlists through optional hooks.  Adapters that do not
        # implement group routing remain available as tool bridges without
        # becoming candidates for blank-target group broadcasts.
        register_adapter = getattr(self._engine, "register_adapter", None)
        if callable(register_adapter):
            group_getter = getattr(bridge, "get_configured_group_ids", None)
            private_getter = getattr(bridge, "get_configured_private_user_ids", None)
            try:
                group_ids = group_getter() if callable(group_getter) else None
            except Exception:
                LOG.debug("读取 adapter 群路由配置失败: %s", type(bridge).__name__, exc_info=True)
                group_ids = None
            try:
                private_user_ids = private_getter() if callable(private_getter) else None
            except Exception:
                LOG.debug("读取 adapter 私聊路由配置失败: %s", type(bridge).__name__, exc_info=True)
                private_user_ids = None
            try:
                register_adapter(
                    bridge,
                    adapter_type=adapter_type,
                    group_ids=group_ids,
                    private_user_ids=private_user_ids,
                )
            except Exception:
                # Tool bridge injection is independent of optional proactive
                # routing.  Do not make a bridge unusable on older/custom
                # engines whose registration hook is incomplete.
                LOG.warning("注册 adapter 主动消息路由失败: %s", type(bridge).__name__, exc_info=True)

        executor = getattr(self._engine, "_tool_executor", None)
        if executor is not None:
            executor.set_bridge(adapter_type, bridge)
            LOG.info("平台 bridge 已注入 tool executor: %s → %s", adapter_type, type(bridge).__name__)
        # 同时直接存储在引擎上，方便 plugin 直接取用
        self._engine._adapter = bridge

        # 将 adapter 注入到所有已加载 Plugin 实例的 ctx 中
        # 定时任务（如 chat_analyzer 的每日分析）需要 adapter 来调用平台 API
        plugin_registry = getattr(self._engine, "_plugin_registry", None)
        if plugin_registry is not None:
            count = 0
            for name in list(plugin_registry.plugin_names):
                instance = plugin_registry.get_instance(name)
                if instance is not None and hasattr(instance, "_ctx") and instance._ctx is not None:
                    instance._ctx.adapter = bridge
                    count += 1
            if count > 0:
                LOG.info("平台 adapter 已注入 %d 个 Plugin 实例", count)

    def _plugins_dir(self) -> Path:
        """Resolve the shared plugin directory for this runtime.

        Persona workers use ``data/personas/<name>`` as ``work_path`` while
        external plugins live beside ``data`` in the workspace root. Keep a
        legacy per-persona directory as a fallback for existing installations.
        """
        # In the normal layout work_path is data/personas/<name>, while the
        # shared plugin source is <workspace>/plugins (beside data/).
        candidates = [
            self.global_data_path.parent / "plugins",
            self.global_data_path / "plugins",
        ]
        for shared in candidates:
            if shared.exists():
                return shared
        # Keep compatibility with older installations that stored plugins in
        # each persona directory.
        return self.work_path / "plugins"

    async def _setup_plugin_runtime(self, engine: "EmotionalGroupChatEngine") -> None:
        """初始化 Plugin 系统：加载插件、注册、注入到引擎。"""
        plugins_dir = self._plugins_dir()
        if not plugins_dir.exists():
            LOG.info("插件目录不存在，跳过 Plugin 初始化: %s", plugins_dir)
            return

        from sirius_pulse.plugins.dispatcher import OutputDispatcher
        from sirius_pulse.plugins.executor import PluginExecutor
        from sirius_pulse.plugins.loader import PluginLoader
        from sirius_pulse.plugins.registry import PluginRegistry

        # 确保插件目录存在
        PluginLoader.ensure_plugins_directory(plugins_dir)

        # 创建注册表
        registry = PluginRegistry()

        # Discover only literal metadata first.  This allows an enablement
        # decision before any external Plugin module is imported or dependency
        # installer is run.
        loader = PluginLoader(plugins_dir)
        metadata_definitions = loader.load_all_definitions(metadata_only=True)

        if not metadata_definitions:
            LOG.info("未发现任何 Plugin")
            return

        from sirius_pulse.plugins.config import get_config_manager

        plugins_config_manager = get_config_manager(plugins_dir)
        # WebUI 可能在另一个请求中修改了配置文件；加载前刷新管理器缓存。
        plugins_config_manager.reload()
        disabled = {
            definition.name
            for definition in metadata_definitions
            if not plugins_config_manager.get_enabled(definition.name)
        }
        if disabled:
            LOG.info("跳过已禁用 Plugin: %s", ", ".join(sorted(disabled)))

        # 导入 Python 类并注册
        persona_data_path = Path(self.work_path) / "plugin_data"
        persona_data_path.mkdir(parents=True, exist_ok=True)

        for metadata in metadata_definitions:
            if metadata.name in disabled:
                continue
            if metadata.source_path is None:
                continue
            try:
                if metadata.dependencies:
                    _ok, failed = await asyncio.to_thread(
                        loader.install_dependencies, metadata.dependencies
                    )
                    if failed:
                        LOG.error(
                            "Plugin 依赖安装失败，跳过 [%s]: %d 项",
                            metadata.name,
                            failed,
                        )
                        continue
                definition = loader.load_definition(metadata.source_path)
                if definition is None:
                    LOG.error("Plugin 缺少可执行入口，跳过 [%s]", metadata.name)
                    continue
                if definition._plugin_class is None:
                    LOG.error("Plugin 类导入失败，跳过 [%s]", metadata.name)
                    continue

                # 合并 plugins/_config.json 中的运行时配置到 definition.permissions
                self._merge_plugin_config(definition)

                registry.register(definition)
            except Exception as exc:
                LOG.error("导入 Plugin 类失败 [%s]: %s", metadata.name, exc)

        if registry.plugin_count == 0:
            LOG.info("未加载任何 Plugin")
            return

        # 创建执行器和调度器
        executor = PluginExecutor(
            registry,
            persona_data_path=persona_data_path,
            engine=engine,
            config_manager=plugins_config_manager,
        )
        self._plugin_executor = executor
        dispatcher = OutputDispatcher()

        # 实例化所有 Plugin
        count = await executor.instantiate_all()
        LOG.info("Plugin 实例化完成: %d/%d", count, registry.plugin_count)

        # 注入到引擎
        engine.set_plugin_runtime(
            plugin_registry=registry,
            plugin_executor=executor,
            plugin_dispatcher=dispatcher,
        )
        LOG.info(
            "Plugin runtime 已挂载，共 %d 个插件: %s",
            registry.plugin_count,
            ", ".join(registry.plugin_names),
        )

        # 创建并启动 PluginScheduler（使 _plugin_events / _plugin_schedule 定时事件生效）
        from sirius_pulse.plugins.scheduler import PluginScheduler, ScheduledTask

        self._plugin_scheduler = PluginScheduler(check_interval=10.0)
        # 通知 executor，供卸载时清理定时任务
        executor.set_scheduler(self._plugin_scheduler)
        from sirius_pulse.plugins.base import PluginBase

        registered_tasks = 0
        for definition in registry.plugin_names:  # type: ignore[assignment]
            inst = registry.get_instance(definition)  # type: ignore[arg-type]
            if inst is None:
                continue
            plugin_def = registry.get(definition)  # type: ignore[arg-type]
            if plugin_def is None:
                continue
            assert isinstance(inst, PluginBase)
            for evt in plugin_def.events:
                if not evt.cron and evt.interval_seconds <= 0:
                    continue
                task = ScheduledTask(
                    name=f"{definition}:{evt.type}",
                    plugin_name=definition,  # type: ignore[arg-type]
                    cron=evt.cron,
                    interval_seconds=evt.interval_seconds,
                    callback=lambda e=evt, i=inst: i.on_event(
                        e.type,
                        {  # type: ignore[misc]
                            "cron": e.cron,
                            "interval_seconds": e.interval_seconds,
                            "description": e.description,
                        },
                    ),
                )
                self._plugin_scheduler.add_task(task)
                registered_tasks += 1
        if registered_tasks > 0:
            LOG.info("PluginScheduler 已注册 %d 个定时任务，等待引擎启动", registered_tasks)

        # Plugin 定时事件与自声明后台任务均在 _ensure_engine() 中启动，
        # 确保引擎的后台生命周期已经建立。

    def _load_experience_config(self) -> PersonaExperienceConfig:
        """从人格目录加载 experience.json，回退到默认值。"""
        paths = PersonaConfigPaths(self.work_path)
        try:
            return PersonaExperienceConfig.load(paths.experience)
        except Exception as exc:
            LOG.debug("加载 experience 配置失败，使用默认值: %s", exc)
            return PersonaExperienceConfig()

    def _build_engine_runtime_config(self, exp: PersonaExperienceConfig) -> dict[str, Any]:
        return {
            # v1.0 日记记忆配置
            "diary_top_k": int(self.plugin_config.get("diary_top_k", exp.diary_top_k)),
            "diary_token_budget": int(
                self.plugin_config.get("diary_token_budget", exp.diary_token_budget)
            ),
            "memory_unit_top_k": int(
                self.plugin_config.get("memory_unit_top_k", exp.memory_unit_top_k)
            ),
            # 行为控制
            "sensitivity": float(self.plugin_config.get("sensitivity", 0.5)),
            "group_reply_strategies": dict(exp.group_reply_strategies),
            "expressiveness": {"expressiveness": exp.expressiveness},
            "reply_cooldown_seconds": int(self.plugin_config.get("reply_cooldown_seconds", 12)),
            "main_model_reply_cooldown_seconds": float(
                self.plugin_config.get(
                    "main_model_reply_cooldown_seconds",
                    exp.main_model_reply_cooldown_seconds,
                )
            ),
            "reply_time_curve_points": exp.reply_time_curve_points,
            "max_tool_rounds": int(self.plugin_config.get("max_tool_rounds", 3)),
            "partial_reply_lead_seconds": float(
                self.plugin_config.get("partial_reply_lead_seconds", 1.5)
            ),
            "cross_group_memory_enabled": bool(
                self.plugin_config.get("cross_group_memory_enabled", True)
            ),
            # 后台任务
            "delayed_queue_tick_interval_seconds": int(
                self.plugin_config.get("delayed_queue_tick_interval_seconds", 3)
            ),
            "memory_promote_interval_seconds": int(
                self.plugin_config.get("memory_promote_interval_seconds", 300)
            ),
            "memory_idle_consolidation_seconds": int(
                self.plugin_config.get("memory_idle_consolidation_seconds", 3600)
            ),
            # 消息前缀过滤
            "message_prefixes": list(self.plugin_config.get("message_prefixes", [])),
            # 输出长度约束
            "max_sentence_chars": int(self.plugin_config.get("max_sentence_chars", 20)),
        }

    async def _build_engine(self) -> "EmotionalGroupChatEngine":
        # 顺序不能反：**先备齐工作空间与它的推理 key，才能建 provider**。
        # provider 发出去的是该空间的推理 key，而它只在建空间那一次返回；先建
        # provider 会在全新安装上永远拿不到凭据。注册本身用的是管理员凭据，因此
        # 这一步不依赖 provider。
        #
        # 在 AMKR 里确保本 persona 的任务已注册（只创建缺失的，不覆盖已有配置）。
        self._amkr_sync_result = await self.register_amkr_tasks()

        provider = self._build_provider()
        if provider is None:
            settings = load_amkr_settings(self.global_data_path)
            workspace = workspace_for(settings, self.work_path.name)
            has_inference_key = bool(load_inference_keys(self.global_data_path).get(workspace))
            if settings.configured and not has_inference_key:
                # 地址与管理员凭据都在，缺的只是这一把作用域凭据。这与「没配
                # AMKR」是两回事，混成一条提示会让运维去翻一个没问题的配置。
                raise RuntimeError(
                    f"工作空间 {workspace} 还没有推理 key，模型调用无法发起。\n"
                    "建空间时 AMKR 会一并返回它，但本空间建于该能力之前（或从只含"
                    "面板 key 的备份恢复），key 已无法重新取回。补救办法：\n"
                    "1) 在本框架的「AMKR 运维」页点「轮换推理 key」（会把旧 key 作废）；\n"
                    f"2) 或直接调 AMKR: POST /api/workspaces/{workspace}/inference-key\n"
                    f'3) 或从 AMKR 配置文件 workspaces."{workspace}".inference_key 取出。'
                )
            raise RuntimeError(
                "未配置 AMKR 连接。请通过以下任一方式配置：\n"
                "1) WebUI 的「AMKR 运维」页面（写入 global_config.json）\n"
                "2) 环境变量: SIRIUS_AMKR_API_KEY（必填）、"
                "SIRIUS_AMKR_BASE_URL、SIRIUS_AMKR_WORKSPACE\n"
                f"当前地址为 {settings.base_url}，缺少本地授权 Key。"
            )

        # 优先从 experience.json 读取记忆配置，回退到 plugin_config
        exp = self._load_experience_config()
        config = self._build_engine_runtime_config(exp)

        # 创建向量存储（ChromaDB）
        vector_store = DiaryVectorStore(self.work_path / "diary" / "vector_db")
        if vector_store.available:
            LOG.info("日记向量存储已启用: %s", vector_store._persist_dir)
        else:
            LOG.warning("日记向量存储未启用，将使用纯内存索引")

        # 创建共享 Embedding 客户端。向量化由 AMKR 提供，本框架不再自己拉起本地
        # 模型服务，因此这里没有「先启动再等就绪」这一步，只做一次可达性预检。
        embedding_client = create_embedding_client(self.global_data_path, self.work_path.name)
        embedding_model = embedding_client.model

        # 指数退避：失败后冷却，避免每次消息都阻塞 60 秒
        now = time.monotonic()
        cooldown = min(300.0, 30.0 * (2**self._embedding_fail_count))
        if self._embedding_build_failed and (now - self._embedding_last_fail_at) < cooldown:
            remaining = int(cooldown - (now - self._embedding_last_fail_at))
            raise RuntimeError(f"Embedding 模型 {embedding_model} 不可用，{remaining}秒后重试。")

        LOG.info("检查 AMKR Embedding 可用性: %s (%s) ...", embedding_model, embedding_client._base_url)
        try:
            embedding_ready = await _wait_for_embedding_health(embedding_client)
        except asyncio.CancelledError:
            # Do not turn shutdown/reload into a cached service failure.
            raise
        if embedding_ready:
            LOG.info("AMKR Embedding 已就绪: %s", embedding_model)
            self._embedding_build_failed = False
            self._embedding_fail_count = 0
        else:
            self._embedding_build_failed = True
            self._embedding_last_fail_at = time.monotonic()
            self._embedding_fail_count = min(self._embedding_fail_count + 1, 4)
            LOG.error(
                "AMKR Embedding 不可用: %s (连续失败 %d 次)",
                embedding_model,
                self._embedding_fail_count,
            )
            raise RuntimeError(
                f"Embedding 模型 {embedding_model} 不可用。请在 AMKR 中确认该模型已配置"
                "（供应商与 Key 都可用），并已加入本工作空间的模型白名单。"
            )

        engine = create_emotional_engine(
            work_path=self.work_path,
            provider=provider,
            config=config,
            vector_store=vector_store,
            embedding_client=embedding_client,
            persona_db_conn=self.persona_db.conn,
        )
        # Keep a reference until _ensure_engine_locked publishes the engine so
        # cancellation during optional runtime setup can still tear it down.
        self._building_engine = engine

        # 尝试恢复状态
        try:
            engine.load_state()
            LOG.info("引擎状态已恢复")
        except Exception as exc:
            LOG.warning("引擎状态恢复失败（首次运行可忽略）: %s", exc)

        # 注入 TokenUsageStore
        engine.token_store = self.token_store
        engine.brain.token_store = self.token_store

        # 初始化并注入 TOOL runtime
        try:
            await self._setup_tool_runtime(engine)
        except Exception as exc:
            LOG.warning("TOOL runtime 初始化失败: %s", exc)

        # 初始化并注入 Plugin runtime（v1.2+）
        try:
            await self._setup_plugin_runtime(engine)
        except Exception as exc:
            LOG.warning("Plugin runtime 初始化失败: %s", exc)

        return engine

    @property
    def engine(self) -> "EmotionalGroupChatEngine | None":
        """获取引擎实例，未初始化时返回 None。"""
        return self._engine

    async def _ensure_engine(self) -> "EmotionalGroupChatEngine":
        """Return the singleton running engine, building it at most once."""
        async with self._engine_lock:
            return await self._ensure_engine_locked()

    async def _ensure_engine_locked(self) -> "EmotionalGroupChatEngine":
        """Build the engine while ``_engine_lock`` is held."""
        if self._closed:
            raise RuntimeError("EngineRuntime 已关闭")
        if self._engine is not None:
            return self._engine

        engine: EmotionalGroupChatEngine | None = None
        try:
            engine = await self._build_engine()
            # Rebuilt engines are published only after their old counterpart has
            # been retired.  Adapters use this marker to reject late old events.
            try:
                setattr(engine, "_runtime_retiring", False)
            except Exception:
                LOG.debug("引擎不支持 runtime retiring 标记", exc_info=True)
            self._engine = engine
            try:
                engine.start_background_tasks()
                if self._plugin_scheduler is not None:
                    await self._plugin_scheduler.start()
                if self._plugin_executor is not None and not self._plugin_tasks_started:
                    try:
                        started = await self._plugin_executor.start_background_tasks(
                            running_check=lambda: bool(
                                engine._bg_running and self._engine is engine
                            )
                        )
                        self._plugin_tasks_started = True
                        if started:
                            LOG.info("Plugin 后台任务已启动: %d 个", started)
                    except Exception:
                        LOG.warning("Plugin 后台任务启动失败", exc_info=True)
            except BaseException:
                raise
            return engine
        except BaseException:
            # Cancellation can arrive while _build_engine is still setting up
            # optional runtimes, before it returns the partially built engine.
            # Promote that private reference temporarily so the normal teardown
            # closes its event bus, plugins, and MCP manager as well.
            if engine is None:
                engine = self._building_engine
            if engine is not None and self._engine is None:
                self._engine = engine
            try:
                # Preserve embedding backoff after a failed build.  Resetting it
                # here would make every adapter/message immediately start
                # another full warmup loop against an unavailable service.
                await self._reload_engine_locked(reset_embedding_backoff=False)
            finally:
                self._building_engine = None
            raise
        finally:
            if engine is not None and self._building_engine is engine:
                self._building_engine = None

    async def rebuild_engine(self) -> "EmotionalGroupChatEngine":
        """Atomically replace the running engine with a fresh instance."""
        async with self._engine_lock:
            if self._closed:
                raise RuntimeError("EngineRuntime 已关闭")
            await self._reload_engine_locked()
            return await self._ensure_engine_locked()

    async def start(self) -> None:
        """Start the runtime and warm a configured engine under one lock.

        A runtime with no provider/persona remains deliberately lazy for setup
        flows.  By contrast, once both prerequisites exist, a failed warmup is
        a failed start: keeping ``_running`` true would make callers believe
        they can attach an adapter to ``None`` and prevent a later retry.
        """
        async with self._engine_lock:
            if self._closed:
                raise RuntimeError("EngineRuntime 已关闭")
            if self._running:
                return
            self._running = True
            # 预热引擎：在加载时就完成初始化，避免第一个消息到达时才加载
            if self.has_provider_config() and self.has_persona():
                try:
                    await self._ensure_engine_locked()
                    LOG.info("EmotionalGroupChatEngine v1.0 已预热启动")
                except BaseException:
                    # ``_ensure_engine_locked`` already tears down any partial
                    # engine.  Reset admission state before propagating the
                    # failure so a caller can safely retry after remediation.
                    self._running = False
                    raise
            else:
                LOG.info("EmotionalGroupChatEngine 已启动（等待配置完成后预热）")

    async def reload_engine(self) -> None:
        """Save and tear down the engine without racing a concurrent build."""
        async with self._engine_lock:
            if self._closed:
                return
            await self._reload_engine_locked()

    async def _reload_engine_locked(self, *, reset_embedding_backoff: bool = True) -> None:
        """Tear down replaceable resources while ``_engine_lock`` is held.

        ``reset_embedding_backoff`` is false only when this method cleans a
        failed build.  In that path the recorded health failure remains the
        throttle for the next initialization attempt.
        """
        cleanup_cancelled = False
        engine = self._engine
        if engine is not None:
            # Stop adapters from admitting late events while the caller is
            # rebuilding and has not yet rebound them to the replacement.
            try:
                setattr(engine, "_runtime_retiring", True)
            except Exception:
                LOG.debug("引擎不支持 runtime retiring 标记", exc_info=True)
            try:
                engine.save_state()
                LOG.info("引擎状态已保存，准备重建")
            except Exception as exc:
                LOG.warning("引擎状态保存失败: %s", exc)

        if self._plugin_scheduler is not None:
            try:
                cleanup_cancelled = (
                    await _await_cleanup(self._plugin_scheduler.stop()) or cleanup_cancelled
                )
            except Exception as exc:
                LOG.warning("PluginScheduler 停止失败: %s", exc)
        if self._plugin_executor is not None:
            try:
                cleanup_cancelled = (
                    await _await_cleanup(self._plugin_executor.unload_all()) or cleanup_cancelled
                )
            except Exception as exc:
                LOG.warning("卸载 Plugin 失败: %s", exc)
        self._plugin_scheduler = None
        self._plugin_executor = None
        self._plugin_tasks_started = False

        if engine is not None:
            try:
                engine.stop_background_tasks()
            except Exception as exc:
                LOG.warning("停止后台任务失败: %s", exc)
            event_bus = getattr(engine, "event_bus", None)
            close_event_bus = getattr(event_bus, "close", None)
            if callable(close_event_bus):
                try:
                    result = close_event_bus()
                    if inspect.isawaitable(result):
                        cleanup_cancelled = await _await_cleanup(result) or cleanup_cancelled
                except Exception as exc:
                    LOG.warning("关闭旧引擎事件总线失败: %s", exc)
            if self._engine is engine:
                self._engine = None
            LOG.info("引擎已标记为重建，下次访问时将重新初始化")

        if self._mcp_manager is not None:
            manager = self._mcp_manager
            self._mcp_manager = None
            try:
                cleanup_cancelled = await _await_cleanup(manager.close()) or cleanup_cancelled
            except Exception as exc:
                LOG.warning("MCP 连接关闭失败: %s", type(exc).__name__)

        self._building_engine = None
        if reset_embedding_backoff:
            # An explicit rebuild is operator intent to retry immediately.
            self._embedding_build_failed = False
            self._embedding_fail_count = 0
        if cleanup_cancelled:
            raise asyncio.CancelledError

    async def stop(self) -> None:
        """Stop all runtime resources after any in-progress build completes."""
        async with self._engine_lock:
            if self._closed:
                return
            # Fence queued ensure/rebuild calls before persistent stores close.
            self._closed = True
            self._running = False
            engine = self._engine
            reload_cancelled = False
            try:
                await self._reload_engine_locked()
            except asyncio.CancelledError:
                # _reload_engine_locked defers cancellation until all resource
                # closes finish; still flush the persistent stores before the
                # terminal cancellation is re-raised.
                reload_cancelled = True

            # Flush stores while the captured engine is still available.  The
            # old code cleared ``self._engine`` first, which skipped cognition.
            if engine is not None and hasattr(engine, "cognition_store"):
                try:
                    engine.cognition_store.flush()
                except Exception as exc:
                    LOG.warning("CognitionEventStore flush 失败: %s", exc)
            if hasattr(self, "token_store") and self.token_store is not None:
                try:
                    self.token_store.flush()
                except Exception as exc:
                    LOG.warning("TokenUsageStore flush 失败: %s", exc)
            if hasattr(self, "persona_db") and self.persona_db is not None:
                try:
                    self.persona_db.close()
                except Exception as exc:
                    LOG.warning("PersonaDatabase 关闭失败: %s", exc)
            if reload_cancelled:
                raise asyncio.CancelledError

        LOG.info("EmotionalGroupChatEngine 已停止")
