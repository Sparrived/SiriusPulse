"""Sirius Pulse 独立单人格 CLI。

启动与管理单个人格实例，引擎在主进程内直接运行。

使用方法::

    python main.py                               # 进入交互式 CLI
    python main.py run                           # 启动人格引擎 + WebUI
    python main.py init                          # 在 data/ 目录初始化人格配置
    python main.py webui                         # 仅启动 WebUI（管理模式）
    python main.py assistant --butler ws://...    # 以助手模式连接管家端
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import io
import json
import logging
import os
import signal
import socket
import subprocess
import sys
import unicodedata
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from sirius_pulse.logging_config import configure_logging, setup_log_archival

REPO_ROOT = Path(os.environ.get("SIRIUS_PULSE_HOME", Path.cwd())).expanduser().resolve()
DATA_DIR = REPO_ROOT / "data"
GLOBAL_CONFIG_PATH = DATA_DIR / "global_config.json"
WEBUI_STATUS_PATH = DATA_DIR / "webui_status.json"
_TUI_SCREEN_ACTIVE = False


class _Ansi:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    CYAN = "\033[36m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    RED = "\033[31m"
    MAGENTA = "\033[35m"
    BLUE = "\033[34m"


def _use_color() -> bool:
    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _paint(text: str, *styles: str) -> str:
    if not _use_color():
        return text
    return "".join(styles) + text + _Ansi.RESET


def _clear_screen() -> None:
    if sys.stdout.isatty():
        print("\033[2J\033[H", end="")


def _display_width(text: str) -> int:
    return sum(2 if unicodedata.east_asian_width(char) in {"F", "W"} else 1 for char in text)


def _pad(text: str, width: int) -> str:
    return text + " " * max(0, width - _display_width(text))


def _center(text: str, width: int) -> str:
    padding = max(0, width - _display_width(text))
    left = padding // 2
    return " " * left + text + " " * (padding - left)


def _pause(message: str = "按 Enter 返回...") -> None:
    try:
        input(_paint(message, _Ansi.DIM))
    except KeyboardInterrupt:
        raise
    except EOFError:
        print()


def _prompt(message: str) -> str:
    try:
        return input(_paint(message, _Ansi.CYAN, _Ansi.BOLD)).strip()
    except KeyboardInterrupt:
        raise
    except EOFError:
        return "q"


def _header(title: str, subtitle: str | None = None) -> None:
    _clear_screen()
    print(_header_text(title, subtitle), end="")
    print()


def _header_text(title: str, subtitle: str | None = None) -> str:
    width = 74
    lines = [
        _paint("╭" + "─" * width + "╮", _Ansi.BLUE),
        _paint("│", _Ansi.BLUE) + _pad(" Sirius Pulse ", width) + _paint("│", _Ansi.BLUE),
        _paint("│", _Ansi.BLUE) + _center(title, width) + _paint("│", _Ansi.BLUE),
    ]
    if subtitle:
        lines.append(_paint("│", _Ansi.BLUE) + _center(subtitle, width) + _paint("│", _Ansi.BLUE))
    lines.append(_paint("╰" + "─" * width + "╯", _Ansi.BLUE))
    return "\n".join(lines) + "\n"


def _capture_render(render: Callable[[], None] | None) -> str:
    if render is None:
        return ""
    buffer = io.StringIO()
    with contextlib.redirect_stdout(buffer):
        render()
    return buffer.getvalue()


def _write_frame(frame: str) -> None:
    if sys.stdout.isatty():
        sys.stdout.write("\033[?25l\033[H\033[J" + frame)
    else:
        sys.stdout.write(frame)
    sys.stdout.flush()


def _enter_tui_screen() -> None:
    global _TUI_SCREEN_ACTIVE
    if not sys.stdout.isatty() or _TUI_SCREEN_ACTIVE:
        return
    sys.stdout.write("\033[?1049h\033[?25l\033[2J\033[H")
    sys.stdout.flush()
    _TUI_SCREEN_ACTIVE = True


def _exit_tui_screen() -> None:
    global _TUI_SCREEN_ACTIVE
    if not _TUI_SCREEN_ACTIVE:
        return
    sys.stdout.write("\033[?25h\033[?1049l")
    sys.stdout.flush()
    _TUI_SCREEN_ACTIVE = False


def _menu_item(key: str, label: str, detail: str) -> None:
    print(
        f"  {_paint(_pad(key, 12), _Ansi.BOLD, _Ansi.CYAN)}  "
        f"{_paint(_pad(label, 20), _Ansi.BOLD)} {_paint(detail, _Ansi.DIM)}"
    )


def _read_menu_key() -> str:
    if not sys.stdin.isatty():
        return _prompt("\n选择操作: ").lower()
    if sys.platform == "win32":
        import msvcrt

        ch = msvcrt.getwch()
        if ch == "\x03":
            raise KeyboardInterrupt
        if ch in {"\r", "\n"}:
            return "enter"
        if ch == "\x1b":
            return "esc"
        if ch in {"\x00", "\xe0"}:
            nxt = msvcrt.getwch()
            if nxt == "H":
                return "up"
            if nxt == "P":
                return "down"
        return ch.lower()
    import termios
    import tty

    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
        if ch == "\x03":
            raise KeyboardInterrupt
        if ch in {"\r", "\n"}:
            return "enter"
        if ch == "\x1b":
            seq = sys.stdin.read(2)
            if seq == "[A":
                return "up"
            if seq == "[B":
                return "down"
            return "esc"
        return ch.lower()
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _select_menu(
    title: str,
    subtitle: str,
    items: list[tuple[str, str, str]],
    render_extra: Callable[[], None] | None = None,
) -> str:
    selected = 0
    try:
        while True:
            parts = [_header_text(title, subtitle)]
            extra = _capture_render(render_extra)
            if extra:
                parts.append(extra.rstrip("\n") + "\n\n")
            parts.append(
                _paint("使用 ↑/↓ 选择，Enter 确认，Esc 返回，Ctrl+C 退出", _Ansi.DIM) + "\n\n"
            )
            for idx, (key, label, detail) in enumerate(items):
                active = idx == selected
                marker = ">" if active else " "
                row_key = _paint(_pad(key, 8), _Ansi.BOLD, _Ansi.CYAN)
                row_label = _paint(_pad(label, 18), _Ansi.BOLD)
                row_detail = _paint(detail, _Ansi.DIM)
                line = f"  {marker} {row_key} {row_label} {row_detail}"
                parts.append((_paint(line, _Ansi.BLUE, _Ansi.BOLD) if active else line) + "\n")
            frame = "".join(parts)
            _write_frame(frame)
            key = _read_menu_key()
            if key == "up":
                selected = (selected - 1) % len(items)
            elif key == "down":
                selected = (selected + 1) % len(items)
            elif key == "enter":
                return items[selected][0]
            elif key == "esc":
                return "b"
            else:
                for idx, (item_key, _label, _detail) in enumerate(items):
                    if key == item_key.lower():
                        selected = idx
                        return item_key
    finally:
        if sys.stdout.isatty():
            sys.stdout.write("\033[?25h")
            sys.stdout.flush()


def _run_cli_action(action: Callable[[], None]) -> None:
    try:
        action()
    except KeyboardInterrupt:
        _shutdown_services()
        raise SystemExit(0)


def _read_webui_status() -> dict[str, Any] | None:
    if not WEBUI_STATUS_PATH.exists():
        return None
    try:
        return json.loads(WEBUI_STATUS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_webui_status(pid: int, config: dict[str, Any]) -> None:
    WEBUI_STATUS_PATH.parent.mkdir(parents=True, exist_ok=True)
    status = {
        "pid": pid,
        "host": str(config.get("webui_host", "0.0.0.0")),
        "port": int(config.get("webui_port", 8080)),
        "started_at": datetime.now().isoformat(timespec="seconds"),
    }
    WEBUI_STATUS_PATH.write_text(json.dumps(status, indent=2, ensure_ascii=False), encoding="utf-8")


def _pid_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        result = subprocess.run(
            ["tasklist", "/FI", f"PID eq {pid}", "/NH"],
            capture_output=True,
            text=True,
            check=False,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        return str(pid) in result.stdout
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    probe_host = "127.0.0.1" if host in {"0.0.0.0", "::", ""} else host
    try:
        with socket.create_connection((probe_host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _webui_url(config: dict[str, Any] | None = None) -> str:
    cfg = config or _load_global_config()
    return f"http://localhost:{int(cfg.get('webui_port', 8080))}"


def _webui_status() -> tuple[bool, dict[str, Any] | None]:
    status = _read_webui_status()
    if not status:
        return False, None
    pid = int(status.get("pid") or 0)
    host = str(status.get("host") or "127.0.0.1")
    port = int(status.get("port") or 8080)
    if not _pid_exists(pid):
        WEBUI_STATUS_PATH.unlink(missing_ok=True)
        return False, None
    status["ready"] = _port_open(host, port)
    return True, status


def _start_webui_background() -> dict[str, Any]:
    running, status = _webui_status()
    config = _load_global_config()
    if running and status:
        return {
            "started": False,
            "running": True,
            "pid": status.get("pid"),
            "url": _webui_url(config),
        }

    cmd = [sys.executable, "-m", "sirius_pulse.cli", "webui", "--foreground"]
    kwargs: dict[str, Any] = {
        "cwd": str(REPO_ROOT),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = (
            subprocess.DETACHED_PROCESS
            | subprocess.CREATE_NEW_PROCESS_GROUP
            | subprocess.CREATE_NO_WINDOW
        )
    else:
        kwargs["start_new_session"] = True
    process = subprocess.Popen(cmd, **kwargs)
    _write_webui_status(process.pid, config)
    return {"started": True, "running": True, "pid": process.pid, "url": _webui_url(config)}


def _stop_webui_background() -> bool:
    status = _read_webui_status()
    if not status:
        return False
    pid = int(status.get("pid") or 0)
    if pid and _pid_exists(pid):
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                check=False,
                capture_output=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        else:
            os.kill(pid, signal.SIGTERM)
    WEBUI_STATUS_PATH.unlink(missing_ok=True)
    return True


def _shutdown_services() -> None:
    print(_paint("\n正在执行退出清理...", _Ansi.YELLOW))
    try:
        if _stop_webui_background():
            print(_paint("已停止 WebUI 与其托管的 Embedding 微服务", _Ansi.GREEN))
    except Exception as exc:
        print(_paint(f"停止 WebUI 失败: {exc}", _Ansi.RED))


def _default_global_config() -> dict:
    """返回默认全局配置。"""
    return {
        "webui_host": "0.0.0.0",
        "webui_port": 8080,
        "napcat_install_dir": str(REPO_ROOT / "napcat"),
        "log_level": "INFO",
    }


def _load_global_config() -> dict:
    """加载全局配置，若不存在则创建默认。"""
    if GLOBAL_CONFIG_PATH.exists():
        try:
            return json.loads(GLOBAL_CONFIG_PATH.read_text(encoding="utf-8"))
        except Exception as exc:
            logging.getLogger("sirius.main").warning("全局配置读取失败: %s，使用默认", exc)
    config = _default_global_config()
    GLOBAL_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    GLOBAL_CONFIG_PATH.write_text(
        json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return config


# ---------------------------------------------------------------------------
# 子命令实现
# ---------------------------------------------------------------------------


async def _cmd_run(args: argparse.Namespace) -> None:
    """启动人格引擎 + WebUI。引擎在主进程内直接运行。"""
    config = _load_global_config()
    webui_log_file = DATA_DIR / "logs" / "webui.log"
    configure_logging(
        level=config.get("log_level", "INFO"),
        format_type="console",
        log_file=webui_log_file,
    )
    LOG = logging.getLogger("sirius.main")

    from sirius_pulse.webui import WebUIServer

    # ── 先启动 WebUI（含 Embedding 服务）──
    webui = WebUIServer(
        data_dir=DATA_DIR,
        host=str(config.get("webui_host", "0.0.0.0")),
        port=int(config.get("webui_port", 8080)),
    )
    await webui.start()
    LOG.info("WebUI: http://localhost:%s", webui.port)

    # 等待 Embedding 服务就绪（模型加载完成 + /embed 可用）
    import time

    from sirius_pulse.embedding.client import EmbeddingClient

    emb_url = config.get("embedding_url", "http://127.0.0.1:18900")
    emb_client = EmbeddingClient(base_url=emb_url)
    LOG.info("等待 Embedding 服务就绪: %s ...", emb_url)
    for _attempt in range(120):
        if emb_client.check_health():
            # 额外验证 /embed 接口真正可用（模型已加载）
            try:
                _ = emb_client.encode(["ping"])
                LOG.info("Embedding 服务已就绪: %s", emb_url)
                break
            except Exception:
                pass
        time.sleep(0.5)
    else:
        LOG.error("Embedding 服务在 60 秒内未就绪，无法启动人格")
        await webui.stop()
        raise RuntimeError(
            f"Embedding 服务不可用 ({emb_url})。"
            "请检查日志或手动启动: python -m sirius_pulse.embedding.server"
        )

    # ── 直接在主进程启动 PersonaWorker ──
    from sirius_pulse.persona_worker import PersonaWorker

    worker = PersonaWorker(DATA_DIR)

    # 可选：启动 ButlerServer
    butler_port = getattr(args, "butler_port", 0)
    butler_server = None
    if butler_port > 0:
        from sirius_pulse.network.butler_server import ButlerServer

        butler_server = ButlerServer(
            host="0.0.0.0",
            port=butler_port,
            data_dir=DATA_DIR,
            token=getattr(args, "butler_token", None),
        )
        await butler_server.start()
        LOG.info("ButlerServer 已启动: ws://0.0.0.0:%d", butler_port)

    # 信号处理
    if sys.platform == "win32":
        import signal as _signal

        def _sig_handler(_s, _f):
            worker.shutdown()

        _signal.signal(_signal.SIGINT, _sig_handler)
        _signal.signal(_signal.SIGTERM, _sig_handler)
    else:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, worker.shutdown)

    LOG.info("按 Ctrl+C 停止所有服务")

    try:
        await worker.run()
    finally:
        if butler_server:
            await butler_server.stop()
        await webui.stop()
        LOG.info("所有服务已停止")


async def _cmd_webui(args: argparse.Namespace) -> None:
    """仅启动 WebUI（不启动人格引擎）。"""
    if not getattr(args, "foreground", False):
        result = _start_webui_background()
        state = "已在后台运行" if not result["started"] else "已后台启动"
        print(f"WebUI {state}: {result['url']} (pid={result['pid']})")
        return

    config = _load_global_config()
    configure_logging(level=config.get("log_level", "INFO"), format_type="console")
    LOG = logging.getLogger("sirius.main")

    from sirius_pulse.webui import WebUIServer

    webui = WebUIServer(
        data_dir=DATA_DIR,
        host=str(config.get("webui_host", "0.0.0.0")),
        port=int(config.get("webui_port", 8080)),
    )
    await webui.start()
    _write_webui_status(os.getpid(), config)
    LOG.info("WebUI: http://localhost:%s（仅管理模式，无引擎运行）", webui.port)

    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass
    finally:
        await webui.stop()
        WEBUI_STATUS_PATH.unlink(missing_ok=True)


def _cmd_webui_status(args: argparse.Namespace) -> None:
    running, status = _webui_status()
    if running and status:
        state = "运行中" if status.get("ready") else "启动中"
        print(f"WebUI {state}: http://localhost:{status.get('port', 8080)}")
        print(f"PID: {status.get('pid')}")
        print(f"启动时间: {status.get('started_at') or '—'}")
        return
    print("WebUI 未在后台运行")


def _cmd_webui_stop(args: argparse.Namespace) -> None:
    if _stop_webui_background():
        print("WebUI 已停止")
    else:
        print("WebUI 未在后台运行")


def _cmd_init(args: argparse.Namespace) -> None:
    """在 DATA_DIR 目录初始化人格配置（如果不存在则创建默认配置）。"""
    if not DATA_DIR.exists():
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        print(_paint(f"已创建数据目录: {DATA_DIR}", _Ansi.GREEN))
    else:
        print(_paint(f"数据目录已存在: {DATA_DIR}", _Ansi.DIM))

    created: list[str] = []

    # 创建子目录
    for subdir in ("engine_state", "logs", "plugins", "image_cache"):
        d = DATA_DIR / subdir
        if not d.exists():
            d.mkdir(parents=True, exist_ok=True)
            created.append(subdir)

    # 创建默认 persona.json
    persona_path = DATA_DIR / "persona.json"
    if not persona_path.exists():
        persona_path.write_text(
            json.dumps(
                _default_persona_config(),
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        created.append("persona.json")

    # 创建默认 adapters.json
    adapters_path = DATA_DIR / "adapters.json"
    if not adapters_path.exists():
        adapters_path.write_text(
            json.dumps(
                _default_adapters_config(),
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        created.append("adapters.json")

    # 创建默认 experience.json
    experience_path = DATA_DIR / "experience.json"
    if not experience_path.exists():
        experience_path.write_text(
            json.dumps(
                _default_experience_config(),
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        created.append("experience.json")

    # 创建默认全局配置
    _load_global_config()

    if created:
        print(_paint(f"已创建: {', '.join(created)}", _Ansi.GREEN))
        print()
        print("接下来请编辑以下文件配置你的人格:")
        print(f"  {persona_path}")
        print(f"  {adapters_path}")
        print(f"  {experience_path}")
        print()
        print("然后运行:")
        print("  python main.py run")
    else:
        print(_paint("所有配置文件已存在，无需初始化。", _Ansi.DIM))


def _default_persona_config() -> dict:
    """返回默认 persona.json 内容。"""
    return {
        "persona_name": "默认人格",
        "personality": "",
        "speaking_style": "",
        "ai": {
            "model": "auto",
            "prompt": "",
        },
    }


def _default_adapters_config() -> dict:
    """返回默认 adapters.json 内容。"""
    return {
        "adapters": [
            {
                "type": "napcat",
                "enabled": False,
                "ws_url": "ws://localhost:3001",
                "token": "",
                "groups": [],
            }
        ]
    }


def _default_experience_config() -> dict:
    """返回默认 experience.json 内容。"""
    return {
        "memory_depth": "standard",
        "plugins": {},
    }


def _load_persona_name() -> str:
    """从 persona.json 加载人格名称。"""
    persona_path = DATA_DIR / "persona.json"
    if persona_path.exists():
        try:
            data = json.loads(persona_path.read_text(encoding="utf-8"))
            return data.get("persona_name", "default")
        except Exception:
            pass
    return "default"


def _cli_start_all() -> None:
    _header("启动运行模式", "WebUI 与人格引擎会一起启动")
    print(_paint("即将进入长期运行模式，按 Ctrl+C 可停止所有服务。", _Ansi.YELLOW))
    choice = _prompt("继续启动？[Y/n] ").lower()
    if choice in {"n", "no", "否"}:
        return
    asyncio.run(_cmd_run(argparse.Namespace(butler_port=0, butler_token=None)))


def _cli_open_webui() -> None:
    config = _load_global_config()
    running, status = _webui_status()

    def render_status() -> None:
        print(f"地址: {_paint(_webui_url(config), _Ansi.GREEN, _Ansi.BOLD)}")
        if running and status:
            print(_paint(f"状态: 运行中 (pid={status.get('pid')})", _Ansi.GREEN))
        else:
            print(_paint("状态: 未运行", _Ansi.YELLOW))

    choice = _select_menu(
        "WebUI 管理面板",
        "WebUI 作为后台服务运行，不占用 CLI 终端",
        [
            ("1", "启动/打开", "后台启动 WebUI 并立即返回 CLI"),
            ("2", "查看状态", "显示后台服务 PID 与启动时间"),
            ("3", "停止服务", "关闭后台 WebUI"),
            ("b", "返回首页", "回到主菜单"),
        ],
        render_status,
    ).lower()
    if choice == "1":
        result = _start_webui_background()
        state = "已在后台运行" if not result["started"] else "已后台启动"
        print(_paint(f"WebUI {state}: {result['url']} (pid={result['pid']})", _Ansi.GREEN))
        _pause()
    elif choice == "2":
        _cmd_webui_status(argparse.Namespace())
        _pause()
    elif choice == "3":
        _cmd_webui_stop(argparse.Namespace())
        _pause()


def _cli_config() -> None:
    config = _load_global_config()
    _header("运行配置", "当前 CLI、WebUI 与数据目录")
    rows = [
        ("工作根目录", str(REPO_ROOT)),
        ("数据目录", str(DATA_DIR)),
        ("全局配置", str(GLOBAL_CONFIG_PATH)),
        ("WebUI 监听", f"{config.get('webui_host', '0.0.0.0')}:{config.get('webui_port', 8080)}"),
        ("Embedding", str(config.get("embedding_url", "http://127.0.0.1:18900"))),
        ("日志级别", str(config.get("log_level", "INFO"))),
    ]
    running, status = _webui_status()
    rows.append(
        ("WebUI 状态", f"运行中 pid={status.get('pid')}" if running and status else "未运行")
    )
    for key, value in rows:
        print(f"{_paint(key + ':', _Ansi.BOLD):<18} {value}")
    _pause()


def _cmd_cli(args: argparse.Namespace) -> None:
    if os.environ.get("SIRIUS_PULSE_LEGACY_CLI") == "1":
        _cmd_legacy_cli(args)
        return
    configure_logging(level="WARNING", format_type="console")
    from sirius_pulse.tui import run_textual_cli

    result = run_textual_cli()
    if result.action == "run":
        asyncio.run(_cmd_run(argparse.Namespace(butler_port=0, butler_token=None)))


def _cmd_legacy_cli(args: argparse.Namespace) -> None:
    configure_logging(level="WARNING", format_type="console")
    _enter_tui_screen()
    try:
        while True:
            config = _load_global_config()
            choice = _select_menu(
                "交互式控制台",
                f"data: {DATA_DIR} · webui: http://localhost:{config.get('webui_port', 8080)}",
                [
                    ("1", "启动运行模式", "人格引擎 + WebUI"),
                    ("2", "WebUI 面板", "后台服务，不占用 CLI 终端"),
                    ("3", "运行配置", "查看路径、端口与日志级别"),
                    ("q", "退出", "关闭 CLI"),
                ],
            ).lower()
            if choice in {"q", "quit", "exit", "退出"}:
                _shutdown_services()
                print(_paint("再见。", _Ansi.GREEN))
                return
            if choice == "1":
                _run_cli_action(_cli_start_all)
            elif choice == "2":
                _run_cli_action(_cli_open_webui)
            elif choice == "3":
                _cli_config()
    except KeyboardInterrupt:
        _shutdown_services()
        print(_paint("已退出。", _Ansi.GREEN))
    finally:
        _exit_tui_screen()


# ---------------------------------------------------------------------------
# 助手模式
# ---------------------------------------------------------------------------


async def _cmd_assistant(args: argparse.Namespace) -> None:
    """以助手模式启动人格：连接管家端，接管消息处理。"""
    persona_name = _load_persona_name()
    butler_url = args.butler
    token = getattr(args, "token", None)
    log_level = getattr(args, "log_level", "INFO")

    if not DATA_DIR.is_dir():
        print(f"数据目录不存在: {DATA_DIR}")
        print("请先运行: python main.py init")
        raise SystemExit(1)

    log_file = DATA_DIR / "logs" / "assistant.log"
    setup_log_archival(log_file)
    configure_logging(
        level=log_level.upper(),
        format_type="console",
        log_file=str(log_file),
    )
    LOG = logging.getLogger("sirius.assistant")

    from sirius_pulse.network.assistant_client import AssistantClient
    from sirius_pulse.persona_worker import PersonaWorker

    # 1. 连接管家端
    client = AssistantClient(
        butler_url=butler_url,
        persona_name=persona_name,
        token=token,
    )
    try:
        success = await client.connect_and_takeover()
    except ConnectionError as exc:
        print(f"连接管家端失败: {exc}")
        raise SystemExit(1)

    if not success:
        print("接管请求被拒绝")
        raise SystemExit(1)

    LOG.info("已接管人格「%s」，正在启动本地引擎...", persona_name)
    print(f"已接管人格「{persona_name}」，助手模式运行中")
    print(f"  管家端: {butler_url}")
    print("  按 Ctrl+C 释放控制权并退出")

    # 2. 启动 PersonaWorker（助手模式）
    worker = PersonaWorker(DATA_DIR)

    # 注册信号处理
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
        # worker.run() 会阻塞直到 shutdown
        # 在后台监听管家端断开
        async def _watch_butler():
            await client.wait_disconnect()
            LOG.warning("与管家端的连接已断开，正在停止...")
            worker.shutdown()

        watch_task = asyncio.create_task(_watch_butler())
        await worker.run()
        watch_task.cancel()
    finally:
        await client.release()
        LOG.info("助手模式已退出，控制权已归还管家端")


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="Sirius Pulse 独立单人格 CLI")
    subparsers = parser.add_subparsers(dest="command", help="可用命令")

    subparsers.add_parser("cli", help="进入交互式 CLI")

    # init
    subparsers.add_parser("init", help="在 data/ 目录初始化人格配置")

    # run
    run_parser = subparsers.add_parser("run", help="启动人格引擎 + WebUI")
    run_parser.add_argument(
        "--butler-port",
        type=int,
        default=0,
        help="启用管家端 WebSocket 服务（指定端口号，如 9500）",
    )
    run_parser.add_argument(
        "--butler-token",
        default=None,
        help="管家端认证令牌（可选）",
    )

    # assistant
    assistant_parser = subparsers.add_parser("assistant", help="以助手模式连接管家端")
    assistant_parser.add_argument(
        "--butler",
        required=True,
        help="管家端 WebSocket 地址（如 ws://server:9500）",
    )
    assistant_parser.add_argument("--token", default=None, help="管家端认证令牌")
    assistant_parser.add_argument("--log-level", default="INFO", help="日志级别")

    # webui
    webui_parser = subparsers.add_parser("webui", help="后台启动 WebUI 管理服务")
    webui_parser.add_argument(
        "--foreground", action="store_true", help="前台运行 WebUI，用于调试或后台子进程"
    )
    webui_parser.add_argument("--status", action="store_true", help="查看后台 WebUI 状态")
    webui_parser.add_argument("--stop", action="store_true", help="停止后台 WebUI")

    args = parser.parse_args()

    try:
        if args.command is None:
            _cmd_cli(args)
        elif args.command == "cli":
            _cmd_cli(args)
        elif args.command == "init":
            _cmd_init(args)
        elif args.command == "run":
            asyncio.run(_cmd_run(args))
        elif args.command == "assistant":
            asyncio.run(_cmd_assistant(args))
        elif args.command == "webui":
            if args.status:
                _cmd_webui_status(args)
            elif args.stop:
                _cmd_webui_stop(args)
            else:
                asyncio.run(_cmd_webui(args))
        else:
            parser.print_help()
            return 1
    except KeyboardInterrupt:
        _shutdown_services()
        print(_paint("已退出。", _Ansi.GREEN))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
