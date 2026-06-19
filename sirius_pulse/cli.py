"""SiriusChat 多进程人格管理 CLI。

启动与管理多个人格实例，每个人格在独立子进程中运行。

使用方法::

    python main.py                               # 进入交互式 CLI
    python main.py run                           # 启动所有已启用人格 + WebUI
    python main.py webui                         # 仅启动 WebUI（管理模式）
    python main.py persona list                  # 列出所有人格
    python main.py persona create <name>         # 创建人格
    python main.py persona remove <name>         # 删除人格
    python main.py persona start <name>          # 前台启动单个人格
    python main.py persona stop <name>           # 停止单个人格
    python main.py persona status <name>         # 查看人格状态
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

from sirius_pulse.logging_config import configure_logging

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
            parts.append(_paint("使用 ↑/↓ 选择，Enter 确认，Esc 返回，Ctrl+C 退出", _Ansi.DIM) + "\n\n")
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


def _status_badge(running: bool) -> str:
    if running:
        return _paint("● 运行中", _Ansi.GREEN, _Ansi.BOLD)
    return _paint("○ 已停止", _Ansi.DIM)


def _manager():
    from sirius_pulse.persona_manager import PersonaManager

    config = _load_global_config()
    return PersonaManager(DATA_DIR, global_config=config)


def _print_persona_table() -> list[dict[str, Any]]:
    manager = _manager()
    personas = manager.list_personas()
    if not personas:
        print(_paint("还没有人格。可以在「人格管理」中创建第一个人格。", _Ansi.YELLOW))
        return []

    print(
        _paint(
            f"{_pad('序号', 6)}{_pad('人格', 18)}{_pad('角色名', 18)}{_pad('状态', 16)}{_pad('PID', 10)}{_pad('端口', 8)}",
            _Ansi.BOLD,
        )
    )
    print(_paint("─" * 76, _Ansi.DIM))
    for idx, persona in enumerate(personas, 1):
        name = str(persona.get("name") or "-")[:16]
        persona_name = str(persona.get("persona_name") or "-")[:16]
        pid = str(persona.get("pid") or "-")
        port = str(manager.get_port(persona["name"]) or "-")
        print(
            f"{_pad(str(idx), 6)}{_pad(name, 18)}{_pad(persona_name, 18)}"
            f"{_pad(_status_badge(bool(persona.get('running'))), 24)}{_pad(pid, 10)}{_pad(port, 8)}"
        )
    return personas


def _select_persona(prompt_text: str = "选择人格序号或名称: ") -> str | None:
    personas = _print_persona_table()
    if not personas:
        return None
    if sys.stdin.isatty():
        items = [
            (str(idx), str(persona["name"]), str(persona.get("persona_name") or "—"))
            for idx, persona in enumerate(personas, 1)
        ]
        items.append(("b", "返回", "取消选择"))
        choice = _select_menu("选择人格", "使用方向键选择目标人格", items)
        if choice in {"b", "back", "q"}:
            return None
        index = int(choice) - 1
        return str(personas[index]["name"])
    value = _prompt(f"\n{prompt_text}")
    if not value:
        return None
    if value.isdigit():
        index = int(value) - 1
        if 0 <= index < len(personas):
            return str(personas[index]["name"])
    names = {str(p["name"]) for p in personas}
    if value in names:
        return value
    print(_paint("未找到对应人格。", _Ansi.RED))
    _pause()
    return None


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
    try:
        manager = _manager()
        manager.stop_all()
        print(_paint("已停止所有人格子进程", _Ansi.GREEN))
    except Exception as exc:
        print(_paint(f"停止人格进程失败: {exc}", _Ansi.RED))


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
    """启动所有已启用的人格 + WebUI。NapCat 由人格子进程自动管理。"""
    config = _load_global_config()
    webui_log_file = DATA_DIR / "logs" / "webui.log"
    configure_logging(
        level=config.get("log_level", "INFO"),
        format_type="console",
        log_file=webui_log_file,
    )
    LOG = logging.getLogger("sirius.main")

    from sirius_pulse.persona_manager import PersonaManager
    from sirius_pulse.webui import WebUIServer

    persona_manager = PersonaManager(DATA_DIR, global_config=config)

    # ── 先启动 WebUI（含 Embedding 服务），确保子进程能连上 ──
    webui = WebUIServer(
        persona_manager=persona_manager,
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
            f"Embedding 服务不可用 ({emb_url})。" "请检查日志或手动启动: python -m sirius_pulse.embedding.server"
        )

    # ── 启动所有已启用人格（worker 子进程会自动管理 NapCat 实例）──
    LOG.info("正在启动已启用人格...")
    results = persona_manager.start_all()
    for name, ok in results.items():
        LOG.info("  %s %s", "✓" if ok else "✗", name)

    LOG.info("按 Ctrl+C 停止所有服务")

    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        pass
    finally:
        LOG.info("正在停止所有人格...")
        persona_manager.stop_all()
        await webui.stop()
        LOG.info("所有服务已停止")


async def _cmd_webui(args: argparse.Namespace) -> None:
    """仅启动 WebUI（不启动任何人格）。"""
    if not getattr(args, "foreground", False):
        result = _start_webui_background()
        state = "已在后台运行" if not result["started"] else "已后台启动"
        print(f"WebUI {state}: {result['url']} (pid={result['pid']})")
        return

    config = _load_global_config()
    configure_logging(level=config.get("log_level", "INFO"), format_type="console")
    LOG = logging.getLogger("sirius.main")

    from sirius_pulse.persona_manager import PersonaManager
    from sirius_pulse.webui import WebUIServer

    persona_manager = PersonaManager(DATA_DIR, global_config=config)
    webui = WebUIServer(
        persona_manager=persona_manager,
        host=str(config.get("webui_host", "0.0.0.0")),
        port=int(config.get("webui_port", 8080)),
    )
    await webui.start()
    _write_webui_status(os.getpid(), config)
    LOG.info("WebUI: http://localhost:%s（仅管理模式，无人格运行）", webui.port)

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


def _cmd_persona_list(args: argparse.Namespace) -> None:
    """列出所有人格（含进程存活检测）。"""
    configure_logging(level="WARNING", format_type="console")
    from sirius_pulse.persona_manager import PersonaManager

    config = _load_global_config()
    manager = PersonaManager(DATA_DIR, global_config=config)
    personas = manager.list_personas()
    if not personas:
        print("暂无任何人格。使用 `python main.py persona create <name>` 创建。")
        return

    print(f"{'人格名':<12} {'角色名':<12} {'状态':<8} {'PID':<8} {'端口':<8} {'Adapter'}")
    print("-" * 70)
    for p in personas:
        status = "运行中" if p.get("running") else "已停止"
        pid = str(p.get("pid") or "-")
        port = str(manager.get_port(p["name"]) or "-")
        adapters = p.get("adapters_count", 0)
        print(
            f"{p['name']:<12} {p.get('persona_name') or '-':<12} {status:<8} {pid:<8} {port:<8} {adapters}"
        )


def _cmd_persona_create(args: argparse.Namespace) -> None:
    """创建新人格。"""
    configure_logging(level="WARNING", format_type="console")
    from sirius_pulse.persona_manager import PersonaManager

    config = _load_global_config()
    manager = PersonaManager(DATA_DIR, global_config=config)
    try:
        pdir = manager.create_persona(
            args.name,
            persona_name=args.name,
        )
        print(f"人格已创建: {args.name}")
        print(f"  目录: {pdir}")
        print(f"  请编辑 {pdir / 'adapters.json'} 配置连接，然后运行:")
        print(f"    python main.py run")
    except FileExistsError:
        print(f"人格已存在: {args.name}")
        sys.exit(1)


def _cmd_persona_remove(args: argparse.Namespace) -> None:
    """删除人格。"""
    configure_logging(level="WARNING", format_type="console")
    from sirius_pulse.persona_manager import PersonaManager

    config = _load_global_config()
    manager = PersonaManager(DATA_DIR, global_config=config)
    ok = manager.remove_persona(args.name)
    if ok:
        print(f"人格已删除: {args.name}")
    else:
        print(f"人格不存在: {args.name}")
        sys.exit(1)


def _cmd_persona_migrate(args: argparse.Namespace) -> None:
    """从旧目录迁移人格。"""
    configure_logging(level="INFO", format_type="console")
    from sirius_pulse.persona_manager import PersonaManager

    config = _load_global_config()
    manager = PersonaManager(DATA_DIR, global_config=config)
    source = Path(args.source).resolve()
    if not source.exists():
        print(f"源目录不存在: {source}")
        sys.exit(1)

    try:
        pdir = manager.migrate_persona(source, args.name)
        print(f"人格已迁移: {args.name}")
        print(f"  目录: {pdir}")
        port = manager.get_port(args.name)
        if port:
            print(f"  分配端口: {port}")
            print(f"  请为该人格配置 NapCat (QQ) 并监听端口 {port}")
    except FileExistsError as exc:
        print(f"迁移失败: {exc}")
        sys.exit(1)
    except FileNotFoundError as exc:
        print(f"迁移失败: {exc}")
        sys.exit(1)


async def _cmd_persona_start(args: argparse.Namespace) -> None:
    """前台启动单个人格（含 NapCat 自动管理）。"""
    from sirius_pulse.persona_config import NapCatAdapterConfig, PersonaAdaptersConfig
    from sirius_pulse.persona_worker import PersonaWorker

    pdir = DATA_DIR / "personas" / args.name
    if not pdir.exists():
        print(f"人格不存在: {args.name}")
        sys.exit(1)

    configure_logging(level="INFO", format_type="console")
    LOG = logging.getLogger("sirius.main")

    # ── NapCat 自动管理（默认启用）─────────────────────────
    napcat_mgr = None
    config = _load_global_config()
    adapters = PersonaAdaptersConfig.load(pdir / "adapters.json")
    for a in adapters.adapters:
        if isinstance(a, NapCatAdapterConfig) and a.enabled and a.qq_number:
            from sirius_pulse.platforms.onebot_v11.napcat.manager import NapCatManager

            napcat_install_dir = str(config.get("napcat_install_dir", str(REPO_ROOT / "napcat")))
            napcat_mgr = NapCatManager.for_persona(
                global_install_dir=napcat_install_dir,
                persona_name=args.name,
            )
            if not napcat_mgr.is_installed:
                LOG.info("NapCat 未安装，尝试自动安装...")
                result = await napcat_mgr.install()
                if not result["success"]:
                    LOG.warning("NapCat 安装失败: %s", result["message"])
                    break
            port = int(a.ws_url.rsplit(":", 1)[-1]) if ":" in a.ws_url else 3001
            napcat_mgr.configure(qq_number=a.qq_number, ws_port=port)
            result = await napcat_mgr.start(qq_number=a.qq_number)
            if result["success"]:
                LOG.info("NapCat 已启动，等待 WS 就绪...")
                ready = await napcat_mgr.wait_for_ws(port=port, timeout=120.0)
                if ready:
                    LOG.info("NapCat WS 已就绪")
                else:
                    LOG.warning("NapCat WS 未就绪，请检查 QQ 是否已扫码登录")
            else:
                LOG.warning("NapCat 启动失败: %s", result["message"])
            break

    worker = PersonaWorker(pdir)

    # 信号处理（Windows 兼容）
    if sys.platform == "win32":
        import signal as _signal

        def _sig_handler(_s, _f):
            worker.shutdown()

        _signal.signal(_signal.SIGINT, _sig_handler)
        _signal.signal(_signal.SIGTERM, _sig_handler)
    else:
        loop = asyncio.get_running_loop()
        for sig in (__import__("signal").SIGTERM, __import__("signal").SIGINT):
            loop.add_signal_handler(sig, worker.shutdown)

    try:
        await worker.run()
    except Exception:
        LOG.exception("人格工作进程异常退出")
        raise
    finally:
        if napcat_mgr and napcat_mgr.is_running:
            LOG.info("正在停止 NapCat...")
            await napcat_mgr.stop()


def _cmd_persona_stop(args: argparse.Namespace) -> None:
    """停止单个人格。"""
    configure_logging(level="WARNING", format_type="console")
    from sirius_pulse.persona_manager import PersonaManager

    config = _load_global_config()
    manager = PersonaManager(DATA_DIR, global_config=config)
    ok = manager.stop_persona(args.name)
    if ok:
        print(f"人格已停止: {args.name}")
    else:
        print(f"人格未在运行或不存在: {args.name}")


def _cmd_persona_status(args: argparse.Namespace) -> None:
    """查看人格状态。"""
    configure_logging(level="WARNING", format_type="console")
    from sirius_pulse.persona_manager import PersonaManager

    config = _load_global_config()
    manager = PersonaManager(DATA_DIR, global_config=config)
    info = manager.get_persona_status(args.name)
    if info is None:
        print(f"人格不存在: {args.name}")
        sys.exit(1)

    # 简洁格式输出
    print(f"人格: {info['name']}")
    print(f"角色名: {info.get('persona_name') or '—'}")
    print(f"状态: {'运行中' if info.get('running') else '已停止'}")
    print(f"PID: {info.get('pid') or '—'}")
    print(f"端口: {manager.get_port(args.name) or '—'}")
    print(f"Adapter: {info.get('adapters_count', 0)} 个")
    print(f"心跳: {info.get('heartbeat_at') or '—'}")
    print(f"目录: {info['work_path']}")


def _cmd_persona_logs(args: argparse.Namespace) -> None:
    """查看人格日志。"""
    configure_logging(level="WARNING", format_type="console")
    from sirius_pulse.persona_manager import PersonaManager

    config = _load_global_config()
    manager = PersonaManager(DATA_DIR, global_config=config)
    logs = manager.get_logs(args.name, lines=args.lines)
    if not logs:
        print("暂无日志")
        return
    for line in logs:
        print(line)


def _cli_start_all() -> None:
    _header("启动运行模式", "WebUI 与所有已启用人格会一起启动")
    print(_paint("即将进入长期运行模式，按 Ctrl+C 可停止所有服务。", _Ansi.YELLOW))
    choice = _prompt("继续启动？[Y/n] ").lower()
    if choice in {"n", "no", "否"}:
        return
    asyncio.run(_cmd_run(argparse.Namespace()))


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


def _cli_personas() -> None:
    while True:
        choice = _select_menu(
            "人格管理",
            "查看状态、创建人格、启停单个人格",
            [
                ("1", "创建人格", "生成独立配置目录与默认适配器"),
                ("2", "启动人格", "后台启动单个人格"),
                ("3", "停止人格", "停止后台运行的人格"),
                ("4", "查看详情", "显示心跳、端口与目录"),
                ("b", "返回首页", "回到主菜单"),
            ],
            _print_persona_table,
        ).lower()
        if choice in {"b", "back", "q"}:
            return
        if choice == "1":
            name = _prompt("人格标识名: ")
            if name:
                _run_cli_action(lambda: _cmd_persona_create(argparse.Namespace(name=name)))
                _pause()
        elif choice == "2":
            name = _select_persona()
            if name:
                _header(f"启动人格 {name}", "人格会作为后台子进程运行")
                print(_paint("启动后可在 WebUI 实时日志页面查看运行日志。", _Ansi.YELLOW))
                if _prompt("继续启动？[Y/n] ").lower() not in {"n", "no", "否"}:
                    asyncio.run(_cmd_persona_start(argparse.Namespace(name=name)))
        elif choice == "3":
            name = _select_persona()
            if name:
                _run_cli_action(lambda: _cmd_persona_stop(argparse.Namespace(name=name)))
                _pause()
        elif choice == "4":
            name = _select_persona()
            if name:
                _header(f"人格详情 {name}")
                _run_cli_action(lambda: _cmd_persona_status(argparse.Namespace(name=name)))
                _pause()


def _cli_logs() -> None:
    while True:
        _header("日志查看", "日志作为 CLI 中的可选界面")
        name = _select_persona("选择要查看日志的人格序号或名称，留空返回: ")
        if not name:
            return
        raw_lines = _prompt("显示行数 [80]: ") or "80"
        try:
            lines = max(1, int(raw_lines))
        except ValueError:
            lines = 80
        _header(f"{name} 日志", f"最近 {lines} 行")
        _run_cli_action(lambda: _cmd_persona_logs(argparse.Namespace(name=name, lines=lines)))
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
    rows.append(("WebUI 状态", f"运行中 pid={status.get('pid')}" if running and status else "未运行"))
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
        asyncio.run(_cmd_run(argparse.Namespace()))


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
                    ("1", "启动运行模式", "所有已启用人格 + WebUI"),
                    ("2", "WebUI 面板", "后台服务，不占用 CLI 终端"),
                    ("3", "人格管理", "创建、查看、启停单个人格"),
                    ("4", "日志界面", "查看人格 worker 日志"),
                    ("5", "运行配置", "查看路径、端口与日志级别"),
                    ("q", "退出", "关闭 CLI"),
                ],
                _print_persona_table,
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
                _cli_personas()
            elif choice == "4":
                _cli_logs()
            elif choice == "5":
                _cli_config()
    except KeyboardInterrupt:
        _shutdown_services()
        print(_paint("已退出。", _Ansi.GREEN))
    finally:
        _exit_tui_screen()


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="SiriusChat 多进程人格管理 CLI")
    subparsers = parser.add_subparsers(dest="command", help="可用命令")

    subparsers.add_parser("cli", help="进入交互式 CLI")

    # run
    subparsers.add_parser("run", help="启动所有已启用人格 + WebUI")

    # webui
    webui_parser = subparsers.add_parser("webui", help="后台启动 WebUI 管理服务")
    webui_parser.add_argument("--foreground", action="store_true", help="前台运行 WebUI，用于调试或后台子进程")
    webui_parser.add_argument("--status", action="store_true", help="查看后台 WebUI 状态")
    webui_parser.add_argument("--stop", action="store_true", help="停止后台 WebUI")

    # persona
    persona_parser = subparsers.add_parser("persona", help="人格管理")
    persona_sub = persona_parser.add_subparsers(dest="persona_cmd", help="人格子命令")

    persona_sub.add_parser("list", help="列出所有人格")

    create_parser = persona_sub.add_parser("create", help="创建人格")
    create_parser.add_argument("name", help="人格标识名（目录名）")

    remove_parser = persona_sub.add_parser("remove", help="删除人格")
    remove_parser.add_argument("name", help="人格标识名")

    migrate_parser = persona_sub.add_parser("migrate", help="从旧目录迁移人格")
    migrate_parser.add_argument("--source", required=True, help="源目录路径（如 data/bot）")
    migrate_parser.add_argument("--name", required=True, help="目标人格标识名")

    start_parser = persona_sub.add_parser("start", help="后台启动单个人格")
    start_parser.add_argument("name", help="人格标识名")

    stop_parser = persona_sub.add_parser("stop", help="停止单个人格")
    stop_parser.add_argument("name", help="人格标识名")

    status_parser = persona_sub.add_parser("status", help="查看人格状态")
    status_parser.add_argument("name", help="人格标识名")

    logs_parser = persona_sub.add_parser("logs", help="查看人格日志")
    logs_parser.add_argument("name", help="人格标识名")
    logs_parser.add_argument("--lines", type=int, default=50, help="显示行数")

    args = parser.parse_args()

    try:
        if args.command is None:
            _cmd_cli(args)
        elif args.command == "cli":
            _cmd_cli(args)
        elif args.command == "run":
            asyncio.run(_cmd_run(args))
        elif args.command == "webui":
            if args.status:
                _cmd_webui_status(args)
            elif args.stop:
                _cmd_webui_stop(args)
            else:
                asyncio.run(_cmd_webui(args))
        elif args.command == "persona":
            if args.persona_cmd == "list":
                _cmd_persona_list(args)
            elif args.persona_cmd == "create":
                _cmd_persona_create(args)
            elif args.persona_cmd == "remove":
                _cmd_persona_remove(args)
            elif args.persona_cmd == "migrate":
                _cmd_persona_migrate(args)
            elif args.persona_cmd == "start":
                asyncio.run(_cmd_persona_start(args))
            elif args.persona_cmd == "stop":
                _cmd_persona_stop(args)
            elif args.persona_cmd == "status":
                _cmd_persona_status(args)
            elif args.persona_cmd == "logs":
                _cmd_persona_logs(args)
            else:
                persona_parser.print_help()
                return 1
        else:
            parser.print_help()
            return 1
    except KeyboardInterrupt:
        _shutdown_services()
        print(_paint("已退出。", _Ansi.GREEN))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
