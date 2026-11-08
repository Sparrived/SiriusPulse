"""LLM 传输侧车（Go）的进程生命周期。

侧车是**唯一**的传输实现——Python 侧既不做对话补全的 HTTP/SSE 聚合，也不做生图，
因此它由 ``_cmd_run`` 无条件拉起，没有开关。它只承担「对 AMKR 的 HTTP 调用 +
SSE 聚合 + 多模态内联 + 生图」，prompt 组装、工具循环、重试、兜底任务与 token
记账仍全部留在 Python。

进程与网络这一侧的约定见 ``go/llm-transport/CONTRACT.md`` §1：

- 监听固定 ``127.0.0.1``；端口由 Python 先绑 0 拿一个空闲端口再传 ``--port``；
- 鉴权 token 由 Python 现生成、只存在内存里（``--token``），不落盘；
- 就绪信号是 stdout 的单行 ``READY <port>``，其余日志全部走 stderr；
- ``GET /health`` 免鉴权，返回语义化版本，主版本必须为 ``0``（契约 §8）。

设计取舍：

- **进程级单例**。侧车是无状态的传输层，每个人格一份只会多占端口与内存，因此
  多个 persona 共用一个进程（:func:`get_sidecar`）。
- **重启权在 Python**。契约明确 Go 侧不做自动重启，崩溃后的重启发生在
  :meth:`LLMSidecar.ensure_running`：它在每次生成前被调用，存活检查只是一次
  ``poll()``，只有进程真的死了才走启动流程。
- **启动失败即不就绪**。没有降级路径：调用方（``cli.py``）直接 ``SystemExit(1)``，
  ``platforms/runtime.py::_build_provider`` 返回 ``None``。本模块只负责抛
  :class:`SidecarError` 并留日志，不替调用方做决定。
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import platform
import secrets
import shutil
import socket
import subprocess
import threading
import time
from pathlib import Path
from typing import IO
from urllib import request as urllib_request

LOGGER = logging.getLogger(__name__)

__all__ = [
    "BINARY_ENV_VAR",
    "DEFAULT_BINARY_DIR",
    "LLMSidecar",
    "SidecarError",
    "allocate_free_port",
    "get_sidecar",
    "resolve_binary",
    "stop_sidecar",
]

#: 仓库根目录：本文件是 ``sirius_pulse/utils/llm_sidecar.py``。二进制查找必须
#: 从这里推导而不是靠 cwd——``run`` 子命令不保证从仓库根启动。
PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: ``scripts/build_llm_transport.py`` 的默认产出目录（相对仓库根）。
DEFAULT_BINARY_DIR = Path("go") / "llm-transport" / "bin"

BINARY_NAME = "llm-transport.exe" if platform.system() == "Windows" else "llm-transport"

#: 环境变量形态的二进制路径（配置里 ``llm_transport_binary`` 优先于它）。
BINARY_ENV_VAR = "SIRIUS_LLM_TRANSPORT_BIN"

#: 契约 §1 的就绪信号前缀。
_READY_PREFIX = "READY "

#: 契约 §8：v1 契约期内侧车版本主版本必须是 ``0``。
SUPPORTED_MAJOR_VERSION = 0

STARTUP_TIMEOUT_SECONDS = 10.0
HEALTH_TIMEOUT_SECONDS = 5.0
HEALTH_POLL_INTERVAL_SECONDS = 0.1
HEALTH_REQUEST_TIMEOUT_SECONDS = 2.0

#: ``stop()`` 的预算：先 terminate 等这么久，仍不退出再 kill。
STOP_TIMEOUT_SECONDS = 5.0


class SidecarError(RuntimeError):
    """侧车定位/启动/健康检查失败。

    本异常只描述「为什么起不来」，不包含任何降级动作——Python 侧没有可回退的
    传输实现，失败就是不就绪。
    """


def resolve_binary(configured: str = "") -> Path | None:
    """按约定顺序定位侧车二进制，找不到返回 ``None``。

    顺序：配置 ``llm_transport_binary`` → 环境变量 ``SIRIUS_LLM_TRANSPORT_BIN``
    → 仓库内 ``go/llm-transport/bin/llm-transport[.exe]`` → ``PATH``。

    配置/环境变量指向的路径不存在时只记 WARNING 并继续往下找：换机器时配置里
    留着一个旧绝对路径是很常见的，因此不能因为它就放弃后面两档。
    """
    for raw in (str(configured or "").strip(), os.getenv(BINARY_ENV_VAR, "").strip()):
        if not raw:
            continue
        candidate = Path(raw).expanduser()
        if candidate.is_file():
            return candidate
        LOGGER.warning("配置的 LLM 传输层二进制不存在，继续按默认顺序查找: %s", candidate)

    bundled = PROJECT_ROOT / DEFAULT_BINARY_DIR / BINARY_NAME
    if bundled.is_file():
        return bundled

    found = shutil.which(BINARY_NAME)
    return Path(found) if found else None


def allocate_free_port() -> int:
    """先绑 0 取一个空闲端口再立刻释放（契约 §1 的做法）。

    这里有一个固有竞态：释放到侧车真正 bind 之间，端口可能被别人抢走。契约要求
    的就是这个流程，所以侧车必须自己处理 bind 失败（它会在 stderr 报错、不打印
    READY），Python 侧据此判为启动失败。
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class LLMSidecar:
    """一个 Go 侧车进程。

    不要直接实例化：进程级单例走 :func:`get_sidecar`。直接 new 只在测试或需要
    独立实例时使用。
    """

    def __init__(
        self,
        *,
        binary: str = "",
        port: int = 0,
        startup_timeout_seconds: float = STARTUP_TIMEOUT_SECONDS,
    ) -> None:
        self._binary_config = str(binary or "").strip()
        self._configured_port = int(port or 0)
        self._startup_timeout_seconds = startup_timeout_seconds
        self._lock = threading.RLock()
        self._process: subprocess.Popen[str] | None = None
        self._binary_path: Path | None = None
        self._port = 0
        self._token = ""
        self._version = ""
        self._ready_event = threading.Event()
        self._ready_port = 0

    # ── 状态查询 ──────────────────────────────────────────────

    @property
    def base_url(self) -> str:
        """侧车根地址；未启动时为空串。"""
        return f"http://127.0.0.1:{self._port}" if self._port else ""

    @property
    def token(self) -> str:
        """随启动参数传给侧车的 Bearer token。"""
        return self._token

    @property
    def version(self) -> str:
        """``/health`` 返回的语义化版本。"""
        return self._version

    @property
    def pid(self) -> int | None:
        process = self._process
        return process.pid if process is not None and process.poll() is None else None

    @property
    def binary_path(self) -> Path | None:
        """本次（或上次）启动所用的二进制路径。"""
        return self._binary_path

    def is_alive(self) -> bool:
        """进程是否还活着（O(1)，可直接放在热路径上）。"""
        process = self._process
        return process is not None and process.poll() is None

    def matches(self, *, binary: str = "", port: int = 0) -> bool:
        """配置是否与当前进程一致；不一致说明该重建了。"""
        return self._binary_config == str(binary or "").strip() and self._configured_port == int(
            port or 0
        )

    # ── 启动与停止 ────────────────────────────────────────────

    def start(self) -> None:
        """拉起侧车，等到 ``READY`` 且 ``/health`` 通过。失败抛 :class:`SidecarError`。"""
        with self._lock:
            if self.is_alive():
                return
            self._abandon_process()
            binary = resolve_binary(self._binary_config)
            if binary is None:
                raise SidecarError(
                    "未找到 LLM 传输层二进制。请先运行 "
                    "`python scripts/build_llm_transport.py`，"
                    f"或用 llm_transport_binary / {BINARY_ENV_VAR} 指向已有二进制。"
                )
            port = self._configured_port or allocate_free_port()
            token = secrets.token_urlsafe(32)
            LOGGER.info("正在启动 LLM 传输层侧车: %s (port=%s)", binary, port)
            try:
                process = subprocess.Popen(
                    [str(binary), "--port", str(port), "--token", token],
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    cwd=str(PROJECT_ROOT),
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    creationflags=_creation_flags(),
                )
            except OSError as exc:
                raise SidecarError(f"启动 LLM 传输层侧车失败：{exc}") from exc

            self._process = process
            self._binary_path = binary
            self._token = token
            self._port = 0
            self._version = ""
            self._ready_port = 0
            self._ready_event = threading.Event()
            self._start_log_pumps(process)

            try:
                ready_port = self._wait_for_ready(process)
                self._port = ready_port
                self._version = self._wait_for_health(process)
            except SidecarError:
                self._abandon_process()
                raise
            LOGGER.info(
                "LLM 传输层侧车已就绪: %s (pid=%s, version=%s)",
                self.base_url,
                process.pid,
                self._version or "(未知)",
            )

    def ensure_running(self) -> str:
        """保证侧车在跑，返回 ``base_url``；起不来时返回空串。

        这是崩溃重启的唯一入口（契约：Go 侧不自动重启）。存活时只是一次
        ``poll()``，因此可以放在每次生成之前的路径上；只有进程真的死了才会走到
        启动流程——那一步是阻塞的，但属于异常恢复，不在稳态热路径上。
        """
        if self.is_alive():
            return self.base_url
        LOGGER.warning("LLM 传输层侧车未在运行，尝试重新启动")
        try:
            self.start()
        except SidecarError as exc:
            LOGGER.error("LLM 传输层侧车重启失败: %s", exc)
            return ""
        return self.base_url

    def stop(self) -> None:
        """优雅退出：terminate → 等 :data:`STOP_TIMEOUT_SECONDS` → kill。幂等。"""
        with self._lock:
            process = self._process
            self._process = None
            self._port = 0
            self._token = ""
            self._version = ""
        if process is None:
            return
        if process.poll() is None:
            LOGGER.info("正在停止 LLM 传输层侧车 (pid=%s)", process.pid)
            try:
                process.terminate()
                process.wait(timeout=STOP_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                # Windows 上 terminate() 等于 TerminateProcess，几乎不会走到这里；
                # POSIX 上一个卡在在飞请求里的侧车会用到这条兜底。
                LOGGER.warning("LLM 传输层侧车 %.0f 秒内未退出，强制结束", STOP_TIMEOUT_SECONDS)
                _kill(process)
            except OSError as exc:
                LOGGER.warning("停止 LLM 传输层侧车失败: %s", exc)
                _kill(process)

    # ── 内部：等待就绪与健康检查 ──────────────────────────────

    def _wait_for_ready(self, process: subprocess.Popen[str]) -> int:
        """等 stdout 的 ``READY <port>``；进程提前退出或超时都算失败。"""
        deadline = time.monotonic() + self._startup_timeout_seconds
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise SidecarError(
                    f"LLM 传输层侧车启动后立即退出（exit={process.returncode}），" "详见上方 stderr 日志。"
                )
            if self._ready_event.wait(HEALTH_POLL_INTERVAL_SECONDS) and self._ready_port:
                return self._ready_port
        raise SidecarError(f"LLM 传输层侧车在 {self._startup_timeout_seconds:.0f} 秒内没有打印 READY。")

    def _wait_for_health(self, process: subprocess.Popen[str]) -> str:
        """轮询 ``/health``；成功返回版本串。主版本不符按契约 §8 判为不兼容。"""
        deadline = time.monotonic() + HEALTH_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise SidecarError(f"LLM 传输层侧车在健康检查期间退出（exit={process.returncode}）。")
            ready, version = self._probe_health()
            if ready:
                major = _version_major(version)
                if major is None:
                    LOGGER.warning("LLM 传输层侧车版本号无法解析（version=%r），按兼容处理", version)
                    return version
                if major != SUPPORTED_MAJOR_VERSION:
                    raise SidecarError(
                        f"LLM 传输层契约版本不兼容：侧车 version={version}，"
                        f"本框架只支持主版本 {SUPPORTED_MAJOR_VERSION}（见 CONTRACT.md §8）。"
                    )
                return version
            time.sleep(HEALTH_POLL_INTERVAL_SECONDS)
        raise SidecarError(f"LLM 传输层侧车在 {HEALTH_TIMEOUT_SECONDS:.0f} 秒内未通过 /health 健康检查。")

    def _probe_health(self) -> tuple[bool, str]:
        """一次 ``GET /health``（免鉴权）。"""
        try:
            with urllib_request.urlopen(  # noqa: S310 - 固定回环地址
                f"{self.base_url}/health",
                timeout=HEALTH_REQUEST_TIMEOUT_SECONDS,
            ) as response:
                payload = json.loads(response.read().decode("utf-8", errors="replace") or "{}")
        except Exception as exc:  # 连接拒绝/超时/坏 JSON 都只表示「还没好」
            LOGGER.debug("LLM 传输层健康检查失败: %s", exc)
            return False, ""
        if not isinstance(payload, dict):
            return False, ""
        if str(payload.get("status", "")).strip().lower() != "ok":
            LOGGER.debug("LLM 传输层健康检查返回异常: %s", payload)
            return False, ""
        return True, str(payload.get("version", "") or "").strip()

    # ── 内部：日志泵 ──────────────────────────────────────────

    def _start_log_pumps(self, process: subprocess.Popen[str]) -> None:
        """stdout 只等 ``READY``，stderr 全部转进日志（契约 §1）。"""
        threading.Thread(
            target=self._pump_stdout,
            args=(process,),
            name="llm-sidecar-stdout",
            daemon=True,
        ).start()
        threading.Thread(
            target=self._pump_stderr,
            args=(process,),
            name="llm-sidecar-stderr",
            daemon=True,
        ).start()

    def _pump_stdout(self, process: subprocess.Popen[str]) -> None:
        stream = process.stdout
        if stream is None:
            return
        try:
            for line in stream:
                text = line.strip()
                if not text:
                    continue
                if text.startswith(_READY_PREFIX):
                    raw_port = text.removeprefix(_READY_PREFIX).strip()
                    self._ready_port = int(raw_port) if raw_port.isdigit() else 0
                    self._ready_event.set()
                    continue
                # 契约要求 READY 之外不污染 stdout；真出现了也别丢，降级成日志。
                LOGGER.debug("[llm-transport] %s", text)
        except (ValueError, OSError):
            pass
        finally:
            # 流被关闭时不能让等待方挂满整个启动超时。
            self._ready_event.set()
            _close_quietly(stream)

    def _pump_stderr(self, process: subprocess.Popen[str]) -> None:
        stream = process.stderr
        if stream is None:
            return
        try:
            for line in stream:
                text = line.rstrip()
                if text:
                    LOGGER.info("[llm-transport] %s", text)
        except (ValueError, OSError):
            pass
        finally:
            _close_quietly(stream)

    def _abandon_process(self) -> None:
        """丢弃当前句柄：还活着的先结束掉，避免留下孤儿进程。"""
        process = self._process
        self._process = None
        self._port = 0
        self._version = ""
        if process is not None and process.poll() is None:
            _kill(process)


def _creation_flags() -> int:
    """Windows 下不要给侧车弹一个控制台窗口；其它平台无需特殊 flag。

    刻意不建新会话：侧车与父进程同组时，Ctrl+C 会同时送达，它自己的优雅退出
    （契约 §1）就直接生效了。
    """
    if platform.system() == "Windows":
        return int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
    return 0


def _kill(process: subprocess.Popen[str]) -> None:
    """强制结束侧车，Windows 上**连它的子进程一起**结束。

    只 ``kill()`` 顶层进程会留下一棵树继续握着 stdout/stderr 的写端：读线程拿不到
    EOF，管道也永远关不干净。因此 Windows 上先走 ``taskkill /T``，让整棵树一起走。
    把二进制换成一个 ``.bat``/``.cmd`` 包装脚本是很现实的做法，这条路径必须扛得住。
    """
    if platform.system() == "Windows":
        try:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(process.pid)],
                capture_output=True,
                check=False,
                creationflags=_creation_flags(),
            )
        except OSError as exc:
            LOGGER.debug("taskkill 不可用，退回 process.kill(): %s", exc)
    try:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=STOP_TIMEOUT_SECONDS)
    except (OSError, subprocess.TimeoutExpired) as exc:
        LOGGER.error("强制结束 LLM 传输层侧车失败: %s", exc)


def _close_quietly(stream: IO[str] | None) -> None:
    """关掉一条子进程管道，**只在读线程里调用**。

    Windows 上如果管道另一头还有自己的子进程开着写端，``close()`` 会卡在缓冲区锁上
    等一个永远不会完成的读——从调用方线程关就等于把事件循环卡死。读线程自己关，最坏
    也只让这个守护线程陪着那个进程一起挂着，不拖累主线程。
    """
    if stream is None:
        return
    try:
        stream.close()
    except (OSError, ValueError):
        pass


def _version_major(version: str) -> int | None:
    head = str(version or "").strip().lstrip("v").split(".", 1)[0]
    return int(head) if head.isdigit() else None


# ── 进程级单例 ────────────────────────────────────────────────
#
# 侧车是无状态的传输层：多个 persona 共用一个进程即可。单例也保证 atexit 只注册
# 一次，不会在退出时对同一份配置起停两轮。

_SHARED_LOCK = threading.Lock()
_SHARED: LLMSidecar | None = None
_ATEXIT_REGISTERED = False


def get_sidecar(*, binary: str = "", port: int = 0) -> LLMSidecar | None:
    """返回已启动的进程级侧车；启动失败返回 ``None``（原因已写进日志）。

    配置（二进制/端口）变化时会重建进程：改完配置不该还跑着旧二进制的进程。
    """
    global _SHARED, _ATEXIT_REGISTERED

    with _SHARED_LOCK:
        sidecar = _SHARED
        if sidecar is not None and sidecar.is_alive() and sidecar.matches(binary=binary, port=port):
            return sidecar
        if sidecar is not None:
            sidecar.stop()
            _SHARED = None

        candidate = LLMSidecar(binary=binary, port=port)
        try:
            candidate.start()
        except SidecarError as exc:
            LOGGER.error("LLM 传输层侧车不可用: %s", exc)
            return None

        _SHARED = candidate
        if not _ATEXIT_REGISTERED:
            # 兜底：正常路径由 CLI 的 finally 调 stop_sidecar()，这里只管异常退出。
            atexit.register(stop_sidecar)
            _ATEXIT_REGISTERED = True
        return candidate


def shared_sidecar() -> LLMSidecar | None:
    """当前共享侧车（不启动新进程）；未启用或已退出时返回 ``None``。"""
    with _SHARED_LOCK:
        return _SHARED


def stop_sidecar() -> None:
    """停掉共享侧车（幂等）。CLI 退出与 ``atexit`` 都走这里。"""
    global _SHARED

    with _SHARED_LOCK:
        sidecar = _SHARED
        _SHARED = None
    if sidecar is not None:
        sidecar.stop()
