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

LOG = logging.getLogger("sirius.persona_worker")


class PersonaWorker:
    """单个人格的运行时封装。"""

    def __init__(
        self,
        persona_dir: Path | str,
        butler_url: str = "",
        butler_token: str | None = None,
    ) -> None:
        self.persona_dir = Path(persona_dir).resolve()
        self.paths = PersonaConfigPaths(self.persona_dir)
        self._adapters: list[NapCatAdapter] = []
        self._runtime: EngineRuntime | None = None
        self._running = False
        self._shutdown_event = asyncio.Event()
        self._heartbeat_task: asyncio.Task | None = None
        self._butler_url = butler_url
        self._butler_token = butler_token
        self._remote_bridge: Any | None = None

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    async def run(self) -> None:
        LOG.info("启动人格工作进程: %s", self.persona_dir.name)

        # 1. 加载配置
        adapters_cfg = PersonaAdaptersConfig.load(self.paths.adapters)
        experience = PersonaExperienceConfig.load(self.paths.experience)
        LOG.info(
            "加载 %d 个 adapter，体验模式: %s", len(adapters_cfg.adapters), experience.memory_depth
        )

        # 2. 创建 EngineRuntime（experience 参数注入 plugin_config）
        plugin_config = self._build_plugin_config(experience)
        self._runtime = EngineRuntime(
            self.persona_dir,
            plugin_config=plugin_config,
        )

        # 2.5 助手模式：创建远程存储桥接
        if self._butler_url:
            from sirius_pulse.network.remote_bridge import RemoteStorageBridge

            self._remote_bridge = RemoteStorageBridge(
                self._butler_url,
                token=self._butler_token,
            )
            LOG.info("助手模式：从管家端加载运行时快照...")
            snapshot = await self._remote_bridge.load_snapshot()
            if snapshot:
                LOG.info("快照加载成功，数据项: %d", len(snapshot))
            else:
                LOG.warning("快照为空或加载失败，将以空状态启动")
            self._runtime.set_remote_bridge(self._remote_bridge)

        # 3. 启动引擎
        await self._runtime.start()

        # 3.5 助手模式：启动写缓冲
        if self._remote_bridge is not None:
            await self._remote_bridge.start()
            LOG.info("远程写缓冲已启动")

        # 4. 创建并启动各平台 Adapter
        for adapter_cfg in adapters_cfg.adapters:
            if not adapter_cfg.enabled:
                LOG.info("跳过 disabled adapter: %s", getattr(adapter_cfg, "type", "?"))
                continue
            await self._start_adapter(adapter_cfg, plugin_config)

        # 5. 启动心跳
        self._running = True
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())
        self._write_status({"status": "running", "pid": os.getpid(), "started_at": _now_iso()})

        LOG.info("人格「%s」已就绪，等待消息...", self.persona_dir.name)

        # 6. 阻塞等待关闭信号
        await self._shutdown_event.wait()

        # 7. 清理
        await self._cleanup()

    # ------------------------------------------------------------------
    # Adapter 启动
    # ------------------------------------------------------------------

    async def _start_adapter(
        self,
        adapter_cfg: Any,
        plugin_config: dict[str, Any],
    ) -> None:
        if isinstance(adapter_cfg, NapCatAdapterConfig):
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
                    "auto_install_skill_deps": plugin_config.get("auto_install_skill_deps", True),
                    "peer_ai_ids": adapter_cfg.peer_ai_ids,
                    "qq_number": adapter_cfg.qq_number,
                },
            )
            if self._runtime is not None and self._runtime.engine is not None:
                persona = getattr(self._runtime.engine, "persona", None)
                if persona:
                    adapter.set_persona_name(getattr(persona, "name", "") or "")
            await adapter.connect()
            await adapter.start_handling(self._runtime.engine)  # type: ignore[union-attr]
            self._adapters.append(adapter)
            self._runtime.add_skill_bridge("napcat", adapter)  # type: ignore[union-attr]
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
            "reply_cooldown_seconds": int(experience.min_reply_interval_seconds),
            # 技能
            "max_skill_rounds": experience.max_skill_rounds,
            "auto_install_skill_deps": experience.auto_install_skill_deps,
            # 后台任务
            "delayed_queue_tick_interval_seconds": 3,
            # 其他体验参数直接透传（Bridge 可能用到）
            "reply_mode": experience.reply_mode,
            "delay_reply_enabled": experience.delay_reply_enabled,
            "pending_message_threshold": experience.pending_message_threshold,
            "reply_frequency_max_replies": experience.reply_frequency_max_replies,
            "reply_frequency_exempt_on_mention": experience.reply_frequency_exempt_on_mention,
            "max_concurrent_llm_calls": experience.max_concurrent_llm_calls,
            "enable_skills": experience.enable_skills,
            "skill_execution_timeout": experience.skill_execution_timeout,
            "plan_mode_enabled": experience.plan_mode_enabled,
            "plan_mode_limit_normal_tools": experience.plan_mode_limit_normal_tools,
            "plan_mode_allow_light_chat": experience.plan_mode_allow_light_chat,
            "plan_mode_chat_awareness_enabled": experience.plan_mode_chat_awareness_enabled,
            "plan_mode_presence_enabled": experience.plan_mode_presence_enabled,
            "plan_mode_presence_min_interval_seconds": (
                experience.plan_mode_presence_min_interval_seconds
            ),
            "plan_mode_presence_enter_message": experience.plan_mode_presence_enter_message,
            "plan_mode_presence_update_message": experience.plan_mode_presence_update_message,
            "memory_depth": experience.memory_depth,
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
            self._write_status(
                {
                    "status": "running",
                    "pid": os.getpid(),
                    "heartbeat_at": _now_iso(),
                }
            )
            self._check_enabled_flag()
            self._check_config_reload()
            await asyncio.sleep(10)

    def set_adapter_enabled(self, enabled: bool) -> None:
        """程序化控制所有 adapter 的消息处理开关。

        供 ButlerServer 在助手端接管/释放时调用。
        """
        for adapter in self._adapters:
            if hasattr(adapter, "_enabled"):
                adapter._enabled = enabled
        LOG.info("所有 adapter 已%s", "启用" if enabled else "禁用")

    def _check_enabled_flag(self) -> None:
        """读取 engine_state/enabled 标志，同步到各 Bridge。"""
        flag = self.paths.engine_state / "enabled"
        if not flag.exists():
            return
        try:
            text = flag.read_text(encoding="utf-8").strip()
            enabled = text == "1"
            for adapter in self._adapters:
                if hasattr(adapter, "_enabled") and adapter._enabled != enabled:
                    adapter._enabled = enabled
                    LOG.info("Adapter %s 已%s", adapter, "启用" if enabled else "禁用")
        except Exception:
            pass

    def _check_config_reload(self) -> None:
        """检查配置文件变更，热重载到引擎。

        通过读取 engine_state/reload_requested 标志文件触发重载。
        标志文件内容为重载类型：persona / orchestration / experience / provider / all
        """
        reload_flag = self.paths.engine_state / "reload_requested"
        if not reload_flag.exists():
            return

        try:
            reload_type = reload_flag.read_text(encoding="utf-8").strip()
            # 原子删除标志文件（消费请求）
            reload_flag.unlink(missing_ok=True)
        except Exception:
            return

        if not self._runtime or not self._runtime.engine:
            LOG.debug("引擎未就绪，跳过配置重载")
            return

        engine = self._runtime.engine

        try:
            if reload_type in ("persona", "all"):
                self._reload_persona(engine)

            if reload_type in ("orchestration", "all"):
                self._reload_orchestration(engine)

            if reload_type in ("experience", "all"):
                self._reload_experience(engine)

            if reload_type in ("provider", "all"):
                self._reload_provider(engine)

            LOG.info("配置热重载完成: type=%s", reload_type)
        except Exception as exc:
            LOG.warning("配置热重载失败: %s", exc)

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
        if hasattr(engine, "glossary_manager"):
            engine.glossary_manager._persona_name = persona.name
        if hasattr(engine, "cognition_analyzer"):
            engine.cognition_analyzer.ai_name = persona.name
            engine.cognition_analyzer.ai_aliases = persona.aliases
            engine.cognition_analyzer.persona = persona

        LOG.info("Persona 配置已热重载: %s", persona.name)

    def _reload_orchestration(self, engine: Any) -> None:
        """热重载 Orchestration 配置（orchestration.json）。"""
        from sirius_pulse.core.orchestration_store import OrchestrationStore

        orch = OrchestrationStore.load(self.persona_dir)
        if not orch:
            LOG.warning("Orchestration 配置加载失败，跳过重载")
            return

        # 重新初始化任务模型映射和模型路由器
        engine._init_orchestration_and_task_models()
        engine._init_model_router()

        # 同步更新 brain 的 model_router
        if hasattr(engine, "brain") and engine.brain:
            engine.brain.router = engine.model_router

        # 编排配置变更时同步刷新 provider，确保新模型名能被路由到正确的提供商
        self._reload_provider(engine)

        LOG.info("Orchestration 配置已热重载")

    def _reload_experience(self, engine: Any) -> None:
        """热重载 Experience 配置（experience.json）。"""
        exp = PersonaExperienceConfig.load(self.paths.experience)

        # 更新 engine.config 中的 experience 相关字段
        exp_dict = exp.to_dict()
        engine.config.update(exp_dict)

        # 同步更新 brain 的 config
        if hasattr(engine, "brain") and engine.brain:
            engine.brain.config.update(exp_dict)

        LOG.info("Experience 配置已热重载")

    def _reload_provider(self, engine: Any) -> None:
        """热重载 Provider 配置（provider_keys.json）。

        重新从磁盘加载 provider 配置，构建新的 AutoRoutingProvider 并同步到
        engine、brain、cognition_analyzer 中，使 provider 变更（新增/删除提供商、
        模型列表更新等）无需重启引擎即可生效。
        """
        if not self._runtime:
            LOG.debug("Runtime 未就绪，跳过 provider 重载")
            return

        new_provider = self._runtime._build_provider()
        if new_provider is None:
            LOG.warning("Provider 重建失败（无可用配置），保留旧 provider")
            return

        # 同步到 engine 及其子系统
        engine.provider_async = new_provider
        if hasattr(engine, "brain") and engine.brain:
            engine.brain.provider_async = new_provider
        if hasattr(engine, "cognition_analyzer") and engine.cognition_analyzer:
            engine.cognition_analyzer.provider_async = new_provider

        LOG.info("Provider 配置已热重载")

    def _write_status(self, status: dict[str, Any]) -> None:
        try:
            path = self.paths.engine_state / "worker_status.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
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

        # 停止远程写缓冲（在 runtime.stop 之前，确保最后一次 flush 完成）
        if self._remote_bridge is not None:
            try:
                await self._remote_bridge.stop()
            except Exception as exc:
                LOG.warning("远程写缓冲停止失败: %s", exc)

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
    parser.add_argument("--butler-url", default="", help="管家端数据 API 地址（助手模式）")
    parser.add_argument("--butler-token", default=None, help="管家端认证 token")
    args = parser.parse_args()

    pdir = Path(args.config).resolve()
    log_file = pdir / "logs" / "worker.log"
    setup_log_archival(log_file)
    configure_logging(
        level=args.log_level.upper(),
        format_type="console",
        log_file=str(log_file),
    )

    worker = PersonaWorker(
        args.config,
        butler_url=args.butler_url,
        butler_token=args.butler_token,
    )

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
