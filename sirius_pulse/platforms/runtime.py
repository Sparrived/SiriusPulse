"""SiriusChat v1.0 EmotionalGroupChatEngine 运行时封装。

职责：
    - 根据环境变量/配置创建 provider
    - 创建并管理 EmotionalGroupChatEngine 实例
    - 加载/保存引擎状态
    - 支持延迟初始化（向导完成后再创建引擎）
"""

from __future__ import annotations

import logging
import os
import time
from pathlib import Path
from typing import Any

from sirius_pulse.core.emotional_engine import EmotionalGroupChatEngine, create_emotional_engine
from sirius_pulse.core.persona_db import PersonaDatabase
from sirius_pulse.core.persona_store import PersonaStore
from sirius_pulse.embedding.client import EmbeddingClient
from sirius_pulse.memory.diary.vector_store import DiaryVectorStore
from sirius_pulse.persona_config import PersonaConfigPaths, PersonaExperienceConfig
from sirius_pulse.providers.routing import AutoRoutingProvider, ProviderConfig
from sirius_pulse.skills.executor import SkillExecutor
from sirius_pulse.skills.registry import SkillRegistry
from sirius_pulse.token.token_store import TokenUsageStore

LOG = logging.getLogger("sirius.platforms.runtime")


def _resolve_api_key(raw: str) -> str:
    text = raw.strip()
    if text.lower().startswith("env:"):
        return os.getenv(text[4:].strip(), "").strip()
    if text.isupper() and " " not in text:
        env_val = os.getenv(text, "").strip()
        if env_val:
            return env_val
    return text


def _build_provider_from_config(config: dict[str, Any]) -> AutoRoutingProvider | None:
    providers = config.get("providers")
    if not providers:
        return None

    entries: dict[str, ProviderConfig] = {}
    for idx, item in enumerate(providers):
        if not isinstance(item, dict):
            continue
        ptype = str(item.get("type", "")).strip()
        api_key = _resolve_api_key(str(item.get("api_key", "")))
        if not ptype or not api_key:
            continue
        cfg = ProviderConfig(
            provider_type=ptype,
            api_key=api_key,
            base_url=str(item.get("base_url", "")).strip(),
            healthcheck_model=str(item.get("healthcheck_model", "")).strip(),
            enabled=bool(item.get("enabled", True)),
            models=list(item.get("models", []) or []),
        )
        entries[f"{ptype}_{idx}"] = cfg

    if not entries:
        return None
    return AutoRoutingProvider(entries)


def _build_provider_from_env() -> AutoRoutingProvider | None:
    """从环境变量构建 provider（快速测试模式）。"""
    ptype = os.getenv("SIRIUS_PROVIDER_TYPE", "openai-compatible").strip()
    api_key = _resolve_api_key(os.getenv("SIRIUS_API_KEY", ""))
    base_url = os.getenv("SIRIUS_BASE_URL", "").strip()
    model = os.getenv("SIRIUS_MODEL", "gpt-4o-mini").strip()

    if not api_key:
        return None

    cfg = ProviderConfig(
        provider_type=ptype,
        api_key=api_key,
        base_url=base_url,
        healthcheck_model=model,
        enabled=True,
        models=[model] if model else [],
    )
    return AutoRoutingProvider({ptype: cfg})


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
        self._embedding_build_failed: bool = False
        self._embedding_last_fail_at: float = 0.0
        self._embedding_fail_count: int = 0
        self._remote_bridge: Any | None = None

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

    def set_remote_bridge(self, bridge: Any) -> None:
        """设置远程存储桥接（助手模式）。

        必须在 start() 之前调用。设置后引擎将使用管家端数据 API 进行持久化。
        """
        self._remote_bridge = bridge

    @property
    def is_remote_mode(self) -> bool:
        """是否处于远程（助手）模式。"""
        return self._remote_bridge is not None

    def has_provider_config(self) -> bool:
        """检查是否已配置有效的 Provider。"""
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
        """检查引擎是否已就绪（provider + persona 均配置完成）。"""
        if not self.has_provider_config():
            LOG.warning(
                "引擎未就绪: 未配置 Provider。请在 WebUI 的「Provider 配置」页面添加 API Key，或在 data/providers/provider_keys.json 中配置。"
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

    def _build_provider(self) -> AutoRoutingProvider | None:
        # 1) 从 ProviderRegistry 加载
        try:
            from sirius_pulse.providers.routing import ProviderRegistry

            registry = ProviderRegistry(self.global_data_path)
            loaded = registry.load()
            if loaded:
                return AutoRoutingProvider(loaded)
        except Exception as exc:
            LOG.debug("ProviderRegistry 加载失败: %s", exc)

        # 2) 从插件配置读取（覆盖/补充）
        provider = _build_provider_from_config(self.plugin_config)
        if provider is not None:
            return provider

        # 3) fallback 到环境变量
        return _build_provider_from_env()

    def _merge_plugin_config(self, definition: Any) -> None:
        """将 plugins/_config.json 中的运行时配置合并到 definition.permissions。"""
        import json

        config_path = self.work_path / "plugins" / "_config.json"
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
        # 只同步 group_blacklist（白名单由主引擎统一管控）
        for key in ("group_blacklist",):
            if key in perm_cfg:
                setattr(perms, key, list(perm_cfg[key]))
        if "developer_only" in perm_cfg:
            perms.developer_only = bool(perm_cfg["developer_only"])
        if "rate_limit_calls_per_minute" in perm_cfg:
            perms.rate_limit_calls_per_minute = int(perm_cfg["rate_limit_calls_per_minute"])

        # 将用户自定义 settings 写入 definition，供 Executor 注入 ctx.config
        settings = plugin_config.get("settings")
        if isinstance(settings, dict) and settings:
            definition.user_settings = settings

    def _setup_skill_runtime(self, engine: EmotionalGroupChatEngine) -> None:
        """Discover and attach SKILL registry + executor to the engine."""
        auto_install = bool(self.plugin_config.get("auto_install_skill_deps", True))
        registry = SkillRegistry()
        # Load built-in skills
        builtin_loaded = registry._load_builtin_skills(auto_install_deps=auto_install)
        if builtin_loaded:
            LOG.info("内置 SKILL 已加载 %d 个", builtin_loaded)

        # Load user-defined skills from workspace
        skills_dir = self.work_path / "skills"
        if skills_dir.exists():
            user_loaded = registry.load_from_directory(
                skills_dir,
                auto_install_deps=auto_install,
                include_builtin=False,
            )
            if user_loaded:
                LOG.info("用户 SKILL 已加载 %d 个", user_loaded)

        executor = SkillExecutor(self.work_path)
        engine.set_skill_runtime(
            skill_registry=registry,
            skill_executor=executor,
        )
        LOG.info("SKILL runtime 已挂载，共 %d 个技能", len(registry.skill_names))

    def add_skill_bridge(self, adapter_type: str, bridge: Any) -> None:
        """Register a platform adapter so adapter-specific skills/plugins can call adapter APIs.

        The bridge IS the adapter itself (e.g. NapCatAdapter).
        Stored directly on the engine for plugin access.
        """
        if self._engine is None:
            return
        executor = getattr(self._engine, "_skill_executor", None)
        if executor is not None:
            executor.set_bridge(adapter_type, bridge)
            LOG.info(
                "平台 bridge 已注入 skill executor: %s → %s", adapter_type, type(bridge).__name__
            )
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

    async def _setup_plugin_runtime(self, engine: "EmotionalGroupChatEngine") -> None:
        """初始化 Plugin 系统：加载插件、注册、注入到引擎。

        Plugin 目录位于项目根：plugins/
        """
        plugins_dir = self.work_path / "plugins"
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

        # 加载插件
        loader = PluginLoader(plugins_dir)
        definitions = loader.load_all_definitions()

        if not definitions:
            LOG.info("未发现任何 Plugin")
            return

        # 导入 Python 类并注册
        persona_data_path = Path(self.work_path) / "plugin_data"
        persona_data_path.mkdir(parents=True, exist_ok=True)

        for definition in definitions:
            if definition.source_path is None:
                continue
            try:
                plugin_class = loader.import_plugin_class(definition.source_path)
                definition._plugin_class = plugin_class

                # 合并 plugins/_config.json 中的运行时配置到 definition.permissions
                self._merge_plugin_config(definition)

                registry.register(definition)
            except Exception as exc:
                LOG.error("导入 Plugin 类失败 [%s]: %s", definition.name, exc)

        if registry.plugin_count == 0:
            LOG.info("未加载任何 Plugin")
            return

        # 创建执行器和调度器
        from sirius_pulse.plugins.config import get_config_manager

        plugins_config_manager = get_config_manager(plugins_dir)
        executor = PluginExecutor(
            registry,
            persona_data_path=persona_data_path,
            engine=engine,
            config_manager=plugins_config_manager,
        )
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
            await self._plugin_scheduler.start()
            LOG.info("PluginScheduler 已启动，注册了 %d 个定时任务", registered_tasks)

    def _load_experience_config(self) -> PersonaExperienceConfig:
        """从人格目录加载 experience.json，回退到默认值。"""
        paths = PersonaConfigPaths(self.work_path)
        try:
            return PersonaExperienceConfig.load(paths.experience)
        except Exception as exc:
            LOG.debug("加载 experience 配置失败，使用默认值: %s", exc)
            return PersonaExperienceConfig()

    async def _build_engine(self) -> "EmotionalGroupChatEngine":
        provider = self._build_provider()
        if provider is None:
            raise RuntimeError(
                "未配置 Provider。请通过以下任一方式配置：\n"
                "1) 环境变量: SIRIUS_PROVIDER_TYPE, SIRIUS_API_KEY, SIRIUS_BASE_URL, SIRIUS_MODEL\n"
                "2) 配置项 providers（列表格式）"
            )

        # 优先从 experience.json 读取记忆配置，回退到 plugin_config
        exp = self._load_experience_config()

        config = {
            # v1.0 日记记忆配置
            "diary_top_k": int(self.plugin_config.get("diary_top_k", exp.diary_top_k)),
            "diary_token_budget": int(
                self.plugin_config.get("diary_token_budget", exp.diary_token_budget)
            ),
            # 行为控制
            "sensitivity": float(self.plugin_config.get("sensitivity", 0.5)),
            "expressiveness": {"expressiveness": exp.expressiveness},
            "reply_cooldown_seconds": int(self.plugin_config.get("reply_cooldown_seconds", 12)),
            "max_skill_rounds": int(self.plugin_config.get("max_skill_rounds", 3)),
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
        }

        # 创建向量存储（ChromaDB）
        vector_store = DiaryVectorStore(self.work_path / "diary" / "vector_db")
        if vector_store.available:
            LOG.info("日记向量存储已启用: %s", vector_store._persist_dir)
        else:
            LOG.warning("日记向量存储未启用，将使用纯内存索引")

        # 创建共享 Embedding 客户端（连接 Embedding 微服务）
        embedding_url = os.environ.get("SIRIUS_EMBEDDING_URL", "http://127.0.0.1:18900")

        # 指数退避：失败后冷却，避免每次消息都阻塞 60 秒
        now = time.monotonic()
        cooldown = min(300.0, 30.0 * (2**self._embedding_fail_count))
        if self._embedding_build_failed and (now - self._embedding_last_fail_at) < cooldown:
            remaining = int(cooldown - (now - self._embedding_last_fail_at))
            raise RuntimeError(f"Embedding 服务不可用 ({embedding_url})，{remaining}秒后重试。")

        embedding_client = EmbeddingClient(base_url=embedding_url)
        LOG.info("等待共享 Embedding 服务就绪: %s ...", embedding_url)
        # 阻塞等待 Embedding 服务就绪（最多 30 秒）
        for _attempt in range(60):
            if embedding_client.check_health():
                LOG.info("共享 Embedding 服务已连接: %s", embedding_url)
                self._embedding_build_failed = False
                self._embedding_fail_count = 0
                break
            time.sleep(0.5)
        else:
            self._embedding_build_failed = True
            self._embedding_last_fail_at = time.monotonic()
            self._embedding_fail_count = min(self._embedding_fail_count + 1, 4)
            LOG.error(
                "共享 Embedding 服务不可用: %s (连续失败 %d 次)",
                embedding_url,
                self._embedding_fail_count,
            )
            raise RuntimeError(
                f"Embedding 服务不可用 ({embedding_url})。"
                "请在 WebUI 检查 Embedding 状态，或手动启动: "
                "python -m sirius_pulse.embedding.server"
            )

        engine = create_emotional_engine(
            work_path=self.work_path,
            provider=provider,
            config=config,
            vector_store=vector_store,
            embedding_client=embedding_client,
            persona_db_conn=self.persona_db.conn,
            remote_bridge=self._remote_bridge,
        )

        # 尝试恢复状态
        if self._remote_bridge is not None:
            # 助手模式：从远程快照恢复状态
            self._restore_from_snapshot(engine)
        else:
            # 本地模式：从磁盘文件恢复
            try:
                engine.load_state()
                LOG.info("引擎状态已恢复")
            except Exception as exc:
                LOG.warning("引擎状态恢复失败（首次运行可忽略）: %s", exc)

        # 注入 TokenUsageStore
        engine.token_store = self.token_store
        engine.brain.token_store = self.token_store

        # 初始化并注入 SKILL runtime
        try:
            self._setup_skill_runtime(engine)
        except Exception as exc:
            LOG.warning("SKILL runtime 初始化失败: %s", exc)

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
        if self._engine is None:
            self._engine = await self._build_engine()
            self._engine.start_background_tasks()
        return self._engine

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        # 预热引擎：在加载时就完成初始化，避免第一个消息到达时才加载
        if self.has_provider_config() and self.has_persona():
            try:
                await self._ensure_engine()
                LOG.info("EmotionalGroupChatEngine v1.0 已预热启动")
            except Exception as exc:
                LOG.warning("引擎预热失败（配置可能不完整）: %s", exc)
        else:
            LOG.info("EmotionalGroupChatEngine 已启动（等待配置完成后预热）")

    def reload_engine(self) -> None:
        """保存当前状态后重建引擎，使配置变更（如模型）立即生效。"""
        if self._engine is not None:
            try:
                self._engine.save_state()
                LOG.info("引擎状态已保存，准备重建")
            except Exception as exc:
                LOG.warning("引擎状态保存失败: %s", exc)
            try:
                self._engine.stop_background_tasks()
            except Exception as exc:
                LOG.warning("停止后台任务失败: %s", exc)
            self._engine = None
            LOG.info("引擎已标记为重建，下次访问时将重新初始化")
        # 重置 embedding 失败缓存，让 reload 后能立即重试
        self._embedding_build_failed = False
        self._embedding_fail_count = 0

    async def stop(self) -> None:
        self._running = False

        # 停止 PluginScheduler（如果有）
        plugin_scheduler = getattr(self, "_plugin_scheduler", None)
        if plugin_scheduler is not None:
            try:
                await plugin_scheduler.stop()
            except Exception as exc:
                LOG.warning("PluginScheduler 停止失败: %s", exc)

        if self._engine is not None:
            try:
                self._engine.stop_background_tasks()
            except Exception as exc:
                LOG.warning("停止后台任务失败: %s", exc)
            if self._remote_bridge is not None:
                # 助手模式：将状态快照推送到管家端
                try:
                    state = self._serialize_engine_state()
                    import asyncio

                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        # 在已有事件循环中创建任务
                        asyncio.ensure_future(self._remote_bridge.save_snapshot(state))
                    else:
                        loop.run_until_complete(self._remote_bridge.save_snapshot(state))
                    LOG.info("引擎状态已推送到管家端")
                except Exception as exc:
                    LOG.warning("引擎状态推送到管家端失败: %s", exc)
                # 停止远程写缓冲
                try:
                    import asyncio

                    loop = asyncio.get_event_loop()
                    if loop.is_running():
                        asyncio.ensure_future(self._remote_bridge.stop())
                    else:
                        loop.run_until_complete(self._remote_bridge.stop())
                except Exception as exc:
                    LOG.warning("远程写缓冲停止失败: %s", exc)
            else:
                # 本地模式：保存到磁盘
                try:
                    self._engine.save_state()
                except Exception as exc:
                    LOG.warning("引擎状态保存失败: %s", exc)
            self._engine = None

        # 关闭统一人格数据库连接
        # 先 flush 所有使用共享连接的缓冲写入，避免数据丢失
        if hasattr(self, "token_store") and self.token_store is not None:
            try:
                self.token_store.flush()
            except Exception as exc:
                LOG.warning("TokenUsageStore flush 失败: %s", exc)
        if self._engine is not None and hasattr(self._engine, "cognition_store"):
            try:
                self._engine.cognition_store.flush()
            except Exception as exc:
                LOG.warning("CognitionEventStore flush 失败: %s", exc)

        if hasattr(self, "persona_db") and self.persona_db is not None:
            try:
                self.persona_db.close()
            except Exception as exc:
                LOG.warning("PersonaDatabase 关闭失败: %s", exc)

        LOG.info("EmotionalGroupChatEngine 已停止")

    # ------------------------------------------------------------------
    # 远程模式：快照加载/保存
    # ------------------------------------------------------------------

    def _restore_from_snapshot(self, engine: EmotionalGroupChatEngine) -> None:
        """助手模式：从远程快照恢复引擎状态。"""
        bridge = self._remote_bridge
        if bridge is None or bridge.snapshot is None:
            LOG.warning("远程快照为空，使用空状态启动")
            return

        try:
            # 1. 恢复 persona
            persona_data = bridge.get_persona()
            if persona_data:
                from sirius_pulse.models.persona import PersonaProfile

                engine.persona = PersonaProfile.from_dict(persona_data)
                LOG.info("从快照恢复 persona: %s", engine.persona.name)

            # 2. 恢复基础记忆
            basic_mem_data = bridge.get_basic_memory_state()
            if basic_mem_data:
                from sirius_pulse.memory.basic import BasicMemoryManager

                engine.basic_memory = BasicMemoryManager.from_dict(basic_mem_data)

            # 3. 恢复工作记忆
            for group_id, entries in bridge.get_working_memories().items():
                if entries:
                    engine.basic_memory.restore_from_snapshot(group_id, entries)

            # 4. 恢复助手情绪
            emotion_data = bridge.get_assistant_emotion()
            if emotion_data:
                for key, value in emotion_data.items():
                    if hasattr(engine.assistant_emotion, key):
                        setattr(engine.assistant_emotion, key, value)

            # 5. 恢复时间戳
            engine._group_last_message_at = dict(bridge.get_group_timestamps())

            # 6. 恢复日记状态
            diary_state = bridge.get_diary_state()
            if diary_state:
                sources = diary_state.get("diarized_sources", {})
                engine.diary_manager._diarized_sources = {
                    gid: set(sids) for gid, sids in sources.items()
                }

            # 7. 恢复归档消息
            for group_id, entries in bridge.get_archives().items():
                if entries:
                    engine.basic_store.restore_archive(group_id, entries)

            # 8. 重新绑定 context_assembler
            from sirius_pulse.memory.context_assembler import ContextAssembler

            engine.context_assembler = ContextAssembler(
                engine.basic_memory,
                engine.diary_manager._retriever,
                profile_manager=getattr(engine, "profile_manager", None),
                is_source_diarized=engine.diary_manager.is_source_diarized,
                memory_unit_retriever=getattr(engine, "memory_unit_manager", None),
                is_source_checkpointed=(
                    engine.memory_unit_manager.is_source_checkpointed
                    if hasattr(engine, "memory_unit_manager")
                    else None
                ),
            )

            LOG.info(
                "从远程快照恢复完成，%d 个群的上下文已加载",
                len(engine.basic_memory.list_groups()),
            )
        except Exception as exc:
            LOG.warning("远程快照恢复部分出错: %s", exc)

        # 9. 同步 experience 和 task_params（取两者中最新的）
        self._sync_configs_from_snapshot(bridge)

    def _sync_configs_from_snapshot(self, bridge: Any) -> None:
        """同步 experience 和 task_params 配置，取两者中最新的。

        管家端和助手端都可能修改这些配置（管家通过 WebUI，助手可能本地改），
        通过 _updated_at 时间戳判断哪个更新，更新的一方覆盖另一方。
        """
        import json
        from datetime import datetime

        def _parse_ts(ts_str: str) -> datetime:
            """解析 ISO 时间戳，缺失时返回 epoch。"""
            if not ts_str:
                return datetime.min
            try:
                return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                return datetime.min

        # --- Experience 同步 ---
        remote_exp = bridge.get_experience()
        if remote_exp:
            local_exp_path = self.work_path / "experience.json"
            local_exp: dict = {}
            if local_exp_path.exists():
                try:
                    local_exp = json.loads(local_exp_path.read_text(encoding="utf-8"))
                except Exception:
                    pass

            remote_ts = _parse_ts(remote_exp.get("_updated_at", ""))
            local_ts = _parse_ts(local_exp.get("_updated_at", ""))

            if remote_ts >= local_ts:
                # 远程更新，写入本地
                from sirius_pulse.persona_config import PersonaExperienceConfig

                exp = PersonaExperienceConfig.from_dict(remote_exp)
                exp.save(local_exp_path)
                # 同步到引擎运行时
                if self._engine is not None:
                    self._engine.config.update(exp.to_dict())
                LOG.info("Experience 配置已从管家端同步（远程更新）")
            else:
                # 本地更新，推送到管家端
                self._push_config_to_butler("experience", local_exp)
                LOG.info("Experience 配置已推送到管家端（本地更新）")

        # --- Task Params 同步 ---
        remote_tp = bridge.get_task_params()
        if remote_tp:
            local_orch_path = self.work_path / "engine_state" / "orchestration.json"
            local_orch: dict = {}
            if local_orch_path.exists():
                try:
                    local_orch = json.loads(local_orch_path.read_text(encoding="utf-8"))
                except Exception:
                    pass

            remote_tp_ts = _parse_ts(remote_tp.get("_updated_at", ""))
            local_tp_ts = _parse_ts(local_orch.get("_updated_at", ""))

            if remote_tp_ts >= local_tp_ts:
                # 远程更新，合并 task_params 字段到本地 orchestration
                for key in (
                    "task_temperatures",
                    "task_max_tokens",
                    "task_timeout",
                    "task_fallback_model",
                ):
                    if key in remote_tp:
                        local_orch[key] = remote_tp[key]
                local_orch_path.parent.mkdir(parents=True, exist_ok=True)
                from sirius_pulse.utils.json_io import atomic_write_json
                atomic_write_json(local_orch_path, local_orch)
                LOG.info("TaskParams 已从管家端同步（远程更新）")
            else:
                # 本地更新，推送 task_params 到管家端
                tp_to_push = {
                    k: local_orch[k]
                    for k in (
                        "task_temperatures",
                        "task_max_tokens",
                        "task_timeout",
                        "task_fallback_model",
                        "_updated_at",
                    )
                    if k in local_orch
                }
                if tp_to_push:
                    self._push_config_to_butler("task_params", tp_to_push)
                    LOG.info("TaskParams 已推送到管家端（本地更新）")

    def _push_config_to_butler(self, config_type: str, data: dict) -> None:
        """将配置推送到管家端（非阻塞）。"""
        bridge = self._remote_bridge
        if bridge is None:
            return
        bridge.write_buffer.add_critical(f"config_{config_type}", data)
        """序列化引擎完整状态，用于推送到管家端。"""
        import dataclasses

        engine = self._engine
        if engine is None:
            return {}

        working_memories: dict[str, list[dict[str, Any]]] = {}
        for group_id in engine.basic_memory.list_groups():
            entries = engine.basic_memory.get_all(group_id)[-100:]
            working_memories[group_id] = [
                {
                    "user_id": e.user_id,
                    "role": e.role,
                    "content": e.content,
                    "timestamp": e.timestamp,
                }
                for e in entries
            ]

        state: dict[str, Any] = {
            "working_memories": working_memories,
            "assistant_emotion": dataclasses.asdict(engine.assistant_emotion),
            "group_timestamps": dict(engine._group_last_message_at),
            "basic_memory": engine.basic_memory.to_dict(),
            "diary_state": {
                "diarized_sources": {
                    gid: list(sids)
                    for gid, sids in engine.diary_manager._diarized_sources.items()
                }
            },
        }

        # Persona
        if hasattr(engine, "persona") and engine.persona:
            state["persona"] = engine.persona.to_dict()

        # 术语表
        glossary_path = self.work_path / "glossary" / "terms.json"
        if glossary_path.exists():
            try:
                import json

                state["glossary"] = json.loads(glossary_path.read_text(encoding="utf-8"))
            except Exception:
                pass

        # 归档消息
        archives: dict[str, list[dict[str, Any]]] = {}
        archive_dir = self.work_path / "archive"
        if archive_dir.exists():
            import json

            for path in archive_dir.glob("*.jsonl"):
                group_id = path.stem
                entries: list[dict[str, Any]] = []
                try:
                    with path.open("r", encoding="utf-8") as f:
                        for line in f:
                            line = line.strip()
                            if line:
                                try:
                                    entries.append(json.loads(line))
                                except json.JSONDecodeError:
                                    continue
                    archives[group_id] = entries[-100:]
                except OSError:
                    continue
        state["archives"] = archives

        return state
