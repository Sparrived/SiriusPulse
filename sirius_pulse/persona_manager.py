"""人格管理器 — 主进程中的多人格生命周期管理。

职责：
- 扫描和维护 personas/ 目录
- 创建/删除人格（含默认配置生成）
- 启动/停止人格子进程
- 监控子进程健康状态
- 为 WebUI 提供查询接口
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sirius_pulse.core.persona_generator import PersonaGenerator
from sirius_pulse.core.persona_store import PersonaStore
from sirius_pulse.models.persona import PersonaProfile
from sirius_pulse.persona_config import (
    NapCatAdapterConfig,
    PersonaAdaptersConfig,
    PersonaConfigPaths,
    PersonaExperienceConfig,
)

LOG = logging.getLogger("sirius.persona_manager")


class PersonaManager:
    """管理所有人格的生命周期。"""

    def __init__(self, data_path: Path | str, global_config: dict[str, Any] | None = None) -> None:
        self.data_path = Path(data_path).resolve()
        self.data_path.mkdir(parents=True, exist_ok=True)
        self.personas_dir = self.data_path / "personas"
        self.personas_dir.mkdir(parents=True, exist_ok=True)
        self.global_config = dict(global_config or {})
        self._processes: dict[str, subprocess.Popen] = {}
        self._port_registry_path = self.data_path / "adapter_port_registry.json"

        import atexit
        atexit.register(self._cleanup_stale_worker_statuses)

    # ------------------------------------------------------------------
    # 端口分配
    # ------------------------------------------------------------------

    def _load_port_registry(self) -> dict[str, dict[str, Any]]:
        if not self._port_registry_path.exists():
            return {}
        try:
            raw = json.loads(self._port_registry_path.read_text(encoding="utf-8"))
            # Backward-compat: old format was dict[str, int]
            migrated: dict[str, dict[str, Any]] = {}
            for k, v in raw.items():
                if isinstance(v, int):
                    migrated[k] = {"port": v}
                elif isinstance(v, dict):
                    migrated[k] = v
            return migrated
        except Exception:
            return {}

    def _save_port_registry(self, ports: dict[str, dict[str, Any]]) -> None:
        self._port_registry_path.write_text(
            json.dumps(ports, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @staticmethod
    def _is_port_free(port: int) -> bool:
        """检查端口是否在 OS 层面可用。"""
        import socket
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.bind(("localhost", port))
                return True
        except OSError:
            return False

    def _is_port_leased(self, record: dict[str, Any]) -> bool:
        """Return True if the port record has an active lease."""
        leased_until = record.get("leased_until")
        if leased_until is None:
            return False
        return time.time() < leased_until

    def _allocate_port(self, name: str) -> int:
        """为指定人格分配一个未被占用的 WebSocket 端口（从 3001 开始递增，并验证 OS 可用性）。"""
        ports = self._load_port_registry()
        # 如果已有分配，先验证是否仍可用
        if name in ports:
            allocated = ports[name]
            allocated_port = allocated["port"] if isinstance(allocated, dict) else allocated
            if self._is_port_free(allocated_port):
                return allocated_port
            # 端口已被占用，重新分配
            LOG.warning("人格 %s 的端口 %s 已被占用，重新分配", name, allocated_port)
            del ports[name]

        base_port = int(self.global_config.get("napcat_base_port", 3001))
        used = {v["port"] if isinstance(v, dict) else v for v in ports.values()}
        port = base_port
        while port in used or not self._is_port_free(port):
            port += 1
            if port > 65535:
                raise RuntimeError("无可用端口")
        ports[name] = port
        self._save_port_registry(ports)
        LOG.info("为 %s 分配端口: %s", name, port)
        return port

    def _release_port(self, name: str) -> None:
        """释放人格占用的端口记录。"""
        ports = self._load_port_registry()
        if name in ports:
            del ports[name]
            self._save_port_registry(ports)

    def get_port(self, name: str) -> int | None:
        """获取人格当前分配的端口。"""
        return self._load_port_registry().get(name)

    # ------------------------------------------------------------------
    # 扫描与列表
    # ------------------------------------------------------------------

    def list_personas(self) -> list[dict[str, Any]]:
        """扫描目录，返回所有人格的元信息列表。"""
        results: list[dict[str, Any]] = []
        if not self.personas_dir.exists():
            return results
        for subdir in sorted(self.personas_dir.iterdir()):
            if not subdir.is_dir():
                continue
            name = subdir.name
            info = self._inspect_persona(name)
            if info:
                results.append(info)
        return results

    def _inspect_persona(self, name: str) -> dict[str, Any] | None:
        """检查单个人格目录，返回元信息（含进程存活检测）。"""
        pdir = self.personas_dir / name
        if not pdir.exists():
            return None

        paths = PersonaConfigPaths(pdir)
        persona = PersonaStore.load(pdir)
        adapters = PersonaAdaptersConfig.load(paths.adapters)
        experience = PersonaExperienceConfig.load(paths.experience)
        status = self._read_worker_status(name)

        # 进程存活检测
        is_alive = self.is_running(name)
        pid = status.get("pid") if status else None
        if not is_alive and pid:
            pid = None  # PID 已死亡
            # 同步更新状态文件为 stopped，避免下次仍显示 running
            if status:
                status["status"] = "stopped"
                status_path = self.personas_dir / name / "engine_state" / "worker_status.json"
                try:
                    tmp = status_path.with_suffix(status_path.suffix + ".tmp")
                    tmp.write_text(json.dumps(status, ensure_ascii=False), encoding="utf-8")
                    tmp.replace(status_path)
                except Exception:
                    pass

        # 是否已启用（至少有一个 adapter enabled）
        has_enabled_adapter = any(a.enabled for a in adapters.adapters)

        return {
            "name": name,
            "persona_name": persona.name if persona else None,
            "persona_summary": persona.persona_summary if persona else None,
            "adapters_count": len(adapters.adapters),
            "adapters": [
                {
                    "type": a.type,
                    "enabled": a.enabled,
                    "ws_url": a.ws_url,
                    "qq_number": getattr(a, "qq_number", ""),
                }
                for a in adapters.adapters
            ],
            "enabled": has_enabled_adapter,
            "running": is_alive,
            "pid": pid,
            "status": "running" if is_alive else "stopped",
            "heartbeat_at": status.get("heartbeat_at") if status else None,
            "work_path": str(pdir),
        }

    def _read_worker_status(self, name: str) -> dict[str, Any] | None:
        """读取子进程的心跳状态文件。"""
        path = self.personas_dir / name / "engine_state" / "worker_status.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            # 检查心跳是否过期（30 秒）
            heartbeat = data.get("heartbeat_at")
            if heartbeat:
                try:
                    hb = datetime.fromisoformat(heartbeat)
                    if (datetime.now(timezone.utc) - hb).total_seconds() > 30:
                        data["status"] = "stale"
                except Exception:
                    pass
            return data
        except Exception:
            return None

    # ------------------------------------------------------------------
    # 创建与删除
    # ------------------------------------------------------------------

    def create_persona(
        self,
        name: str,
        *,
        persona_name: str | None = None,
        template: str = "default",
    ) -> Path:
        """创建新人格目录及默认配置。"""
        pdir = self.personas_dir / name
        if pdir.exists():
            raise FileExistsError(f"人格已存在: {name}")

        pdir.mkdir(parents=True)
        paths = PersonaConfigPaths(pdir)

        # 1. 生成人格定义
        persona_name = persona_name or name
        if template == "default":
            persona = PersonaProfile(name=persona_name)
        else:
            persona = PersonaProfile(name=persona_name)

        PersonaStore.save(pdir, persona)

        # 2. 生成默认 adapter 配置（自动分配端口）
        port = self._allocate_port(name)
        adapters = PersonaAdaptersConfig(
            adapters=[
                NapCatAdapterConfig(
                    ws_url=f"ws://localhost:{port}",
                    token="napcat_ws",
                )
            ]
        )
        adapters.save(paths.adapters)

        # 3. 生成默认 experience 配置
        experience = PersonaExperienceConfig()
        experience.save(paths.experience)

        # 4. 生成默认 orchestration 配置
        orch = {
            "analysis_model": "gpt-4o-mini",
            "chat_model": "gpt-4o",
            "vision_model": "gpt-4o",
        }
        paths.orchestration.write_text(
            json.dumps(orch, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

        # 5. 创建表情包目录（用户手动放入图片文件）
        sticker_dir = pdir / "stickers"
        sticker_dir.mkdir(parents=True, exist_ok=True)

        LOG.info("人格已创建: %s @ %s", name, pdir)
        return pdir

    def migrate_persona(
        self,
        source_dir: Path | str,
        name: str,
    ) -> Path:
        """从旧版单人格目录迁移到新的多人格结构。

        迁移内容：
        - persona.json / orchestration.json
        - engine_state/（记忆、情绪、状态等）
        - image_cache/
        - qq_bridge_config.json → adapters.json
        - 其他子目录（memory, diary, token, skill_data 等）
        """
        source = Path(source_dir).resolve()
        if not source.exists():
            raise FileNotFoundError(f"源目录不存在: {source}")

        pdir = self.personas_dir / name
        if pdir.exists():
            raise FileExistsError(f"目标人格已存在: {name}")

        pdir.mkdir(parents=True)
        paths = PersonaConfigPaths(pdir)

        # 1. 迁移人格定义
        src_persona = source / "engine_state" / "persona.json"
        if not src_persona.exists():
            src_persona = source / "persona.json"
        if src_persona.exists():
            shutil.copy2(str(src_persona), str(paths.persona))
            LOG.info("迁移 persona.json")
        else:
            #  fallback：生成默认人格
            PersonaStore.save(pdir, PersonaProfile(name=name))

        # 2. 迁移模型编排
        src_orch = source / "engine_state" / "orchestration.json"
        if src_orch.exists():
            shutil.copy2(str(src_orch), str(paths.orchestration))
            LOG.info("迁移 orchestration.json")
        else:
            paths.orchestration.write_text(
                json.dumps(
                    {"analysis_model": "gpt-4o-mini", "chat_model": "gpt-4o", "vision_model": "gpt-4o"},
                    ensure_ascii=False, indent=2,
                ),
                encoding="utf-8",
            )

        # 3. 迁移桥接配置 → adapters.json
        # 尝试从 napcat 配置推断 QQ 号和端口
        repo_root = Path(__file__).resolve().parent.parent
        napcat_config_dir = repo_root / "napcat" / "config"
        inferred_qq = ""
        inferred_port = None
        if napcat_config_dir.exists():
            for cfg_path in napcat_config_dir.glob("onebot11_*.json"):
                try:
                    nc_cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
                    qq = cfg_path.stem.replace("onebot11_", "")
                    ws_servers = nc_cfg.get("network", {}).get("websocketServers", [])
                    if ws_servers and ws_servers[0].get("enable"):
                        inferred_qq = qq
                        inferred_port = ws_servers[0].get("port")
                        break
                except Exception:
                    continue

        src_bridge = source / "qq_bridge_config.json"
        if src_bridge.exists():
            bridge_cfg = json.loads(src_bridge.read_text(encoding="utf-8"))
            port = inferred_port or self._allocate_port(name)
            if inferred_port and name not in self._load_port_registry():
                # 如果推断到了端口且未分配，直接注册
                ports = self._load_port_registry()
                ports[name] = port
                self._save_port_registry(ports)
            adapters = PersonaAdaptersConfig(
                adapters=[
                    NapCatAdapterConfig(
                        ws_url=f"ws://localhost:{port}",
                        token="napcat_ws",
                        qq_number=inferred_qq,
                        allowed_group_ids=[str(v) for v in bridge_cfg.get("allowed_group_ids", [])],
                        allowed_private_user_ids=[str(v) for v in bridge_cfg.get("allowed_private_user_ids", [])],
                        enable_group_chat=bool(bridge_cfg.get("enable_group_chat", True)),
                        enable_private_chat=bool(bridge_cfg.get("enable_private_chat", True)),
                        root=str(bridge_cfg.get("root", "")),
                    )
                ]
            )
            adapters.save(paths.adapters)
            LOG.info("迁移 adapters.json (端口: %s, QQ: %s)", port, inferred_qq or "未识别")
        else:
            port = inferred_port or self._allocate_port(name)
            adapters = PersonaAdaptersConfig(
                adapters=[
                    NapCatAdapterConfig(
                        ws_url=f"ws://localhost:{port}",
                        qq_number=inferred_qq,
                    )
                ]
            )
            adapters.save(paths.adapters)

        # 4. 迁移 engine_state/
        src_state = source / "engine_state"
        if src_state.exists():
            shutil.copytree(str(src_state), str(paths.engine_state), dirs_exist_ok=True)
            LOG.info("迁移 engine_state/")

        # 5. 迁移 image_cache/
        src_cache = source / "image_cache"
        if src_cache.exists():
            shutil.copytree(str(src_cache), str(paths.image_cache), dirs_exist_ok=True)
            LOG.info("迁移 image_cache/")

        # 6. 迁移其他常见子目录
        for sub in ("memory", "diary", "token", "skill_data", "skills"):
            src_sub = source / sub
            if src_sub.exists():
                dst_sub = pdir / sub
                shutil.copytree(str(src_sub), str(dst_sub), dirs_exist_ok=True)
                LOG.info("迁移 %s/", sub)

        # 7. 生成默认 experience.json
        experience = PersonaExperienceConfig()
        experience.save(paths.experience)
        LOG.info("生成默认 experience.json")

        LOG.info("迁移完成: %s → %s", source, pdir)
        return pdir

    def remove_persona(self, name: str) -> bool:
        """删除人格（先停止进程，再删除目录，最后释放端口）。"""
        pdir = self.personas_dir / name
        if not pdir.exists():
            return False

        self.stop_persona(name)
        try:
            shutil.rmtree(pdir)
            self._release_port(name)
            LOG.info("人格已删除: %s", name)
            return True
        except Exception as exc:
            LOG.error("删除人格失败 %s: %s", name, exc)
            return False

    # ------------------------------------------------------------------
    # 启动与停止
    # ------------------------------------------------------------------

    def start_persona(self, name: str) -> bool:
        """启动单个人格子进程（Windows 下创建独立控制台窗口）。"""
        if self.is_running(name):
            LOG.warning("人格已在运行: %s", name)
            return True

        pdir = self.personas_dir / name
        if not pdir.exists():
            LOG.error("人格不存在: %s", name)
            return False

        cmd = [
            sys.executable,
            "-m",
            "sirius_pulse.persona_worker",
            "--config",
            str(pdir),
            "--log-level",
            self.global_config.get("log_level", "INFO"),
        ]

        kwargs: dict[str, Any] = {}
        if sys.platform == "win32":
            # CREATE_NEW_CONSOLE: 独立窗口
            # CREATE_NEW_PROCESS_GROUP: 支持 Ctrl+Break 终止
            kwargs["creationflags"] = (
                subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NEW_PROCESS_GROUP
            )

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(Path(__file__).resolve().parent.parent),
                **kwargs,
            )
            self._processes[name] = proc
            LOG.info("人格子进程已启动: %s (pid=%s)", name, proc.pid)
            return True
        except Exception as exc:
            LOG.error("启动人格失败 %s: %s", name, exc)
            return False

    def stop_persona(self, name: str, timeout: int = 10) -> bool:
        """停止单个人格子进程。"""
        proc = self._processes.get(name)
        if proc is None:
            # 可能没有 tracked，尝试通过状态文件推断 PID
            status = self._read_worker_status(name)
            pid = status.get("pid") if status else None
            if pid:
                if sys.platform == "win32":
                    # Windows: 使用 taskkill 终止孤儿进程（比 CTRL_BREAK_EVENT 更可靠）
                    try:
                        result = subprocess.run(
                            ["taskkill", "/PID", str(pid), "/T", "/F"],
                            capture_output=True,
                            timeout=10.0,
                        )
                        if result.returncode == 0:
                            LOG.info("已终止孤儿进程: %s (pid=%s)", name, pid)
                        else:
                            LOG.warning("终止孤儿进程失败 %s (pid=%s): %s", name, pid, result.stderr.decode(errors="ignore"))
                        return True
                    except Exception as exc:
                        LOG.warning("终止孤儿进程失败 %s: %s", name, exc)
                else:
                    try:
                        import os as _os
                        _os.kill(pid, signal.SIGTERM)
                        LOG.info("已向孤儿进程发送 SIGTERM: %s (pid=%s)", name, pid)
                        return True
                    except Exception as exc:
                        LOG.warning("终止孤儿进程失败 %s: %s", name, exc)
            return False

        # 先发送 SIGTERM（Windows 用 CTRL_BREAK_EVENT）
        try:
            if sys.platform == "win32":
                proc.send_signal(signal.CTRL_BREAK_EVENT)
            else:
                proc.send_signal(signal.SIGTERM)
        except Exception as exc:
            LOG.warning("发送终止信号失败 %s: %s", name, exc)

        # 等待退出
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            LOG.warning("人格子进程未在规定时间内退出，强制终止: %s", name)
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception as exc:
                LOG.error("强制终止失败 %s: %s", name, exc)

        self._processes.pop(name, None)
        LOG.info("人格已停止: %s", name)
        return True

    def _cleanup_stale_worker_statuses(self) -> None:
        """清理所有过期的 worker_status.json（atexit 钩子）。"""
        for info in self.list_personas():
            name = info["name"]
            status = self._read_worker_status(name)
            if status is None:
                continue
            pid = status.get("pid")
            if pid and not self._is_pid_alive(pid):
                try:
                    status_path = self.personas_dir / name / "engine_state" / "worker_status.json"
                    if status_path.exists():
                        status_path.unlink()
                        LOG.info("已清理过期 worker_status: %s", name)
                except Exception:
                    pass

    @staticmethod
    def _is_pid_alive(pid: int) -> bool:
        """检测 PID 是否存活（Windows 安全）。"""
        try:
            import psutil
            return psutil.pid_exists(pid)
        except Exception:
            if sys.platform == "win32":
                import ctypes
                kernel32 = ctypes.windll.kernel32
                handle = kernel32.OpenProcess(0x0400, False, pid)  # PROCESS_QUERY_INFORMATION
                if handle:
                    kernel32.CloseHandle(handle)
                    return True
                return False
            try:
                import os
                os.kill(pid, 0)
                return True
            except OSError:
                return False

    def is_running(self, name: str) -> bool:
        """检查人格进程是否仍在运行。"""
        proc = self._processes.get(name)
        if proc is not None:
            if proc.poll() is None:
                return True
            # 进程已退出，清理记录
            self._processes.pop(name, None)

        # 通过状态文件检测 PID 存活（支持 CLI 新实例检测）
        status = self._read_worker_status(name)
        pid = status.get("pid") if status else None
        if pid and self._is_pid_alive(pid):
            # 额外检查：进程命令行是否包含 persona_worker（防止 PID 重用）
            if not self._is_pid_persona_worker(pid):
                LOG.warning(
                    "人格 %s 的 worker_status PID=%s 不是 persona_worker，可能是 PID 重用",
                    name, pid,
                )
                return False
            # 额外检查：心跳是否超时（防止 PID 重用或进程僵死）
            heartbeat_at = status.get("heartbeat_at") if status else None
            if heartbeat_at:
                try:
                    from datetime import datetime, timezone
                    last_hb = datetime.fromisoformat(heartbeat_at)
                    if last_hb.tzinfo is None:
                        last_hb = last_hb.replace(tzinfo=timezone.utc)
                    elapsed = (datetime.now(timezone.utc) - last_hb).total_seconds()
                    if elapsed > 60:  # 超过 60 秒没有心跳，认为已死
                        return False
                except Exception:
                    pass
            return True
        return False

    @staticmethod
    def _is_pid_persona_worker(pid: int) -> bool:
        """检查指定 PID 的进程是否真的是 persona_worker。"""
        try:
            import psutil
            proc = psutil.Process(pid)
            cmdline = " ".join(proc.cmdline())
            return "persona_worker" in cmdline
        except Exception:
            return True  # 无法验证时保守返回 True

    def start_all(self) -> dict[str, bool]:
        """启动所有已启用的人格。"""
        results: dict[str, bool] = {}
        for info in self.list_personas():
            name = info["name"]
            if info.get("enabled") and not info.get("running"):
                results[name] = self.start_persona(name)
        return results

    def stop_all(self) -> None:
        """停止所有人格。"""
        for name in list(self._processes.keys()):
            self.stop_persona(name)

    def get_persona_dir(self, name: str) -> Path:
        """获取人格目录。"""
        return self.personas_dir / name

    def get_persona_paths(self, name: str) -> PersonaConfigPaths | None:
        """获取人格配置路径对象。"""
        pdir = self.personas_dir / name
        if not pdir.exists():
            return None
        return PersonaConfigPaths(pdir)

    # ------------------------------------------------------------------
    # WebUI 便捷接口
    # ------------------------------------------------------------------

    def get_persona_status(self, name: str) -> dict[str, Any] | None:
        """获取单个人格的完整状态（供 WebUI 使用）。"""
        info = self._inspect_persona(name)
        if info is None:
            return None

        # 追加详细配置
        paths = self.get_persona_paths(name)
        if paths:
            try:
                adapters = PersonaAdaptersConfig.load(paths.adapters)
                info["adapters"] = [a.to_dict() for a in adapters.adapters]
            except Exception:
                info["adapters"] = []

            try:
                experience = PersonaExperienceConfig.load(paths.experience)
                info["experience"] = experience.to_dict()
            except Exception:
                info["experience"] = {}

        return info

    def reload_persona(self, name: str) -> bool:
        """热重载尚未实现，请使用重启人格功能使配置生效。"""
        LOG.warning("人格 %s 的热重载尚未实现，请使用重启功能", name)
        return False

    def get_logs(self, name: str, lines: int = 50) -> list[str]:
        """读取人格子进程日志文件。"""
        log_file = self.personas_dir / name / "logs" / "worker.log"
        if not log_file.exists():
            return []
        try:
            text = log_file.read_text(encoding="utf-8", errors="ignore")
            all_lines = text.splitlines()
            return all_lines[-lines:] if len(all_lines) > lines else all_lines
        except Exception as exc:
            LOG.warning("读取日志失败 %s: %s", name, exc)
            return []


__all__ = ["PersonaManager"]
