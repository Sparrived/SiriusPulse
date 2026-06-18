"""人格工作进程 — 单个人格的独立运行入口。

职责：
- 加载人格级配置（persona.json / orchestration.json / adapters.json / experience.json）
- 创建 EngineRuntime + NapCatAdapter
- 运行事件循环，定期写入心跳
- 响应 SIGTERM 优雅退出

启动方式（由 PersonaManager 调用）::

    python -m sirius_pulse.persona_worker --config data/personas/akane
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

    def __init__(self, persona_dir: Path | str) -> None:
        self.persona_dir = Path(persona_dir).resolve()
        self.paths = PersonaConfigPaths(self.persona_dir)
        self._adapters: list[NapCatAdapter] = []
        self._napcat_managers: list[Any] = []
        self._runtime: EngineRuntime | None = None
        self._running = False
        self._shutdown_event = asyncio.Event()
        self._heartbeat_task: asyncio.Task | None = None

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------

    async def run(self) -> None:
        LOG.info("启动人格工作进程: %s", self.persona_dir.name)

        # 1. 加载配置
        adapters_cfg = PersonaAdaptersConfig.load(self.paths.adapters)
        experience = PersonaExperienceConfig.load(self.paths.experience)
        LOG.info("加载 %d 个 adapter，体验模式: %s", len(adapters_cfg.adapters), experience.memory_depth)

        # 1.5 自动发现同项目其他 AI 的 QQ 号
        self._auto_populate_peer_ai_ids(adapters_cfg)

        # 2. 创建 EngineRuntime（experience 参数注入 plugin_config）
        plugin_config = self._build_plugin_config(experience)
        global_data_path = self.persona_dir.parent.parent
        self._runtime = EngineRuntime(
            self.persona_dir,
            plugin_config=plugin_config,
            global_data_path=global_data_path,
        )

        # 3. 启动引擎
        await self._runtime.start()

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

    def _auto_populate_peer_ai_ids(self, adapters_cfg: PersonaAdaptersConfig) -> None:
        """自动扫描同项目其他人格的 QQ 号，填充到 peer_ai_ids 中。"""
        personas_dir = self.persona_dir.parent
        if not personas_dir.exists():
            return
        other_qqs: list[str] = []
        for subdir in personas_dir.iterdir():
            if not subdir.is_dir() or subdir.name == self.persona_dir.name:
                continue
            other_paths = PersonaConfigPaths(subdir)
            if not other_paths.adapters.exists():
                continue
            try:
                other_adapters = PersonaAdaptersConfig.load(other_paths.adapters)
                for a in other_adapters.adapters:
                    qq = getattr(a, "qq_number", "")
                    if qq:
                        other_qqs.append(str(qq))
            except Exception:
                continue
        if not other_qqs:
            return
        for cfg in adapters_cfg.adapters:
            if isinstance(cfg, NapCatAdapterConfig):
                existing = set(str(x) for x in cfg.peer_ai_ids)
                added = [qq for qq in other_qqs if qq not in existing]
                if added:
                    cfg.peer_ai_ids.extend(added)
                    LOG.info("自动填充 peer_ai_ids: %s", cfg.peer_ai_ids)

    def _auto_populate_host_qq_ids(self, sidekick: Any) -> None:
        """当 sidekick.host_persona_names 非空时，扫描同项目其他人格的
        persona.json 获取人格名，再扫描 adapters.json 获取 qq_number，
        将匹配人格的 qq_number 自动补入 host_qq_ids。"""
        host_names = {n.lower() for n in sidekick.host_persona_names if n}
        if not host_names:
            return
        personas_dir = self.persona_dir.parent
        if not personas_dir.exists():
            return
        for subdir in personas_dir.iterdir():
            if not subdir.is_dir() or subdir.name == self.persona_dir.name:
                continue
            from sirius_pulse.core.persona_store import PersonaStore

            other_persona = PersonaStore.load(subdir)
            if not other_persona:
                continue
            if other_persona.name.lower() not in host_names:
                continue
            # 找到匹配人格，读取 adapters.json 获取 qq_number
            other_paths = PersonaConfigPaths(subdir)
            if not other_paths.adapters.exists():
                continue
            try:
                other_adapters = PersonaAdaptersConfig.load(other_paths.adapters)
                for a in other_adapters.adapters:
                    qq = getattr(a, "qq_number", "")
                    if qq and str(qq) not in [str(v) for v in sidekick.host_qq_ids]:
                        sidekick.host_qq_ids.append(str(qq))
                        LOG.info("小跟班: 自动补入宿主 QQ %s (人格: %s)", qq, other_persona.name)
            except Exception:
                continue

    @staticmethod
    async def _probe_ws_port(host: str, port: int, timeout: float = 3.0) -> bool:
        """快速探测 TCP 端口是否可连接。"""
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, port), timeout=timeout
            )
            writer.close()
            await writer.wait_closed()
            return True
        except Exception:
            return False

    async def _ensure_napcat_running(self, adapter_cfg: "NapCatAdapterConfig") -> bool:
        """检查 NapCat WS 是否可达，不可达则自动启动实例并等待就绪。

        Returns True if WS is (now) reachable, False on failure.
        """
        ws_url = adapter_cfg.ws_url
        try:
            host_port = ws_url.replace("ws://", "").replace("wss://", "").split("/")[0]
            host, port_str = host_port.rsplit(":", 1)
            port = int(port_str)
        except (ValueError, IndexError):
            LOG.warning("无法解析 ws_url: %s，跳过自动管理", ws_url)
            return True

        if await self._probe_ws_port(host, port):
            return True

        LOG.info("NapCat WS %s:%s 不可达，尝试自动启动实例...", host, port)

        qq_number = getattr(adapter_cfg, "qq_number", "")
        if not qq_number:
            LOG.warning("人格 %s 未配置 qq_number，无法自动启动 NapCat 实例", self.persona_dir.name)
            return False

        # 查找全局 NapCat 安装目录
        global_data_path = self.persona_dir.parent.parent
        config_path = global_data_path / "global_config.json"
        napcat_install_dir = None
        if config_path.exists():
            try:
                import json as _json

                gcfg = _json.loads(config_path.read_text(encoding="utf-8"))
                napcat_install_dir = gcfg.get("napcat_install_dir")
            except Exception:
                pass
        if not napcat_install_dir:
            napcat_install_dir = str(self.persona_dir.parent.parent.parent / "napcat")

        from sirius_pulse.platforms.onebot_v11.napcat.manager import NapCatManager

        mgr = NapCatManager.for_persona(
            global_install_dir=napcat_install_dir,
            persona_name=self.persona_dir.name,
        )

        if not mgr.is_installed:
            LOG.info("NapCat 未安装，尝试自动下载安装...")
            result = await mgr.install()
            if not result["success"]:
                LOG.error("NapCat 自动安装失败: %s", result["message"])
                return False
            LOG.info("NapCat 安装完成")

        LOG.info("配置 NapCat 实例 (QQ: %s, 端口: %s)...", qq_number, port)
        mgr.configure(qq_number=qq_number, ws_port=port)

        result = await mgr.start(qq_number=qq_number)
        if not result["success"]:
            LOG.error("NapCat 实例启动失败: %s", result["message"])
            return False

        LOG.info("NapCat 实例已启动，等待 WS 就绪...")
        ready_result = await mgr.wait_for_ws(port=port, timeout=180.0)
        if ready_result.get("ready"):
            self._napcat_managers.append(mgr)
            LOG.info("NapCat WS 已就绪 (QQ=%s)", ready_result.get("self_id", "unknown"))
            return True
        else:
            LOG.error("NapCat WS 等待超时: %s", ready_result.get("error", "unknown"))
            return False

    async def _start_adapter(
        self,
        adapter_cfg: Any,
        plugin_config: dict[str, Any],
    ) -> None:
        if isinstance(adapter_cfg, NapCatAdapterConfig):
            ok = await self._ensure_napcat_running(adapter_cfg)
            if not ok:
                LOG.error(
                    "NapCat 不可用 (%s)，跳过该 adapter",
                    adapter_cfg.ws_url,
                )
                return

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
                    "sidekick": plugin_config.get("sidekick", {}),
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
            "proactive_check_interval_seconds": int(experience.proactive_interval_seconds),
            "proactive_silence_minutes": max(1, int(experience.proactive_interval_seconds / 60)),
            # 其他体验参数直接透传（Bridge 可能用到）
            "reply_mode": experience.reply_mode,
            "proactive_enabled": experience.proactive_enabled,
            "delay_reply_enabled": experience.delay_reply_enabled,
            "pending_message_threshold": experience.pending_message_threshold,
            "reply_frequency_max_replies": experience.reply_frequency_max_replies,
            "reply_frequency_exempt_on_mention": experience.reply_frequency_exempt_on_mention,
            "max_concurrent_llm_calls": experience.max_concurrent_llm_calls,
            "enable_skills": experience.enable_skills,
            "skill_execution_timeout": experience.skill_execution_timeout,
            "memory_depth": experience.memory_depth,
            "message_prefixes": experience.message_prefixes,
            "sidekick": experience.sidekick.to_dict(),
        }
        # 小跟班宿主 QQ 自动解析：当 host_persona_names 非空时，
        # 扫描同项目其他人格的 adapters.json，将匹配人格的 qq_number 自动补入 host_qq_ids。
        if experience.sidekick.host_persona_names:
            self._auto_populate_host_qq_ids(experience.sidekick)

        # 同项目其他 AI 的名字/别名，用于抑制"人类叫别的 AI 时当前 AI 抢话"
        other_ai_names: list[str] = []
        personas_dir = self.persona_dir.parent
        if personas_dir.exists():
            for subdir in personas_dir.iterdir():
                if not subdir.is_dir() or subdir.name == self.persona_dir.name:
                    continue
                from sirius_pulse.core.persona_store import PersonaStore

                other_persona = PersonaStore.load(subdir)
                if other_persona:
                    other_ai_names.append(other_persona.name)
                    other_ai_names.extend(other_persona.aliases)
        # 合并手动配置的其他 AI 名字
        manual_names = experience.other_ai_names
        if manual_names:
            other_ai_names.extend(manual_names)
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

        for mgr in self._napcat_managers:
            try:
                if mgr.is_running:
                    # Windows 下默认保留 NapCat/QQ 进程，避免杀死已登录会话导致下次需重新扫码。
                    # 仅断开管理器引用，让 QQ 进程继续运行以实现快速登录复用。
                    # 如需强制终止，可通过配置或手动调用 mgr.stop(force=True)。
                    if sys.platform == "win32":
                        LOG.info("Windows 下保留 NapCat/QQ 进程运行，以维持登录状态供下次复用")
                        await mgr.stop(force=False, preserve_session=True)
                    else:
                        await mgr.stop()
            except Exception as exc:
                LOG.warning("NapCat 实例停止失败: %s", exc)

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
    log_file = pdir / "logs" / "worker.log"
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
