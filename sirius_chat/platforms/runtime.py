"""SiriusChat v1.0 EmotionalGroupChatEngine 运行时封装。

职责：
    - 根据环境变量/配置创建 provider
    - 创建并管理 EmotionalGroupChatEngine 实例
    - 加载/保存引擎状态
    - 支持延迟初始化（向导完成后再创建引擎）
"""

from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path
from typing import Any

from sirius_chat.core.emotional_engine import create_emotional_engine
from sirius_chat.core.emotional_engine import EmotionalGroupChatEngine
from sirius_chat.core.persona_store import PersonaStore
from sirius_chat.providers.routing import AutoRoutingProvider, ProviderConfig
from sirius_chat.skills.registry import SkillRegistry
from sirius_chat.skills.executor import SkillExecutor
from sirius_chat.memory.diary.vector_store import DiaryVectorStore
from sirius_chat.persona_config import PersonaConfigPaths, PersonaExperienceConfig
from sirius_chat.token.store import TokenUsageStore

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
        self.global_data_path = Path(global_data_path).resolve() if global_data_path else self.work_path
        self.plugin_config = dict(plugin_config or {})
        self._engine: EmotionalGroupChatEngine | None = None
        self._running = False
        self.token_store = TokenUsageStore(
            self.work_path / "token" / "token_usage.db",
            session_id="default",
        )

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
            pass
        return "小星"

    def is_ready(self) -> bool:
        """检查引擎是否已就绪（provider + persona 均配置完成）。"""
        if not self.has_provider_config():
            LOG.warning("引擎未就绪: 未配置 Provider。请在 WebUI 的「Provider 配置」页面添加 API Key，或在 data/providers/provider_keys.json 中配置。")
            return False
        if not self.has_persona():
            LOG.warning("引擎未就绪: 未找到人格配置。请在 WebUI 的「人格配置」页面保存人格，或检查 %s/engine_state/persona.json 是否存在。", self.work_path)
            return False
        try:
            _ = self.engine
            return True
        except Exception as exc:
            LOG.warning("引擎未就绪: 引擎初始化失败: %s", exc)
            return False

    def _build_provider(self) -> AutoRoutingProvider | None:
        # 1) 优先从全局 ProviderRegistry 持久化加载（多人格架构）
        try:
            from sirius_chat.providers.routing import ProviderRegistry
            registry = ProviderRegistry(self.global_data_path)
            loaded = registry.load()
            if loaded:
                return AutoRoutingProvider(loaded)
        except Exception as exc:
            LOG.debug("全局 ProviderRegistry 加载失败: %s", exc)

        # 2) 回退到人格目录（兼容旧版单人格架构）
        try:
            from sirius_chat.providers.routing import ProviderRegistry
            registry = ProviderRegistry(self.work_path)
            loaded = registry.load()
            if loaded:
                return AutoRoutingProvider(loaded)
        except Exception as exc:
            LOG.debug("人格级 ProviderRegistry 加载失败: %s", exc)

        # 3) 从插件配置读取（覆盖/补充）
        provider = _build_provider_from_config(self.plugin_config)
        if provider is not None:
            return provider

        # 4) fallback 到环境变量
        return _build_provider_from_env()

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
        """Register a platform bridge so adapter-specific skills can call adapter APIs.

        Multiple bridges can be registered (e.g. napcat + discord).
        The SkillExecutor matches them to skills via adapter_types.
        """
        if self._engine is None:
            return
        executor = getattr(self._engine, "_skill_executor", None)
        if executor is not None:
            executor.set_bridge(adapter_type, bridge)
            LOG.info("平台 bridge 已注入 skill executor: %s → %s", adapter_type, type(bridge).__name__)

    def _load_experience_config(self) -> PersonaExperienceConfig:
        """从人格目录加载 experience.json，回退到默认值。"""
        paths = PersonaConfigPaths(self.work_path)
        try:
            return PersonaExperienceConfig.load(paths.experience)
        except Exception as exc:
            LOG.debug("加载 experience 配置失败，使用默认值: %s", exc)
            return PersonaExperienceConfig()

    def _build_engine(self) -> EmotionalGroupChatEngine:
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
            # v1.0 基础记忆配置
            "basic_memory_hard_limit": int(self.plugin_config.get("basic_memory_hard_limit", exp.basic_memory_hard_limit)),
            "basic_memory_context_window": int(self.plugin_config.get("basic_memory_context_window", exp.basic_memory_context_window)),
            # v1.0 日记记忆配置
            "diary_top_k": int(self.plugin_config.get("diary_top_k", exp.diary_top_k)),
            "diary_token_budget": int(self.plugin_config.get("diary_token_budget", exp.diary_token_budget)),
            # 行为控制
            "sensitivity": float(self.plugin_config.get("sensitivity", 0.5)),
            "expressiveness": {"expressiveness": exp.expressiveness},
            "reply_cooldown_seconds": int(self.plugin_config.get("reply_cooldown_seconds", 12)),
            "max_skill_rounds": int(self.plugin_config.get("max_skill_rounds", 3)),
            "cross_group_memory_enabled": bool(self.plugin_config.get("cross_group_memory_enabled", True)),
            # 后台任务
            "delayed_queue_tick_interval_seconds": int(self.plugin_config.get("delayed_queue_tick_interval_seconds", 3)),
            "proactive_check_interval_seconds": int(self.plugin_config.get("proactive_check_interval_seconds", 60)),
            "proactive_silence_minutes": int(self.plugin_config.get("proactive_silence_minutes", 60)),
            "proactive_active_start_hour": int(self.plugin_config.get("proactive_active_start_hour", 8)),
            "proactive_active_end_hour": int(self.plugin_config.get("proactive_active_end_hour", 23)),
            "memory_promote_interval_seconds": int(self.plugin_config.get("memory_promote_interval_seconds", 300)),
            # Developer proactive private-chat memory conversations
            "proactive_developer_chat_interval_seconds": int(self.plugin_config.get("proactive_developer_chat_interval_seconds", 1800)),
            "proactive_developer_min_silence_seconds": int(self.plugin_config.get("proactive_developer_min_silence_seconds", 120)),
        }

        # 创建向量存储（ChromaDB）
        vector_store = DiaryVectorStore(self.work_path / "diary" / "vector_db")
        if vector_store.available:
            LOG.info("日记向量存储已启用: %s", vector_store._persist_dir)
        else:
            LOG.warning("日记向量存储未启用，将使用纯内存索引")

        engine = create_emotional_engine(
            work_path=self.work_path,
            provider=provider,
            config=config,
            vector_store=vector_store,
        )

        # 尝试恢复状态
        try:
            engine.load_state()
            LOG.info("引擎状态已恢复")
        except Exception as exc:
            LOG.warning("引擎状态恢复失败（首次运行可忽略）: %s", exc)

        # 注入 TokenUsageStore
        engine.token_store = self.token_store

        # 初始化并注入 SKILL runtime
        try:
            self._setup_skill_runtime(engine)
        except Exception as exc:
            LOG.warning("SKILL runtime 初始化失败: %s", exc)

        return engine

    @property
    def engine(self) -> EmotionalGroupChatEngine:
        if self._engine is None:
            self._engine = self._build_engine()
            self._engine.start_background_tasks()
        return self._engine

    async def start(self) -> None:
        if self._running:
            return
        self._running = True
        # 预热引擎：在加载时就完成初始化，避免第一个消息到达时才加载
        if self.has_provider_config() and self.has_persona():
            try:
                _ = self.engine
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

    async def stop(self) -> None:
        self._running = False
        if self._engine is not None:
            try:
                self._engine.stop_background_tasks()
            except Exception as exc:
                LOG.warning("停止后台任务失败: %s", exc)
            try:
                self._engine.save_state()
            except Exception as exc:
                LOG.warning("引擎状态保存失败: %s", exc)
            self._engine = None
        LOG.info("EmotionalGroupChatEngine 已停止")
