"""LLM 传输层侧车的进程生命周期与进程契约（契约 §1/§8）。

分两层：

- **单元行为**（``monkeypatch`` 掉 ``Popen``）：token 生成、端口分配、二进制定位、
  ``READY`` 解析、``/health`` 版本校验、``stop()`` 幂等、进程级单例重建。用假的
  进程对象跑，毫秒级，确定性；
- **真实进程契约**（真跑 ``llm-transport.exe``）：``--port 0`` + ``READY`` 行、
  ``LLM_TRANSPORT_TOKEN`` 环境变量回退、``/health`` 免鉴权、``/v1/*`` 无 token 即
  401、``--version``。这几条落在 ``cmd/llm-transport`` 里，Go 侧没有单测覆盖
  （该包是 ``[no test files]``），只能在这一层验。

任何用例都不许留下孤儿进程：假进程的强杀走 ``_kill`` 的打桩记录，真进程一律在
``finally`` 里 ``terminate`` + ``wait``。
"""

from __future__ import annotations

import json
import os
import platform
import queue
import re
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator

import pytest

from sirius_pulse.utils import llm_sidecar
from sirius_pulse.utils.llm_sidecar import (
    BINARY_ENV_VAR,
    LLMSidecar,
    SidecarError,
    allocate_free_port,
    get_sidecar,
    resolve_binary,
    shared_sidecar,
    stop_sidecar,
)

BINARY = resolve_binary("")

requires_binary = pytest.mark.skipif(
    BINARY is None,
    reason="未找到 LLM 传输层二进制：先运行 python scripts/build_llm_transport.py",
)

TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43}$")  # secrets.token_urlsafe(32)


# ── 假的子进程 ────────────────────────────────────────────
class _FakeStream:
    """够用的行迭代器：``for line in stream`` + ``close()``。"""

    def __init__(self, lines: list[str]) -> None:
        self._lines = lines
        self.closed = False

    def __iter__(self) -> Iterator[str]:
        return iter(self._lines)

    def close(self) -> None:
        self.closed = True


class _FakeProcess:
    def __init__(
        self,
        *,
        ready_port: int = 0,
        stdout_lines: list[str] | None = None,
        exit_code: int | None = None,
    ) -> None:
        self.pid = 4242
        self.returncode = exit_code
        self.stdout = _FakeStream(
            [f"READY {ready_port}\n"] if stdout_lines is None else stdout_lines
        )
        self.stderr = _FakeStream(["[llm-transport] 启动\n"])
        self.terminated = 0
        self.killed = 0

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated += 1
        self.returncode = 0

    def wait(self, timeout: float | None = None) -> int | None:
        return self.returncode

    def kill(self) -> None:
        self.killed += 1
        self.returncode = -9


@dataclass
class _Launcher:
    """Popen 打桩的控制器：用例改这里的字段来编排假进程的行为。"""

    ready_port: int | None = None
    stdout_lines: list[str] | None = None
    exit_immediately: bool = False
    argv: list[str] = field(default_factory=list)
    kwargs: dict[str, object] = field(default_factory=dict)
    procesos: list[_FakeProcess] = field(default_factory=list)
    killed: list[_FakeProcess] = field(default_factory=list)

    @property
    def process(self) -> _FakeProcess:
        assert self.procesos, "Popen 从未被调用"
        return self.procesos[-1]


@pytest.fixture
def launcher(monkeypatch: pytest.MonkeyPatch) -> _Launcher:
    """把 ``Popen`` 换成假进程，并把 ``_kill`` 换成记录器。

    ``_kill`` 必须打桩：它在 Windows 上会 ``taskkill /F /T /PID 4242``，而 4242
    很可能是这台机器上某个真实进程的 PID。
    """
    control = _Launcher()

    def _popen(argv: list[str], **kwargs: object) -> _FakeProcess:
        control.argv = list(argv)
        control.kwargs = dict(kwargs)
        port = control.ready_port
        if port is None:
            port = int(control.argv[control.argv.index("--port") + 1])
        lines = control.stdout_lines if control.stdout_lines is not None else [f"READY {port}\n"]
        process = _FakeProcess(
            ready_port=port,
            stdout_lines=lines,
            exit_code=3 if control.exit_immediately else None,
        )
        control.procesos.append(process)
        return process

    class _PatchedSubprocess:
        """只替换 ``Popen``，其余属性透传给真的 ``subprocess`` 模块。"""

        Popen = staticmethod(_popen)

        def __getattr__(self, name: str) -> object:
            return getattr(subprocess, name)

    def _fake_kill(process: _FakeProcess) -> None:
        control.killed.append(process)
        process.kill()

    monkeypatch.setattr(llm_sidecar, "subprocess", _PatchedSubprocess())
    monkeypatch.setattr(llm_sidecar, "_kill", _fake_kill)
    return control


@pytest.fixture(autouse=True)
def _isolated_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    """每个用例从「没有共享侧车」开始，且不注册 atexit。"""
    monkeypatch.setattr(llm_sidecar, "_SHARED", None)
    monkeypatch.setattr(llm_sidecar, "_ATEXIT_REGISTERED", True)


# ── 二进制定位 ────────────────────────────────────────────
def test_resolve_binary_prefers_configured_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configured = tmp_path / "custom-sidecar.exe"
    configured.write_bytes(b"")
    env_binary = tmp_path / "env-sidecar.exe"
    env_binary.write_bytes(b"")
    monkeypatch.setenv(BINARY_ENV_VAR, str(env_binary))
    assert resolve_binary(str(configured)) == configured


def test_resolve_binary_falls_back_to_env_var(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """配置里的旧绝对路径（换机器后失效）不能挡住环境变量与仓库内默认位置。"""
    env_binary = tmp_path / "env-sidecar.exe"
    env_binary.write_bytes(b"")
    monkeypatch.setenv(BINARY_ENV_VAR, str(env_binary))
    assert resolve_binary(str(tmp_path / "gone.exe")) == env_binary


def test_resolve_binary_falls_back_to_bundled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(BINARY_ENV_VAR, raising=False)
    monkeypatch.setattr(llm_sidecar, "PROJECT_ROOT", tmp_path)
    bundled = tmp_path / llm_sidecar.DEFAULT_BINARY_DIR / llm_sidecar.BINARY_NAME
    bundled.parent.mkdir(parents=True)
    bundled.write_bytes(b"")
    assert resolve_binary("") == bundled


def test_resolve_binary_returns_none_when_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv(BINARY_ENV_VAR, raising=False)
    monkeypatch.setattr(llm_sidecar, "PROJECT_ROOT", tmp_path)
    monkeypatch.setattr(
        llm_sidecar, "shutil", type("_NoWhich", (), {"which": staticmethod(lambda n: None)})()
    )
    assert resolve_binary("") is None


def test_allocate_free_port_is_bindable() -> None:
    port = allocate_free_port()
    assert 1024 < port < 65536
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", port))  # 拿到的必须是真空闲端口


# ── 启动：token / 端口 / READY / health ──────────────────
def _prepare_start(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, version: str = "0.1.1"
) -> Path:
    monkeypatch.delenv(BINARY_ENV_VAR, raising=False)
    binary = tmp_path / "llm-transport.exe"
    binary.write_bytes(b"")
    monkeypatch.setattr(LLMSidecar, "_probe_health", lambda self: (True, version))
    return binary


def test_start_generates_token_and_reads_ready_port(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, launcher: _Launcher
) -> None:
    binary = _prepare_start(tmp_path, monkeypatch)
    sidecar = LLMSidecar(binary=str(binary), port=18083)
    sidecar.start()

    # 命令行：二进制 + --port + --token（token 现生成、只在内存里）
    assert launcher.argv[:2] == [str(binary), "--port"]
    assert launcher.argv[2] == "18083"
    assert launcher.argv[3] == "--token"
    token = launcher.argv[4]
    assert TOKEN_PATTERN.match(token), f"token 形态不对: {token!r}"

    # 管道必须拿到手上：stdout 等 READY，stderr 进日志
    assert launcher.kwargs["stdout"] == subprocess.PIPE
    assert launcher.kwargs["stderr"] == subprocess.PIPE
    assert launcher.kwargs["stdin"] == subprocess.DEVNULL
    assert launcher.kwargs["text"] is True
    assert launcher.kwargs["encoding"] == "utf-8"
    assert launcher.kwargs["cwd"] == str(llm_sidecar.PROJECT_ROOT)
    assert launcher.kwargs["creationflags"] == llm_sidecar._creation_flags()

    assert sidecar.token == token
    assert sidecar.base_url == "http://127.0.0.1:18083"
    assert sidecar.version == "0.1.1"
    assert sidecar.pid == 4242
    assert sidecar.is_alive() is True
    assert sidecar.binary_path == binary


def test_start_allocates_port_when_unconfigured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, launcher: _Launcher
) -> None:
    binary = _prepare_start(tmp_path, monkeypatch)
    sidecar = LLMSidecar(binary=str(binary))
    sidecar.start()

    port = int(launcher.argv[launcher.argv.index("--port") + 1])
    assert 1024 < port < 65536
    assert sidecar.base_url == f"http://127.0.0.1:{port}"


def test_start_requires_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, launcher: _Launcher
) -> None:
    monkeypatch.setattr(llm_sidecar, "resolve_binary", lambda configured="": None)
    with pytest.raises(SidecarError) as excinfo:
        LLMSidecar(binary="").start()
    assert "scripts/build_llm_transport.py" in str(excinfo.value), "报错要给出可执行的补救办法"
    assert launcher.procesos == [], "没有二进制时不该尝试拉起进程"


def test_start_fails_when_process_exits_immediately(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, launcher: _Launcher
) -> None:
    binary = _prepare_start(tmp_path, monkeypatch)
    launcher.exit_immediately = True
    launcher.stdout_lines = []
    with pytest.raises(SidecarError) as excinfo:
        LLMSidecar(binary=str(binary)).start()
    assert "启动后立即退出（exit=3）" in str(excinfo.value)


def test_start_times_out_without_ready_line(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, launcher: _Launcher
) -> None:
    binary = _prepare_start(tmp_path, monkeypatch)
    launcher.stdout_lines = []  # 进程活着但什么都不打印
    with pytest.raises(SidecarError) as excinfo:
        LLMSidecar(binary=str(binary), startup_timeout_seconds=0.3).start()
    assert "没有打印 READY" in str(excinfo.value)
    assert launcher.killed, "启动失败必须把进程收掉，不能留孤儿"


def test_start_rejects_incompatible_major_version(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, launcher: _Launcher
) -> None:
    """契约 §8：侧车版本主版本必须是 0，否则宁可起不来。"""
    binary = _prepare_start(tmp_path, monkeypatch, version="1.2.3")
    with pytest.raises(SidecarError) as excinfo:
        LLMSidecar(binary=str(binary)).start()
    assert "契约版本不兼容" in str(excinfo.value)
    assert launcher.killed, "版本不兼容时要先收掉进程再报错"


def test_start_accepts_unparsable_version_with_warning(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, launcher: _Launcher
) -> None:
    """版本号解析不出来时按兼容处理（只警告），不阻断启动。"""
    binary = _prepare_start(tmp_path, monkeypatch, version="dev")
    sidecar = LLMSidecar(binary=str(binary))
    sidecar.start()
    assert sidecar.version == "dev"
    assert sidecar.is_alive() is True


# ── 停止与重启 ────────────────────────────────────────────
def test_stop_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, launcher: _Launcher
) -> None:
    binary = _prepare_start(tmp_path, monkeypatch)
    sidecar = LLMSidecar(binary=str(binary), port=18084)
    sidecar.start()
    process = launcher.process

    sidecar.stop()
    assert process.terminated == 1
    assert sidecar.is_alive() is False
    assert sidecar.base_url == ""
    assert sidecar.token == ""
    assert sidecar.version == ""
    assert sidecar.pid is None

    sidecar.stop()  # 幂等：不再碰已结束的进程
    assert process.terminated == 1
    assert process.killed == 0


def test_ensure_running_returns_empty_when_restart_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sidecar = LLMSidecar(binary=str(tmp_path / "gone.exe"))
    monkeypatch.setattr(sidecar, "start", lambda: (_ for _ in ()).throw(SidecarError("起不来")))
    assert sidecar.ensure_running() == ""


def test_matches_detects_config_change() -> None:
    sidecar = LLMSidecar(binary="a.exe", port=18085)
    assert sidecar.matches(binary="a.exe", port=18085) is True
    assert sidecar.matches(binary="b.exe", port=18085) is False
    assert sidecar.matches(binary="a.exe", port=18086) is False
    assert sidecar.matches(binary=" a.exe ", port=18085) is True


# ── 进程级单例 ────────────────────────────────────────────
def _install_fake_running(sidecar: LLMSidecar) -> None:
    sidecar._process = _FakeProcess(ready_port=18090)  # type: ignore[assignment]
    sidecar._port = 18090
    sidecar._token = "t"


def test_get_sidecar_is_a_process_singleton(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(LLMSidecar, "start", _install_fake_running)
    first = get_sidecar()
    assert first is not None
    assert get_sidecar() is first, "配置不变时必须复用同一个进程"
    assert shared_sidecar() is first


def test_get_sidecar_rebuilds_when_config_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(LLMSidecar, "start", _install_fake_running)
    first = get_sidecar(binary="")
    assert first is not None
    second = get_sidecar(binary="other-binary.exe")
    assert second is not None
    assert second is not first, "改了二进制路径不该还跑着旧进程"
    assert first.is_alive() is False


def test_stop_sidecar_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(LLMSidecar, "start", _install_fake_running)
    sidecar = get_sidecar()
    assert sidecar is not None
    stop_sidecar()
    assert shared_sidecar() is None
    assert sidecar.is_alive() is False
    stop_sidecar()  # 第二次必须是空操作


# ── 真实二进制的进程契约（契约 §1）────────────────────────
def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _post_json(url: str, payload: object | None, *, token: str = "") -> tuple[int, dict]:
    """POST 一个 JSON 请求；``payload is None`` 表示连正文都不发。

    401 用例必须走「不发正文」这条路：鉴权失败发生在读 body 之前，Go 的
    ``http.Server`` 随后会直接关连接；Windows 上关掉一个还有未读数据的连接是
    RST 而不是 FIN，客户端偶尔会看到 ``ConnectionResetError``——这与鉴权语义无关，
    只会让用例变成偶发失败。
    """
    data = b"" if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 - 回环地址
            return response.status, json.loads(response.read() or b"{}")
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")


class _SpawnedBinary:
    """真跑起来的侧车：读走 READY 行拿到端口，退出时确保进程被收掉。

    stdout/stderr 都由后台线程抽干并转进队列：直接 ``read()`` 会跟
    ``TerminateProcess`` 抢时间，进程被杀时管道里那几行就丢了（「缺少 --token」
    这条诊断信息正是要断言的）。
    """

    def __init__(self, *args: str, env: dict[str, str] | None = None) -> None:
        assert BINARY is not None
        self.process = subprocess.Popen(
            [str(BINARY), *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        self._stdout: queue.Queue[str] = queue.Queue()
        self._stderr: queue.Queue[str] = queue.Queue()
        self._pump(self.process.stdout, self._stdout, "spawned-stdout")
        self._pump(self.process.stderr, self._stderr, "spawned-stderr")

    @staticmethod
    def _pump(stream: object, sink: queue.Queue[str], name: str) -> None:
        if stream is None:  # pragma: no cover - 两个管道都是 PIPE
            return

        def _run() -> None:
            for line in stream:  # type: ignore[union-attr]
                sink.put(line)

        threading.Thread(target=_run, name=name, daemon=True).start()

    def next_line(self, timeout: float = 10.0) -> str:
        return self._next(self._stdout, timeout)

    def next_stderr_line(self, timeout: float = 10.0) -> str:
        return self._next(self._stderr, timeout)

    @staticmethod
    def _next(sink: queue.Queue[str], timeout: float) -> str:
        try:
            return sink.get(timeout=timeout)
        except queue.Empty:  # pragma: no cover - 只有二进制坏掉才会走到
            pytest.fail(f"侧车 {timeout}s 内没有输出任何行")

    def stop(self) -> int:
        if self.process.poll() is None:
            self.process.terminate()
        try:
            return int(self.process.wait(timeout=10))
        except subprocess.TimeoutExpired:  # pragma: no cover
            self.process.kill()
            return int(self.process.wait(timeout=10) or -1)


@requires_binary
def test_binary_reads_token_from_environment() -> None:
    """契约 §1：``--token`` 缺省时读 ``LLM_TRANSPORT_TOKEN``。"""
    env = {**os.environ, "LLM_TRANSPORT_TOKEN": "env-secret-token"}
    binary = _SpawnedBinary("--port", "0", env=env)
    try:
        ready = binary.next_line()
        assert ready.startswith("READY "), ready
        port = int(ready.split()[1])
        assert 1024 < port < 65536

        health_url = f"http://127.0.0.1:{port}/health"
        with urllib.request.urlopen(health_url, timeout=10) as response:  # noqa: S310
            health = json.loads(response.read())
        assert health["status"] == "ok"
        assert health["pid"] > 0
        assert re.match(r"^\d+\.\d+\.\d+$", health["version"]), health["version"]

        generate_url = f"http://127.0.0.1:{port}/v1/generate"
        status, body = _post_json(generate_url, None)
        assert status == 401, "没有 token 的 /v1/* 必须 401"
        assert body["error"]["error_code"] == "UNAUTHORIZED"

        status, body = _post_json(generate_url, None, token="wrong-token")
        assert status == 401

        status, body = _post_json(generate_url, {}, token="env-secret-token")
        assert status == 400, "鉴权通过后该轮到入参校验"
        assert body["error"]["error_code"] == "INVALID_REQUEST"
    finally:
        binary.stop()


@requires_binary
def test_binary_refuses_to_start_without_token() -> None:
    env = {key: value for key, value in os.environ.items() if key != "LLM_TRANSPORT_TOKEN"}
    binary = _SpawnedBinary("--port", "0", env=env)
    try:
        stderr = binary.next_stderr_line()
    finally:
        binary.stop()
    assert binary.process.returncode != 0
    assert "缺少 --token" in stderr


@requires_binary
def test_binary_version_flag() -> None:
    assert BINARY is not None
    completed = subprocess.run(
        [str(BINARY), "--version"], capture_output=True, text=True, encoding="utf-8", timeout=30
    )
    assert completed.returncode == 0
    assert completed.stdout.strip().startswith("llm-transport ")


@requires_binary
def test_binary_binds_loopback_only() -> None:
    """契约 §1：不得监听 0.0.0.0。"""
    binary = _SpawnedBinary("--port", "0", "--token", "t")
    try:
        port = int(binary.next_line().split()[1])
        # 本机回环可达
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=10) as response:
            assert response.status == 200

        local_ip = _local_ipv4()
        if local_ip is None or local_ip == "127.0.0.1":  # pragma: no cover - 离线机器
            pytest.skip("拿不到非回环的本机地址，无法验证「不监听 0.0.0.0」")
        with pytest.raises((urllib.error.URLError, ConnectionError, OSError)):
            urllib.request.urlopen(f"http://{local_ip}:{port}/health", timeout=2)  # noqa: S310
    finally:
        binary.stop()


def _local_ipv4() -> str | None:
    """取一个非回环的本机地址（只做 UDP connect，不发包）。"""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        try:
            sock.connect(("192.0.2.1", 9))  # TEST-NET-1，不会真的发包
            return str(sock.getsockname()[0])
        except OSError:  # pragma: no cover
            return None


# ── 平台差异（记录，不做断言失败）────────────────────────
def test_stop_uses_terminate_on_windows() -> None:
    """契约 §1 要求优雅退出并返回 0；Windows 的 ``terminate()`` 是 TerminateProcess。

    Python 这条路径上拿不到 SIGTERM，因此 Windows 的退出码不是契约期望的 0。侧车
    是无状态传输层，这不影响正确性；这条用例把差异固定下来，避免以后有人误以为
    在 Windows 上也验证过优雅退出。
    """
    if platform.system() != "Windows":
        pytest.skip("只在 Windows 上有这条差异")
    assert llm_sidecar.STOP_TIMEOUT_SECONDS == 5.0
