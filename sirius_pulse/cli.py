"""Sirius Pulse CLI。

命令行启动与管理人格实例，所有交互通过 WebUI 完成。

使用方法::

    start.pyw                                    # 无窗口启动（双击运行）
    python main.py run                           # 启动活跃人格引擎 + WebUI
    python main.py webui                         # 仅启动 WebUI（管理模式）
    python main.py assistant --butler ws://...   # 以助手模式连接管家端
    python main.py persona list                  # 列出所有人格
    python main.py persona create <名称>         # 创建新人格
    python main.py persona activate <名称>       # 切换活跃人格
    python main.py persona delete <名称>         # 删除人格
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import signal
import socket
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from sirius_pulse.logging_config import (
    add_filtered_file_handler,
    configure_logging,
    setup_log_archival,
)

REPO_ROOT = Path(os.environ.get("SIRIUS_PULSE_HOME", Path.cwd())).expanduser().resolve()
DATA_DIR = REPO_ROOT / "data"
GLOBAL_CONFIG_PATH = DATA_DIR / "global_config.json"
WEBUI_STATUS_PATH = DATA_DIR / "webui_status.json"

WEBUI_LOGGER_PREFIXES = (
    "sirius.main",
    "sirius.webui",
    "sirius.persona_manager",
    "sirius.butler_server",
    "sirius.data_sync",
    "embedding.",
    "sirius_pulse.embedding.",
)

PERSONA_LOGGER_PREFIXES = (
    "sirius.persona_worker",
    "sirius.platforms.",
    "platforms.",
    "core.",
    "engine_",
    "plugin.",
    "sirius_pulse.core.",
    "sirius_pulse.memory.",
    "sirius_pulse.platforms.",
    "sirius_pulse.plugins.",
    "sirius_pulse.providers.",
    "sirius_pulse.skills.",
    "sirius_pulse.token.",
)


# ---------------------------------------------------------------------------
# ANSI 工具
# ---------------------------------------------------------------------------


class _Ansi:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    CYAN = "\033[36m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    RED = "\033[31m"


def _use_color() -> bool:
    return sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _paint(text: str, *styles: str) -> str:
    if not _use_color():
        return text
    return "".join(styles) + text + _Ansi.RESET


# ---------------------------------------------------------------------------
# 全局配置
# ---------------------------------------------------------------------------


def _default_global_config() -> dict:
    return {
        "active_persona": "default",
        "webui_host": "0.0.0.0",
        "webui_port": 8080,
        "napcat_install_dir": str(REPO_ROOT / "napcat"),
        "log_level": "INFO",
    }


def _load_global_config() -> dict:
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


def _save_global_config(config: dict) -> None:
    GLOBAL_CONFIG_PATH.write_text(
        json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _get_active_persona_dir() -> Path:
    config = _load_global_config()
    name = config.get("active_persona", "default")
    return DATA_DIR / "personas" / name


# ---------------------------------------------------------------------------
# WebUI 后台管理
# ---------------------------------------------------------------------------


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

    # 使用 pythonw.exe 启动后台进程，避免弹出 CMD 窗口
    pythonw = Path(sys.executable).parent / "pythonw.exe"
    python_exe = str(pythonw) if pythonw.exists() else sys.executable
    cmd = [python_exe, "-m", "sirius_pulse.cli", "webui", "--foreground"]
    kwargs: dict[str, Any] = {
        "cwd": str(REPO_ROOT),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform == "win32":
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0  # SW_HIDE
        kwargs["startupinfo"] = si
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


# ---------------------------------------------------------------------------
# 数据迁移（旧扁平格式 → 多人格目录）
# ---------------------------------------------------------------------------


def _migrate_flat_to_personas() -> None:
    if not (DATA_DIR / "persona.json").exists():
        return
    if (DATA_DIR / "personas").exists():
        return

    LOG = logging.getLogger("sirius.migrate")
    LOG.info("检测到旧格式数据目录，正在迁移到多人格结构...")

    default_dir = DATA_DIR / "personas" / "default"
    default_dir.mkdir(parents=True)

    persona_items = [
        "persona.json", "experience.json", "orchestration.json", "adapters.json",
        "engine_state", "archive", "glossary", "diary", "plugins", "skills",
        "logs", "image_cache", "plugin_data", "persona.db",
    ]

    migrated = []
    for item_name in persona_items:
        src = DATA_DIR / item_name
        if not src.exists():
            continue
        dst = default_dir / item_name
        try:
            shutil.move(str(src), str(dst))
            migrated.append(item_name)
        except Exception as exc:
            LOG.warning("迁移 %s 失败: %s", item_name, exc)

    if migrated:
        config = _load_global_config()
        config["active_persona"] = "default"
        _save_global_config(config)
        print(_paint(f"数据迁移完成: {', '.join(migrated)} → personas/default/", _Ansi.GREEN))


# ---------------------------------------------------------------------------
# 子命令实现
# ---------------------------------------------------------------------------


async def _cmd_run(args: argparse.Namespace) -> None:
    """启动活跃人格引擎 + WebUI。"""
    _migrate_flat_to_personas()

    config = _load_global_config()
    persona_dir = _get_active_persona_dir()

    if not persona_dir.exists():
        print(_paint(f"活跃人格目录不存在: {persona_dir}", _Ansi.RED))
        print("请运行 `python main.py persona list` 查看可用人格")
        raise SystemExit(1)

    log_level = str(config.get("log_level", "INFO")).upper()
    webui_log_file = DATA_DIR / "logs" / "webui.log"
    persona_log_file = persona_dir / "logs" / "persona.log"
    configure_logging(
        level=log_level,
        format_type="console",
    )
    add_filtered_file_handler(
        webui_log_file,
        logger_prefixes=WEBUI_LOGGER_PREFIXES,
        level=log_level,
        format_type="console",
    )
    add_filtered_file_handler(
        persona_log_file,
        logger_prefixes=PERSONA_LOGGER_PREFIXES,
        level=log_level,
        format_type="console",
    )
    LOG = logging.getLogger("sirius.main")

    from sirius_pulse.webui import WebUIServer

    webui = WebUIServer(
        data_dir=DATA_DIR,
        host=str(config.get("webui_host", "0.0.0.0")),
        port=int(config.get("webui_port", 8080)),
    )
    await webui.start()
    LOG.info("WebUI: http://localhost:%s", webui.port)

    # 等待 Embedding 服务就绪
    import time
    from sirius_pulse.embedding.client import EmbeddingClient

    emb_url = config.get("embedding_url", "http://127.0.0.1:18900")
    emb_client = EmbeddingClient(base_url=emb_url)
    LOG.info("等待 Embedding 服务就绪: %s ...", emb_url)
    for _attempt in range(120):
        if emb_client.check_health():
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

    from sirius_pulse.persona_worker import PersonaWorker

    worker = PersonaWorker(persona_dir)
    webui.persona_manager = worker
    LOG.info("活跃人格: %s (%s)", config.get("active_persona", "default"), persona_dir)

    # 可选：启动 ButlerServer
    butler_port = getattr(args, "butler_port", 0)
    butler_server = None
    if butler_port > 0:
        from sirius_pulse.network.butler_server import ButlerServer

        butler_server = ButlerServer(
            host="0.0.0.0",
            port=butler_port,
            data_dir=persona_dir,
            token=getattr(args, "butler_token", None),
        )
        await butler_server.start()
        LOG.info("ButlerServer 已启动: ws://0.0.0.0:%d", butler_port)

    stop_all_event = asyncio.Event()

    def _request_shutdown() -> None:
        stop_all_event.set()
        worker.shutdown()

    if sys.platform == "win32":
        import signal as _signal
        def _sig_handler(_s, _f):
            _request_shutdown()
        _signal.signal(_signal.SIGINT, _sig_handler)
        _signal.signal(_signal.SIGTERM, _sig_handler)
    else:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, _request_shutdown)

    LOG.info("按 Ctrl+C 停止所有服务")

    try:
        await worker.run()
        if not stop_all_event.is_set():
            LOG.info("Persona worker stopped; WebUI remains running")
            await stop_all_event.wait()
    finally:
        if butler_server:
            await butler_server.stop()
        await webui.stop()
        LOG.info("所有服务已停止")


async def _cmd_assistant(args: argparse.Namespace) -> None:
    """以助手模式启动人格：连接管家端，接管消息处理。"""
    _migrate_flat_to_personas()

    config = _load_global_config()
    persona_dir = _get_active_persona_dir()
    persona_name = config.get("active_persona", "default")
    butler_url = args.butler
    token = getattr(args, "token", None)
    log_level = getattr(args, "log_level", "INFO")

    if not persona_dir.is_dir():
        print(f"人格目录不存在: {persona_dir}")
        print("请先运行: python main.py persona create <名称>")
        raise SystemExit(1)

    log_file = persona_dir / "logs" / "assistant.log"
    setup_log_archival(log_file)
    configure_logging(level=log_level.upper(), format_type="console", log_file=str(log_file))
    LOG = logging.getLogger("sirius.assistant")

    from sirius_pulse.network.assistant_client import AssistantClient
    from sirius_pulse.persona_worker import PersonaWorker

    client = AssistantClient(butler_url=butler_url, persona_name=persona_name, token=token)
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

    worker = PersonaWorker(persona_dir)

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


# ---------------------------------------------------------------------------
# 人格管理命令
# ---------------------------------------------------------------------------


def _cmd_persona_list(args: argparse.Namespace) -> None:
    config = _load_global_config()
    active = config.get("active_persona", "")
    personas_dir = DATA_DIR / "personas"

    if not personas_dir.exists():
        print("暂无人格。运行 `python main.py persona create <名称>` 创建。")
        return

    print(f"人格列表（当前活跃: {active or '无'}）\n")
    found = False
    for d in sorted(personas_dir.iterdir()):
        if not d.is_dir():
            continue
        found = True
        marker = _paint(" ● ", _Ansi.GREEN) if d.name == active else "   "
        persona_file = d / "persona.json"
        display = d.name
        if persona_file.exists():
            try:
                data = json.loads(persona_file.read_text(encoding="utf-8"))
                display = data.get("name", d.name)
            except Exception:
                pass
        print(f"{marker}{_paint(d.name, _Ansi.BOLD)}  {display}")

    if not found:
        print("暂无人格。运行 `python main.py persona create <名称>` 创建。")


def _cmd_persona_create(args: argparse.Namespace) -> None:
    import re

    name = args.name
    if not re.match(r"^[a-zA-Z0-9_\-一-鿿]+$", name):
        print(_paint("人格名称只能包含字母、数字、中文、下划线和连字符", _Ansi.RED))
        return

    persona_dir = DATA_DIR / "personas" / name
    if persona_dir.exists():
        print(_paint(f"人格「{name}」已存在", _Ansi.RED))
        return

    persona_dir.mkdir(parents=True)
    for subdir in ("engine_state", "archive", "plugins", "skills", "logs", "image_cache"):
        (persona_dir / subdir).mkdir(exist_ok=True)

    (persona_dir / "persona.json").write_text(
        json.dumps({"name": name, "aliases": []}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (persona_dir / "experience.json").write_text(
        json.dumps({}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (persona_dir / "adapters.json").write_text(
        json.dumps({"adapters": [{"type": "napcat", "enabled": False}]}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    print(_paint(f"已创建人格「{name}」", _Ansi.GREEN))
    print(f"  目录: {persona_dir}")
    print(f"  激活: python main.py persona activate {name}")


def _cmd_persona_activate(args: argparse.Namespace) -> None:
    name = args.name
    persona_dir = DATA_DIR / "personas" / name
    if not persona_dir.exists():
        print(_paint(f"人格「{name}」不存在", _Ansi.RED))
        return

    config = _load_global_config()
    config["active_persona"] = name
    _save_global_config(config)
    print(_paint(f"已切换活跃人格: {name}", _Ansi.GREEN))


def _cmd_persona_delete(args: argparse.Namespace) -> None:
    name = args.name
    config = _load_global_config()
    active = config.get("active_persona", "")

    if name == active:
        print(_paint("不能删除当前活跃的人格。请先切换到其他人格。", _Ansi.RED))
        return

    persona_dir = DATA_DIR / "personas" / name
    if not persona_dir.exists():
        print(_paint(f"人格「{name}」不存在", _Ansi.RED))
        return

    if not getattr(args, "force", False):
        try:
            choice = input(f"确定删除人格「{name}」？此操作不可恢复。[y/N] ").strip().lower()
        except (KeyboardInterrupt, EOFError):
            print("\n已取消")
            return
        if choice not in {"y", "yes", "是"}:
            print("已取消")
            return

    shutil.rmtree(persona_dir)
    print(_paint(f"已删除人格「{name}」", _Ansi.GREEN))


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="Sirius Pulse CLI")
    subparsers = parser.add_subparsers(dest="command", help="可用命令")

    # run
    run_parser = subparsers.add_parser("run", help="启动活跃人格引擎 + WebUI")
    run_parser.add_argument("--butler-port", type=int, default=0, help="启用管家端 WebSocket 端口")
    run_parser.add_argument("--butler-token", default=None, help="管家端认证令牌")

    # webui
    webui_parser = subparsers.add_parser("webui", help="启动 WebUI 管理服务")
    webui_parser.add_argument("--foreground", action="store_true", help="前台运行")
    webui_parser.add_argument("--status", action="store_true", help="查看 WebUI 状态")
    webui_parser.add_argument("--stop", action="store_true", help="停止后台 WebUI")

    # assistant
    assistant_parser = subparsers.add_parser("assistant", help="以助手模式连接管家端")
    assistant_parser.add_argument("--butler", required=True, help="管家端 WebSocket 地址")
    assistant_parser.add_argument("--token", default=None, help="管家端认证令牌")
    assistant_parser.add_argument("--log-level", default="INFO", help="日志级别")

    # persona
    persona_parser = subparsers.add_parser("persona", help="人格管理")
    persona_sub = persona_parser.add_subparsers(dest="persona_action")
    persona_sub.add_parser("list", help="列出所有人格")
    p_create = persona_sub.add_parser("create", help="创建新人格")
    p_create.add_argument("name", help="人格名称")
    p_activate = persona_sub.add_parser("activate", help="切换活跃人格")
    p_activate.add_argument("name", help="人格名称")
    p_delete = persona_sub.add_parser("delete", help="删除人格")
    p_delete.add_argument("name", help="人格名称")
    p_delete.add_argument("--force", action="store_true", help="跳过确认")

    args = parser.parse_args()

    try:
        if args.command is None:
            parser.print_help()
        elif args.command == "run":
            asyncio.run(_cmd_run(args))
        elif args.command == "webui":
            if args.status:
                _cmd_webui_status(args)
            elif args.stop:
                _cmd_webui_stop(args)
            else:
                asyncio.run(_cmd_webui(args))
        elif args.command == "assistant":
            asyncio.run(_cmd_assistant(args))
        elif args.command == "persona":
            if args.persona_action == "list":
                _cmd_persona_list(args)
            elif args.persona_action == "create":
                _cmd_persona_create(args)
            elif args.persona_action == "activate":
                _cmd_persona_activate(args)
            elif args.persona_action == "delete":
                _cmd_persona_delete(args)
            else:
                persona_parser.print_help()
        else:
            parser.print_help()
            return 1
    except KeyboardInterrupt:
        print(_paint("\n已退出。", _Ansi.GREEN))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
