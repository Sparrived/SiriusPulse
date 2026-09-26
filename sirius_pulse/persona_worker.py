"""人格工作进程 — 单个人格的独立运行入口。

职责：
- 加载人格级配置（persona.json / orchestration.json / adapters.json / experience.json）
- 创建 EngineRuntime + NapCatAdapter
- 运行事件循环，定期写入心跳
- 响应 SIGTERM 优雅退出

启动方式::

    python -m sirius_pulse.persona_worker --config data
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

from sirius_pulse.logging_config import configure_logging, setup_log_archival
from sirius_pulse.persona_config import (
    NapCatAdapterConfig,
    PersonaAdaptersConfig,
    PersonaConfigPaths,
    PersonaExperienceConfig,
)
from sirius_pulse.platforms.onebot_v11.napcat.adapter import NapCatAdapter
from sirius_pulse.platforms.runtime import EngineRuntime
from sirius_pulse.utils.json_io import replace_with_retry

LOG = logging.getLogger("sirius.persona_worker")


class PersonaWorker:
    """单个人格的运行时封装。"""

    def __init__(
        self,
        persona_dir: Path | str,
        startup_lock: asyncio.Lock | None = None,
    ) -> None:
        self.persona_dir = Path(persona_dir).resolve()
        self.paths = PersonaConfigPaths(self.persona_dir)
        self._startup_lock = startup_lock
        self._adapters: list[NapCatAdapter] = []
        self._runtime: EngineRuntime | None = None
        self._running = False
        self._shutdown_event = asyncio.Event()
        self._heartbeat_task: asyncio.Task | None = None
        self._engine_reload_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    async def run(self) -> None:
        LOG.info("启动人格工作进程: %s", self.persona_dir.name)

        # 1. 加载配置
        adapters_cfg = PersonaAdaptersConfig.load(self.paths.adapters)
        experience = PersonaExperienceConfig.load(self.paths.experience)
        LOG.info("加载 %d 个 adapter", len(adapters_cfg.adapters))

        # 2. Create EngineRuntime with experience parameters in plugin_config
        plugin_config = self._build_plugin_config(experience)
        self._runtime = EngineRuntime(
            self.persona_dir,
            plugin_config=plugin_config,
        )

        # 3-4. 启动引擎和 Adapter。多个 Worker 共用外部 MCP 服务时，
        # 初始化阶段串行，进入 ready 后仍然各自独立并行运行。
        await self._start_with_lock(adapters_cfg, plugin_config)

        # 5. 启动心跳
        self._running = True
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        self._write_status({"status": "running", "pid": os.getpid(), "started_at": _now_iso()})

        LOG.info("人格「%s」已就绪，等待消息...", self.persona_dir.name)

        # 6. 阻塞等待关闭信号
        await self._shutdown_event.wait()

        # 7. 清理
        await self._cleanup()

    async def _start_runtime_and_adapters(
        self,
        adapters_cfg: PersonaAdaptersConfig,
        plugin_config: dict[str, Any],
    ) -> None:
        runtime = self._runtime
        if runtime is None:
            raise RuntimeError("EngineRuntime 未初始化")
        await runtime.start()
        if runtime.engine is None:
            # A setup-only runtime may intentionally stay lazy until provider
            # and persona configuration exists.  Never bind a platform adapter
            # to ``None``: it would appear connected yet reject every event.
            LOG.warning("引擎尚未就绪，跳过本次 adapter 启动")
            return
        for adapter_cfg in adapters_cfg.adapters:
            if not adapter_cfg.enabled:
                LOG.info("跳过 disabled adapter: %s", getattr(adapter_cfg, "type", "?"))
                continue
            await self._start_adapter(adapter_cfg, plugin_config)

    async def _start_with_lock(
        self,
        adapters_cfg: PersonaAdaptersConfig,
        plugin_config: dict[str, Any],
    ) -> None:
        if self._startup_lock is None:
            await self._start_runtime_and_adapters(adapters_cfg, plugin_config)
            return
        async with self._startup_lock:
            await self._start_runtime_and_adapters(adapters_cfg, plugin_config)

    # ------------------------------------------------------------------
    # Adapter 启动
    # ------------------------------------------------------------------

    async def _start_adapter(
        self,
        adapter_cfg: Any,
        plugin_config: dict[str, Any],
    ) -> None:
        if isinstance(adapter_cfg, NapCatAdapterConfig):
            runtime_engine = getattr(self._runtime, "engine", None)
            persona_name = getattr(getattr(runtime_engine, "persona", None), "name", "")
            adapter = NapCatAdapter(
                ws_url=adapter_cfg.ws_url,
                token=adapter_cfg.token or None,
                work_path=self.persona_dir,
                config={
                    "root": adapter_cfg.root,
                    "allowed_group_ids": adapter_cfg.allowed_group_ids,
                    "allowed_private_user_ids": adapter_cfg.allowed_private_user_ids,
                    "enable_group_chat": adapter_cfg.enable_group_chat,
                    "enable_private_chat": adapter_cfg.enable_private_chat,
                    "auto_install_tool_deps": plugin_config.get("auto_install_tool_deps", True),
                    "peer_ai_ids": adapter_cfg.peer_ai_ids,
                    "qq_number": adapter_cfg.qq_number,
                    "persona_name": persona_name,
                    "group_dispatch_enabled": adapter_cfg.group_dispatch_enabled,
                    "dispatch_db_path": adapter_cfg.dispatch_db_path
                    or str(self.persona_dir.parent.parent / "dispatcher" / "dispatcher.db"),
                    "dispatch_priority": adapter_cfg.dispatch_priority,
                    "dispatch_min_reply_interval_seconds": (
                        adapter_cfg.dispatch_min_reply_interval_seconds
                    ),
                    "dispatch_lease_seconds": adapter_cfg.dispatch_lease_seconds,
                    "dispatch_peer_cooldown_seconds": adapter_cfg.dispatch_peer_cooldown_seconds,
                    "dispatch_max_peer_turns": adapter_cfg.dispatch_max_peer_turns,
                    "dispatch_score_collection_seconds": adapter_cfg.dispatch_score_collection_seconds,
                    "dispatch_activity_window_seconds": adapter_cfg.dispatch_activity_window_seconds,
                    "dispatch_activity_penalty_per_reply": adapter_cfg.dispatch_activity_penalty_per_reply,
                    "dispatch_max_activity_penalty": adapter_cfg.dispatch_max_activity_penalty,
                },
            )
            if self._runtime is not None and self._runtime.engine is not None:
                persona = getattr(self._runtime.engine, "persona", None)
                if persona:
                    adapter.set_persona_name(getattr(persona, "name", "") or "")
            runtime = self._runtime
            engine = runtime.engine if runtime is not None else None
            if engine is None:
                LOG.warning("引擎尚未就绪，拒绝启动 NapCat adapter")
                return
            await adapter.connect()
            await adapter.start_handling(engine)
            self._adapters.append(adapter)
            runtime.add_tool_bridge("napcat", adapter)
            LOG.info("NapCat adapter 已启动: %s", adapter_cfg.ws_url)
        else:
            LOG.warning("未知 adapter 类型，已跳过: %s", type(adapter_cfg).__name__)

    # ------------------------------------------------------------------
    # 配置转换
    # ------------------------------------------------------------------

    def _build_plugin_config(self, experience: PersonaExperienceConfig) -> dict[str, Any]:
        """将体验参数转换为 EngineRuntime 的 plugin_config。"""
        config: dict[str, Any] = {
            # 参与决策
            "sensitivity": experience.engagement_sensitivity,
            "group_reply_strategies": dict(experience.group_reply_strategies),
            "reply_cooldown_seconds": int(experience.min_reply_interval_seconds),
            "main_model_reply_cooldown_seconds": experience.main_model_reply_cooldown_seconds,
            "memory_unit_top_k": experience.memory_unit_top_k,
            "memory_unit_token_budget": experience.memory_unit_token_budget,
            # 工具
            "max_tool_rounds": experience.max_tool_rounds,
            "auto_install_tool_deps": experience.auto_install_tool_deps,
            "max_sentence_chars": experience.max_sentence_chars,
            "enable_tools": experience.enable_tools,
            "message_prefixes": experience.message_prefixes,
        }

        # 其他 AI 的名字/别名，用于抑制"人类叫别的 AI 时当前 AI 抢话"
        other_ai_names: list[str] = list(experience.other_ai_names or [])
        if other_ai_names:
            config["other_ai_names"] = list(dict.fromkeys(other_ai_names))
        return config

    # ------------------------------------------------------------------
    # 心跳与状态
    # ------------------------------------------------------------------

    async def _heartbeat_loop(self) -> None:
        while self._running:
            for adapter in self._adapters:
                dispatcher = getattr(adapter, "_dispatcher", None)
                if dispatcher is not None:
                    dispatcher.register()
            self._write_status(
                {
                    "status": "running",
                    "pid": os.getpid(),
                    "heartbeat_at": _now_iso(),
                }
            )
            self._check_config_reload()
            await asyncio.sleep(10)

    def _check_config_reload(self) -> None:
        """检查配置文件变更，热重载到引擎。

        通过读取 engine_state/reload_requested 标志文件触发重载。
        标志文件内容为重载类型：persona / orchestration / experience / provider / global / mcp / memory / all
        """
        reload_flag = self.paths.engine_state / "reload_requested"
        if not reload_flag.exists():
            return

        try:
            # 自动保存会在短时间内连续写配置；等待 2 秒静默期后再热重载，避免重复初始化。
            if time.time() - reload_flag.stat().st_mtime < 2.0:
                return
            raw = reload_flag.read_text(encoding="utf-8").strip()
            try:
                payload = json.loads(raw)
                if isinstance(payload, dict):
                    reload_types = {str(item) for item in payload.get("types", []) if str(item)}
                elif isinstance(payload, list):
                    reload_types = {str(item) for item in payload if str(item)}
                else:
                    reload_types = {raw} if raw else set()
            except Exception:
                reload_types = {raw} if raw else set()
            if "all" in reload_types:
                reload_types = {"all"}
            # 原子删除标志文件（消费请求）
            reload_flag.unlink(missing_ok=True)
        except Exception:
            return

        if not reload_types:
            return

        if not self._runtime or not self._runtime.engine:
            LOG.debug("引擎未就绪，跳过配置重载")
            return

        engine = self._runtime.engine

        try:
            if reload_types & {"persona", "all"}:
                self._reload_persona(engine)

            if reload_types & {"orchestration", "all"}:
                self._reload_orchestration(engine)

            if reload_types & {"experience", "all"}:
                self._reload_experience(engine)

            if reload_types & {"provider", "all"}:
                self._reload_provider(engine)

            if reload_types & {"global", "all"}:
                self._reload_global_config(engine)

            if reload_types & {"mcp", "all"}:
                self._schedule_engine_rebuild()

            if reload_types & {"memory", "all"}:
                self._reload_memory_index(engine)

            LOG.info("配置热重载完成: types=%s", sorted(reload_types))
        except Exception as exc:
            LOG.warning("配置热重载失败: %s", exc)

    def _schedule_engine_rebuild(self) -> None:
        if self._engine_reload_task is not None and not self._engine_reload_task.done():
            return
        self._engine_reload_task = asyncio.create_task(self._rebuild_engine_and_rebind())

    async def _rebuild_engine_and_rebind(self) -> None:
        try:
            if self._runtime is None:
                return
            engine = await self._runtime.rebuild_engine()
            for adapter in self._adapters:
                await adapter.rebind_engine(engine)
                self._runtime.add_tool_bridge("napcat", adapter)
            LOG.info("引擎重建完成，已重新绑定 %d 个 adapter", len(self._adapters))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            LOG.warning("引擎重建或 adapter 重绑定失败: %s", exc)
        finally:
            self._engine_reload_task = None

    def _reload_persona(self, engine: Any) -> None:
        """热重载 Persona 配置（persona.json）。"""
        from sirius_pulse.core.persona_store import PersonaStore

        persona = PersonaStore.load(self.persona_dir)
        if not persona:
            LOG.warning("Persona 配置加载失败，跳过重载")
            return

        # 更新 engine 和 brain 的 persona 引用
        engine.persona = persona
        if hasattr(engine, "brain") and engine.brain:
            engine.brain.persona = persona

        # 更新依赖 persona 的组件
        if hasattr(engine, "biography_manager"):
            engine.biography_manager._persona_name = persona.name
            engine.biography_manager._persona_aliases = persona.aliases
        if hasattr(engine, "cognition_analyzer"):
            engine.cognition_analyzer.ai_name = persona.name
            engine.cognition_analyzer.ai_aliases = persona.aliases
            engine.cognition_analyzer.persona = persona

        LOG.info("Persona 配置已热重载: %s", persona.name)

    def _reload_orchestration(self, engine: Any) -> None:
        """热重载编排配置（orchestration.json 中的 task_timeout / task_retries）。"""
        from sirius_pulse.core.orchestration_store import OrchestrationStore

        orch = OrchestrationStore.load(self.persona_dir)
        if not orch:
            LOG.warning("Orchestration 配置加载失败，跳过重载")
            return

        # 重新读取本地任务参数并重建模型路由器（任务名 → AMKR 任务定义）。
        engine._init_orchestration_and_task_models()
        engine._init_model_router()

        # 同步更新 brain 的 model_router
        if hasattr(engine, "brain") and engine.brain:
            engine.brain.router = engine.model_router

        # 一并刷新 AMKR 连接，确保地址 / Key / 工作空间变更立即生效。
        self._reload_provider(engine)

        LOG.info("Orchestration 配置已热重载")

    def _reload_experience(self, engine: Any) -> None:
        """热重载 Experience 配置（experience.json）。"""
        exp = PersonaExperienceConfig.load(self.paths.experience)

        # 更新 engine.config 中的 experience 相关字段
        runtime_config = self._build_plugin_config(exp)
        engine.config.update(runtime_config)

        # 同步更新 brain 的 config
        if hasattr(engine, "brain") and engine.brain:
            engine.brain.config.update(runtime_config)

        LOG.info("Experience 配置已热重载")

    def _reload_global_config(self, engine: Any) -> None:
        """Reload global config; runtime output limits live in experience now."""
        LOG.info("Global config reloaded")

    def _reload_memory_index(self, engine: Any) -> None:
        """丢弃记忆单元的内存缓存，下次检索时从磁盘重新加载。

        WebUI 的「重建索引」在另一个进程里重写了 ``memory_units/*.json`` 与其中的
        向量；本进程缓存的是重建前的旧向量，不丢弃就会继续按旧维度检索。
        """
        reloaded = []
        units = getattr(engine, "memory_unit_manager", None)
        if units is not None:
            units.reload_from_disk()
            reloaded.append("memory_units")
        if reloaded:
            LOG.info("已清空 %s 的内存索引缓存，将按新向量重新加载", "、".join(reloaded))
        else:
            LOG.debug("引擎无记忆单元管理器，跳过记忆索引重载")

    def _reload_provider(self, engine: Any) -> None:
        """热重载 AMKR 连接配置。

        重新读取连接配置与**该空间的推理 key**并同步到 engine、brain、
        cognition_analyzer，使 WebUI 上的地址 / Key / 工作空间变更，以及运维页的
        推理 key 轮换，都无需重启引擎即可生效。凭据每次都从磁盘重读，因此轮换后
        旧 provider 不会继续拿着已作废的 key。
        """
        if not self._runtime:
            LOG.debug("Runtime 未就绪，跳过 provider 重载")
            return

        new_provider = self._runtime._build_provider()
        if new_provider is None:
            LOG.warning("AMKR provider 重建失败（缺少地址或该空间的推理 key），保留旧 provider")
            return

        # 同步到 engine 及其子系统
        engine.provider_async = new_provider
        if hasattr(engine, "brain") and engine.brain:
            engine.brain.provider_async = new_provider
        if hasattr(engine, "cognition_analyzer") and engine.cognition_analyzer:
            engine.cognition_analyzer.provider_async = new_provider

        LOG.info("AMKR 连接配置已热重载")

    def _write_status(self, status: dict[str, Any]) -> None:
        try:
            path = self.paths.engine_state / "worker_status.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
            replace_with_retry(tmp, path)
        except Exception as exc:
            LOG.debug("状态写入失败: %s", exc)

    # ------------------------------------------------------------------
    # 关闭与清理
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        """触发优雅关闭（可在信号处理器中调用）。"""
        LOG.info("收到关闭信号，正在停止人格工作进程...")
        self._running = False
        self._shutdown_event.set()

    async def _cleanup(self) -> None:
        LOG.info("开始清理资源...")

        if self._engine_reload_task and not self._engine_reload_task.done():
            self._engine_reload_task.cancel()
            try:
                await self._engine_reload_task
            except asyncio.CancelledError:
                pass

        if self._heartbeat_task and not self._heartbeat_task.done():
            self._heartbeat_task.cancel()
            try:
                await self._heartbeat_task
            except asyncio.CancelledError:
                pass

        for adapter in self._adapters:
            try:
                await adapter.close()
            except Exception as exc:
                LOG.warning("Adapter 关闭失败: %s", exc)

        if self._runtime is not None:
            try:
                await self._runtime.stop()
            except Exception as exc:
                LOG.warning("EngineRuntime 停止失败: %s", exc)

        self._write_status(
            {
                "status": "stopped",
                "pid": os.getpid(),
                "stopped_at": _now_iso(),
            }
        )
        LOG.info("人格工作进程已停止")


# ---------------------------------------------------------------------------
# 入口
# ---------------------------------------------------------------------------


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


async def _main() -> None:
    parser = argparse.ArgumentParser(description="SiriusChat 人格工作进程")
    parser.add_argument("--config", required=True, help="人格配置目录路径")
    parser.add_argument("--log-level", default="INFO", help="日志级别")
    args = parser.parse_args()

    pdir = Path(args.config).resolve()
    log_file = pdir / "logs" / "persona.log"
    setup_log_archival(log_file)
    configure_logging(
        level=args.log_level.upper(),
        format_type="console",
        log_file=str(log_file),
    )

    worker = PersonaWorker(args.config)

    # 信号处理（Windows 不支持 loop.add_signal_handler）
    if sys.platform == "win32":
        import signal as _signal

        def _sig_handler(_signum, _frame):
            worker.shutdown()

        _signal.signal(_signal.SIGINT, _sig_handler)
        _signal.signal(_signal.SIGTERM, _sig_handler)
    else:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, worker.shutdown)

    try:
        await worker.run()
    except Exception:
        LOG.exception("人格工作进程异常退出")
        raise


if __name__ == "__main__":
    asyncio.run(_main())
