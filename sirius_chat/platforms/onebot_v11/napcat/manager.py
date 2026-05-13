"""NapCat 环境管理器。

负责 NapCat 的自动下载、安装、配置和生命周期管理。

依赖:
    - httpx (下载 NapCat Release)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path

import websockets
import websockets.exceptions

LOG = logging.getLogger("napcat_manager")

GITHUB_API = "https://api.github.com/repos/NapNeko/NapCatQQ/releases/latest"
ASSET_NAME = "NapCat.Shell.zip"


class NapCatManager:
    """NapCat 环境管理器。

    提供安装检查、自动下载、配置生成、启动/停止、日志读取等功能。
    """

    def __init__(
        self,
        install_dir: str | Path,
        instance_dir: str | Path | None = None,
    ) -> None:
        self.install_dir = Path(install_dir).resolve()
        self.instance_dir = Path(instance_dir).resolve() if instance_dir else self.install_dir
        self.config_dir = self.instance_dir / "config"
        self.logs_dir = self.instance_dir / "logs"
        self._pid_file = self.instance_dir / "napcat.pid"
        self._process: subprocess.Popen | None = None

    @classmethod
    def for_persona(
        cls,
        global_install_dir: str | Path,
        persona_name: str,
        instances_root: str | Path | None = None,
    ) -> "NapCatManager":
        """为指定人格创建 NapCat 实例管理器。

        实例目录结构::

            {instances_root or global_install_dir}/instances/{persona_name}/
                ├── config/         # 独立配置
                ├── logs/           # 独立日志
                └── qqnt.json       # 从全局复制
        """
        global_dir = Path(global_install_dir).resolve()
        if instances_root is None:
            instances_root = global_dir / "instances"
        else:
            instances_root = Path(instances_root).resolve()

        instance_dir = instances_root / persona_name
        instance_dir.mkdir(parents=True, exist_ok=True)

        # 复制必要的全局文件到实例目录（如果不存在）
        for filename in ("qqnt.json",):
            src = global_dir / filename
            dst = instance_dir / filename
            if src.exists() and not dst.exists():
                shutil.copy2(str(src), str(dst))

        return cls(global_install_dir, instance_dir)

    # ── 状态检查 ─────────────────────────────────────────

    @property
    def is_installed(self) -> bool:
        """检查 NapCat 是否已安装（通过核心文件 napcat.mjs 判断）。"""
        return (self.install_dir / "napcat.mjs").exists()

    # 常见 QQ 安装路径（用于注册表失效时的回退检测）
    _COMMON_QQ_PATHS: tuple[Path, ...] = (
        Path(r"C:\Program Files\Tencent\QQNT\QQ.exe"),
        Path(r"C:\Program Files (x86)\Tencent\QQNT\QQ.exe"),
        Path(r"C:\Program Files\Tencent\QQ\Bin\QQ.exe"),
        Path(r"C:\Program Files (x86)\Tencent\QQ\Bin\QQ.exe"),
        Path.home() / "AppData" / "Local" / "Tencent" / "QQNT" / "QQ.exe",
        Path.home() / "AppData" / "Roaming" / "Tencent" / "QQNT" / "QQ.exe",
        Path.home() / "Tencent" / "QQNT" / "QQ.exe",
    )

    @staticmethod
    def _strip_quotes(value: str) -> str:
        """去除注册表值首尾可能出现的引号。"""
        v = value.strip()
        if (v.startswith('"') and v.endswith('"')) or (v.startswith("'") and v.endswith("'")):
            return v[1:-1]
        return v

    @classmethod
    def is_qq_installed(cls) -> bool:
        """检查 QQ 是否安装（仅 Windows）。

        优先读取注册表，失败时回退到常见路径扫描。
        """
        if sys.platform != "win32":
            return False
        # 注册表检测
        try:
            import winreg

            key = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\QQ",
            )
            winreg.QueryValueEx(key, "UninstallString")
            winreg.CloseKey(key)
            return True
        except Exception:
            pass
        # 回退：扫描常见路径
        for p in cls._COMMON_QQ_PATHS:
            if p.exists():
                return True
        return False

    @classmethod
    def get_qq_path(cls) -> str | None:
        """获取 QQ.exe 完整路径（仅 Windows）。

        依次尝试：
        1. 注册表 UninstallString 推导
        2. 注册表 InstallLocation 推导
        3. 常见路径扫描
        """
        if sys.platform != "win32":
            return None
        import winreg

        # 1. 尝试 UninstallString
        try:
            key = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\QQ",
            )
            value, _ = winreg.QueryValueEx(key, "UninstallString")
            winreg.CloseKey(key)
            uninstall_path = Path(cls._strip_quotes(value))
            qq_path = uninstall_path.parent / "QQ.exe"
            if qq_path.exists():
                return str(qq_path)
        except Exception:
            pass

        # 2. 尝试 InstallLocation
        try:
            key = winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE,
                r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall\QQ",
            )
            value, _ = winreg.QueryValueEx(key, "InstallLocation")
            winreg.CloseKey(key)
            install_dir = Path(cls._strip_quotes(value))
            candidates = [
                install_dir / "QQ.exe",
                install_dir / "QQNT" / "QQ.exe",
                install_dir / "Bin" / "QQ.exe",
            ]
            for c in candidates:
                if c.exists():
                    return str(c)
        except Exception:
            pass

        # 3. 回退到常见路径扫描
        for p in cls._COMMON_QQ_PATHS:
            if p.exists():
                return str(p)
        return None

    @property
    def is_running(self) -> bool:
        """检查 NapCat 进程是否仍在运行（含跨进程 pid 文件检测）。

        注意：不通过 QQ.exe 进程名判断，因为用户的普通 QQ 也会被检测到。
        """
        if self._process is not None and self._process.poll() is None:
            return True
        # 跨进程检测：如果另一个 CLI/WebUI 进程已启动同一实例
        pid = self._read_pid_file()
        if pid is not None and self._is_process_alive(pid):
            return True
        return False

    @staticmethod
    def _is_qq_process_running() -> bool:
        """按进程名检测 QQ/NapCat 是否仍在运行（Windows 用 tasklist，Linux 用 pgrep）。"""
        if sys.platform == "win32":
            try:
                result = subprocess.run(
                    ["tasklist", "/FI", "IMAGENAME eq QQ.exe", "/FO", "CSV", "/NH"],
                    capture_output=True,
                    text=True,
                    timeout=5.0,
                )
                return "QQ.exe" in result.stdout
            except Exception:
                return False
        else:
            try:
                result = subprocess.run(
                    ["pgrep", "-f", "napcat"],
                    capture_output=True,
                    timeout=5.0,
                )
                return result.returncode == 0
            except Exception:
                return False

    # ── pid 文件辅助 ─────────────────────────────────────

    def _read_pid_file(self) -> int | None:
        """读取 pid 文件中的进程号，文件不存在或格式错误返回 None。"""
        if not self._pid_file.exists():
            return None
        try:
            text = self._pid_file.read_text(encoding="utf-8").strip()
            return int(text)
        except (ValueError, OSError):
            return None

    def _write_pid_file(self, pid: int) -> None:
        """将进程号写入 pid 文件。"""
        try:
            self._pid_file.write_text(str(pid), encoding="utf-8")
        except OSError as exc:
            LOG.warning("写入 pid 文件失败: %s", exc)

    def _remove_pid_file(self) -> None:
        """删除 pid 文件。"""
        try:
            if self._pid_file.exists():
                self._pid_file.unlink()
        except OSError as exc:
            LOG.warning("删除 pid 文件失败: %s", exc)

    @staticmethod
    def _is_process_alive(pid: int) -> bool:
        """检查指定 pid 的进程是否仍在运行。"""
        try:
            import psutil
            return psutil.pid_exists(pid)
        except Exception:
            # fallback: 尝试发送信号 0（Unix）或 ctypes OpenProcess（Windows）
            if sys.platform == "win32":
                import ctypes
                kernel32 = ctypes.windll.kernel32
                handle = kernel32.OpenProcess(0x0400, False, pid)  # PROCESS_QUERY_INFORMATION
                if handle:
                    kernel32.CloseHandle(handle)
                    return True
                return False
            else:
                try:
                    os.kill(pid, 0)
                    return True
                except OSError:
                    return False

    # ── 安装 ─────────────────────────────────────────────

    async def install(self, version: str = "latest") -> dict:
        """从 GitHub Release 下载并安装 NapCat。

        Args:
            version: 目标版本标签，默认 latest。

        Returns:
            {"success": bool, "message": str}
        """
        if self.is_installed:
            return {"success": True, "message": "NapCat 已安装"}

        try:
            tag, download_url = await self._fetch_release_info(version)
        except Exception as exc:
            LOG.error("获取 NapCat Release 信息失败: %s", exc)
            return {
                "success": False,
                "message": f"获取 Release 信息失败: {exc}。请检查网络连接或手动下载 NapCat 到 {self.install_dir}",
            }

        LOG.info("正在下载 NapCat %s ...", tag)
        try:
            zip_path = await self._download_file(download_url)
        except Exception as exc:
            LOG.error("下载 NapCat 失败: %s", exc)
            return {"success": False, "message": f"下载失败: {exc}"}

        try:
            self._extract_zip(zip_path)
        except Exception as exc:
            LOG.error("解压 NapCat 失败: %s", exc)
            return {"success": False, "message": f"解压失败: {exc}"}
        finally:
            try:
                os.remove(zip_path)
            except Exception:
                pass

        LOG.info("NapCat %s 安装完成", tag)
        return {"success": True, "message": f"NapCat {tag} 安装完成"}

    async def _fetch_release_info(self, version: str) -> tuple[str, str]:
        """获取 GitHub Release 信息，返回 (tag, download_url)。"""
        try:
            import httpx
        except ImportError:
            raise RuntimeError("安装 NapCat 需要 httpx: pip install httpx")

        async with httpx.AsyncClient(timeout=30.0, follow_redirects=True) as client:
            if version == "latest":
                resp = await client.get(GITHUB_API, headers={"Accept": "application/vnd.github.v3+json"})
                resp.raise_for_status()
                data = resp.json()
                tag = data["tag_name"]
                assets = data.get("assets", [])
            else:
                url = f"https://api.github.com/repos/NapNeko/NapCatQQ/releases/tags/{version}"
                resp = await client.get(url, headers={"Accept": "application/vnd.github.v3+json"})
                resp.raise_for_status()
                data = resp.json()
                tag = data["tag_name"]
                assets = data.get("assets", [])

            for asset in assets:
                if asset["name"] == ASSET_NAME:
                    return tag, asset["browser_download_url"]

            raise RuntimeError(f"Release {tag} 中未找到资源 {ASSET_NAME}")

    async def _download_file(self, url: str) -> str:
        """流式下载文件到临时目录，返回本地路径。"""
        import httpx

        fd, path = tempfile.mkstemp(suffix=".zip")
        os.close(fd)

        async with httpx.AsyncClient(timeout=300.0, follow_redirects=True) as client:
            async with client.stream("GET", url) as resp:
                resp.raise_for_status()
                total = int(resp.headers.get("content-length", 0))
                downloaded = 0
                chunk_size = 65536
                with open(path, "wb") as f:
                    async for chunk in resp.aiter_bytes(chunk_size=chunk_size):
                        f.write(chunk)
                        downloaded += len(chunk)
                        if total > 0 and downloaded % (chunk_size * 16) == 0:
                            LOG.info("下载进度: %.1f%%", downloaded / total * 100)

        return path

    def _extract_zip(self, zip_path: str) -> None:
        """解压 ZIP 到 install_dir。"""
        self.install_dir.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(zip_path, "r") as zf:
            # 如果 ZIP 根目录只有一个文件夹，先去掉那一层
            top_dirs = {name.split("/")[0] for name in zf.namelist() if "/" in name}
            if len(top_dirs) == 1:
                prefix = list(top_dirs)[0] + "/"
                for member in zf.namelist():
                    if member.startswith(prefix):
                        target = self.install_dir / member[len(prefix):]
                        if member.endswith("/"):
                            target.mkdir(parents=True, exist_ok=True)
                        else:
                            target.parent.mkdir(parents=True, exist_ok=True)
                            with zf.open(member) as src, open(target, "wb") as dst:
                                shutil.copyfileobj(src, dst)
            else:
                zf.extractall(self.install_dir)

    # ── 配置 ─────────────────────────────────────────────

    def configure(
        self,
        qq_number: str,
        ws_port: int = 3001,
        ws_token: str = "napcat_ws",
        report_self_message: bool = False,
    ) -> dict:
        """生成 NapCat 配置文件（merge 模式，保留用户手动修改的字段）。

        会生成/更新两个文件:
            - config/napcat_{qq}.json   NapCat 核心配置
            - config/onebot11_{qq}.json OneBot v11 协议配置

        Returns:
            {"success": bool, "message": str}
        """
        if not self.is_installed:
            return {"success": False, "message": "NapCat 未安装，请先安装"}

        self.config_dir.mkdir(parents=True, exist_ok=True)

        # NapCat 核心配置 — merge 现有配置
        napcat_defaults = {
            "fileLog": False,
            "consoleLog": True,
            "fileLogLevel": "debug",
            "consoleLogLevel": "info",
            "packetBackend": "auto",
            "packetServer": "",
            "o3HookMode": 1,
            "bypass": {
                "hook": False,
                "window": False,
                "module": False,
                "process": False,
                "container": False,
                "js": False,
            },
            "autoTimeSync": True,
        }
        napcat_path = self.config_dir / f"napcat_{qq_number}.json"
        napcat_config = self._load_json(napcat_path, napcat_defaults)
        napcat_path.write_text(
            json.dumps(napcat_config, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        # OneBot v11 协议配置 — merge 现有配置，只更新 WS 服务器参数
        onebot_defaults = {
            "network": {
                "websocketServers": [
                    {
                        "enable": True,
                        "name": "WsServer",
                        "host": "localhost",
                        "port": ws_port,
                        "reportSelfMessage": report_self_message,
                        "enableForcePushEvent": True,
                        "messagePostFormat": "array",
                        "token": ws_token,
                        "debug": False,
                        "heartInterval": 30000,
                    }
                ],
                "httpServers": [],
                "httpSseServers": [],
                "httpClients": [],
                "websocketClients": [],
                "plugins": [],
            },
            "musicSignUrl": "",
            "enableLocalFile2Url": False,
            "parseMultMsg": False,
            "imageDownloadProxy": "",
            "timeout": {
                "baseTimeout": 10000,
                "uploadSpeedKBps": 256,
                "downloadSpeedKBps": 256,
                "maxTimeout": 1800000,
            },
        }
        onebot_path = self.config_dir / f"onebot11_{qq_number}.json"
        onebot_config = self._load_json(onebot_path, onebot_defaults)
        # 精确更新 websocketServers[0] 的关键字段，保留其他服务器配置
        ws_servers = onebot_config.setdefault("network", {}).setdefault("websocketServers", [])
        if not ws_servers:
            ws_servers.append(onebot_defaults["network"]["websocketServers"][0])
        ws0 = ws_servers[0]
        ws0["enable"] = True
        ws0["name"] = "WsServer"
        ws0["host"] = "localhost"
        ws0["port"] = ws_port
        ws0["reportSelfMessage"] = report_self_message
        ws0["enableForcePushEvent"] = True
        ws0["messagePostFormat"] = "array"
        ws0["token"] = ws_token
        ws0["debug"] = False
        ws0["heartInterval"] = 30000
        onebot_path.write_text(
            json.dumps(onebot_config, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )

        LOG.info("NapCat 配置已生成: %s", self.config_dir)
        return {"success": True, "message": f"配置已生成 (QQ: {qq_number}, WS: localhost:{ws_port})"}

    @staticmethod
    def _load_json(path: Path, defaults: dict) -> dict:
        """读取 JSON 文件，不存在或解析失败时返回 defaults 的深拷贝。"""
        if not path.exists():
            return json.loads(json.dumps(defaults))
        try:
            with path.open("r", encoding="utf-8") as f:
                existing = json.load(f)
            if not isinstance(existing, dict):
                return json.loads(json.dumps(defaults))
            # 递归 merge：defaults 为底，existing 覆盖
            return NapCatManager._deep_merge(json.loads(json.dumps(defaults)), existing)
        except Exception:
            return json.loads(json.dumps(defaults))

    @staticmethod
    def _deep_merge(base: dict, override: dict) -> dict:
        """递归合并两个字典，override 覆盖 base 的同键值。"""
        result = dict(base)
        for key, value in override.items():
            if key in result and isinstance(result[key], dict) and isinstance(value, dict):
                result[key] = NapCatManager._deep_merge(result[key], value)
            else:
                result[key] = value
        return result

    # ── 启动 / 停止 ──────────────────────────────────────

    async def start(self, qq_number: str | None = None) -> dict:
        """启动 NapCat。

        Windows 下通过 NapCatWinBootMain.exe 注入 QQ 并启动。
        实例模式下，二进制从 install_dir 加载，但运行时 cwd 在 instance_dir。

        Args:
            qq_number: QQ 号，提供则使用快速登录；省略则使用二维码登录。

        Returns:
            {"success": bool, "message": str}
        """
        # 检查是否已运行（含跨进程 PID 文件检测）
        if self.is_running:
            return {"success": True, "message": "NapCat 已在运行"}

        # 清理过期的 pid 文件（进程已死但文件残留）
        self._remove_pid_file()

        if self._is_qq_process_running():
            LOG.warning("检测到 QQ.exe 正在运行，NapCat 将尝试注入现有 QQ 或启动新实例")

        if not self.is_installed:
            return {"success": False, "message": "NapCat 未安装"}

        qq_path = self.get_qq_path()
        if not qq_path:
            return {
                "success": False,
                "message": "未检测到 QQ 安装。请先安装 QQ 客户端（支持 QQNT 9.9.x）。",
            }

        # 二进制从全局安装目录加载
        launcher = self.install_dir / "NapCatWinBootMain.exe"
        hook = self.install_dir / "NapCatWinBootHook.dll"
        main_script = self.install_dir / "napcat.mjs"
        # loadNapCat.js 在实例目录生成（避免冲突）
        load_script = self.instance_dir / "loadNapCat.js"

        if not launcher.exists():
            return {"success": False, "message": f"启动器不存在: {launcher}"}
        if not hook.exists():
            return {"success": False, "message": f"注入 DLL 不存在: {hook}"}

        # 生成 loadNapCat.js
        mjs_path = str(main_script).replace("\\", "/")
        load_script.write_text(
            f'(async () => {{await import("file:///{mjs_path}")}})()',
            encoding="utf-8",
        )

        # 准备环境变量
        env = os.environ.copy()
        # qqnt.json 优先使用实例目录的，fallback 到全局
        qqnt_path = self.instance_dir / "qqnt.json"
        if not qqnt_path.exists():
            qqnt_path = self.install_dir / "qqnt.json"
        env["NAPCAT_PATCH_PACKAGE"] = str(qqnt_path)
        env["NAPCAT_LOAD_PATH"] = str(load_script)
        env["NAPCAT_INJECT_PATH"] = str(hook)
        env["NAPCAT_LAUNCHER_PATH"] = str(launcher)
        env["NAPCAT_MAIN_PATH"] = str(main_script)

        cmd = [str(launcher), qq_path, str(hook)]
        if qq_number:
            cmd.extend(["-q", qq_number])

        LOG.info("正在启动 NapCat (实例: %s): %s", self.instance_dir.name, " ".join(cmd))
        try:
            # Windows 下使用 CREATE_NEW_CONSOLE 让 QQ 窗口独立显示
            creationflags = subprocess.CREATE_NEW_CONSOLE if sys.platform == "win32" else 0
            self._process = subprocess.Popen(
                cmd,
                env=env,
                cwd=str(self.instance_dir),
                creationflags=creationflags,
            )
        except Exception as exc:
            LOG.error("启动 NapCat 失败: %s", exc)
            return {"success": False, "message": f"启动失败: {exc}"}

        LOG.info("NapCat 进程已启动 (pid=%s)", self._process.pid)
        self._write_pid_file(self._process.pid)
        return {
            "success": True,
            "message": f"NapCat 已启动 (pid={self._process.pid})。首次使用请在弹出的 QQ 窗口中扫码登录。",
        }

    async def stop(self) -> dict:
        """停止 NapCat 进程。

        Windows 下不直接 kill QQ 进程（NapCat 与 QQ 同进程，会误杀用户普通 QQ），
        仅断开内部引用并清理 pid 文件。Linux 下保持原有 terminate/kill 逻辑。
        """
        if not self.is_running:
            self._process = None
            self._remove_pid_file()
            return {"success": True, "message": "NapCat 未在运行"}

        if sys.platform == "win32":
            # Windows：只清理引用，不杀进程（避免关闭用户普通 QQ）
            if self._process is not None:
                try:
                    self._process.terminate()
                except Exception:
                    pass
                self._process = None
            self._remove_pid_file()
            LOG.info("Windows 下已断开 NapCat 引用（QQ 进程保持运行）")
            return {
                "success": True,
                "message": "Windows 下已断开 NapCat 引用（QQ 进程保持运行）",
            }

        # Linux：直接 terminate/kill
        try:
            self._process.terminate()  # type: ignore[union-attr]
            await asyncio.wait_for(
                asyncio.to_thread(self._process.wait),  # type: ignore[union-attr]
                timeout=10.0,
            )
        except asyncio.TimeoutError:
            LOG.warning("NapCat 进程未在 10 秒内退出，强制结束")
            self._process.kill()  # type: ignore[union-attr]
        except Exception as exc:
            LOG.error("停止 NapCat 失败: %s", exc)
            return {"success": False, "message": f"停止失败: {exc}"}

        self._process = None
        self._remove_pid_file()
        LOG.info("NapCat 已停止")
        return {"success": True, "message": "NapCat 已停止"}

    # ── 等待就绪 ─────────────────────────────────────────

    async def wait_for_ws(
        self,
        host: str = "localhost",
        port: int = 3001,
        token: str | None = None,
        timeout: float = 120.0,
    ) -> dict:
        """轮询等待 NapCat WebSocket 真正就绪（真实握手 + 接收首条消息验证）。

        首次启动时 QQ 需要扫码，超时时间建议设长一些。

        Returns:
            {"ready": bool, "self_id": str | None, "error": str | None}
        """
        ws_url = f"ws://{host}:{port}"
        warned_5 = False
        warned_10 = False
        start = time.time()
        expire_time = start + timeout

        while time.time() < expire_time:
            # 阶段 1：快速 TCP 端口检测（避免每次都做 WS 握手）
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port),
                    timeout=2.0,
                )
                writer.close()
                await writer.wait_closed()
            except Exception:
                await asyncio.sleep(2.0)
                continue

            # 阶段 2：真实 WebSocket 握手 + 验证
            try:
                headers: dict[str, str] = {}
                if token:
                    headers["Authorization"] = f"Bearer {token}"
                async with websockets.connect(
                    ws_url,
                    additional_headers=headers,
                    open_timeout=5.0,
                    close_timeout=2.0,
                ) as ws:
                    # NapCat 连接后会发送一条 meta_event / lifecycle 消息
                    raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
                    data = json.loads(raw)
                    self_id = data.get("self_id")
                    if self_id:
                        LOG.info(
                            "NapCat WebSocket 已就绪 (%s:%s, QQ=%s)",
                            host, port, self_id,
                        )
                        return {"ready": True, "self_id": str(self_id), "error": None}
                    # 收到消息但没有 self_id，也认为是就绪（可能是其他事件）
                    return {"ready": True, "self_id": None, "error": None}
            except websockets.exceptions.InvalidStatusCode as exc:
                if exc.status_code == 401:
                    LOG.warning("NapCat WebSocket Token 错误 (401)")
                    return {"ready": False, "self_id": None, "error": "WebSocket Token 错误"}
            except Exception:
                pass

            elapsed = time.time() - start
            if not warned_5 and elapsed >= 5:
                LOG.warning("NapCat WebSocket 已等待 5s 仍未就绪...")
                warned_5 = True
            if not warned_10 and elapsed >= 10:
                LOG.warning("NapCat WebSocket 已等待 10s 仍未就绪...")
                warned_10 = True

            await asyncio.sleep(2.0)

        LOG.warning("等待 NapCat WebSocket 超时 (%s 秒)", timeout)
        return {"ready": False, "self_id": None, "error": f"超时 ({timeout}s)"}

    # ── 日志 ─────────────────────────────────────────────

    def get_logs(self, lines: int = 100) -> list[str]:
        """读取 NapCat 日志文件（从 logs/ 目录读取最新的日志）。"""
        if not self.logs_dir.exists():
            return []

        log_files = sorted(
            [f for f in self.logs_dir.iterdir() if f.suffix == ".log"],
            key=lambda f: f.stat().st_mtime,
            reverse=True,
        )
        if not log_files:
            return []

        try:
            text = log_files[0].read_text(encoding="utf-8", errors="ignore")
            all_lines = text.splitlines()
            return all_lines[-lines:] if len(all_lines) > lines else all_lines
        except Exception as exc:
            LOG.warning("读取日志失败: %s", exc)
            return []

    def get_status(self) -> dict:
        """获取 NapCat 完整状态信息。"""
        qq_running = self._is_qq_process_running()
        return {
            "installed": self.is_installed,
            "running": self.is_running,
            "qq_installed": self.is_qq_installed(),
            "qq_path": self.get_qq_path(),
            "qq_running": qq_running,
            "install_dir": str(self.install_dir),
        }
