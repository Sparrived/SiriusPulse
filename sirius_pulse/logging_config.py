"""
日志系统配置模块

提供结构化日志配置，支持以下功能：
- 日志级别可配置 (DEBUG/INFO/WARNING/ERROR)
- 两种输出格式：Console（易读）和JSON（易解析）
- 异步日志处理（可选）
- 日志文件循环（可选）
"""

from __future__ import annotations

import json
import logging
import logging.handlers
import shutil
import sys
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

# 日志级别类型
LogLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]

# 日志格式类型
LogFormat = Literal["console", "json"]


class JSONFormatter(logging.Formatter):
    """JSON格式化器，将日志转换为JSON结构化输出"""

    def format(self, record: logging.LogRecord) -> str:
        log_data: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(record.created).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # 添加额外的上下文信息（extra字段）
        if hasattr(record, "__dict__"):
            for key, value in record.__dict__.items():
                if key not in (
                    "name",
                    "msg",
                    "args",
                    "created",
                    "filename",
                    "funcName",
                    "levelname",
                    "levelno",
                    "lineno",
                    "module",
                    "msecs",
                    "message",
                    "pathname",
                    "process",
                    "processName",
                    "relativeCreated",
                    "thread",
                    "threadName",
                    "exc_info",
                    "exc_text",
                    "stack_info",
                    "asctime",
                ):
                    if not key.startswith("_"):
                        log_data[key] = value

        # 添加异常信息
        if record.exc_info:
            log_data["exception"] = self.formatException(record.exc_info)

        # 添加堆栈信息（若启用）
        if record.stack_info:
            log_data["stack"] = record.stack_info

        return json.dumps(log_data, ensure_ascii=False)


class FlushingFileHandler(logging.FileHandler):
    """实时刷新的文件处理器 - 每条日志立即写入硬盘"""

    def emit(self, record: logging.LogRecord) -> None:
        """发射日志记录后立即刷新"""
        try:
            super().emit(record)
            self.flush()  # 立即刷新到磁盘
        except Exception:
            self.handleError(record)


class FlushingTimedRotatingFileHandler(logging.handlers.TimedRotatingFileHandler):
    """实时刷新的定时轮换文件处理器"""

    def emit(self, record: logging.LogRecord) -> None:
        """发射日志记录后立即刷新"""
        try:
            super().emit(record)
            self.flush()  # 立即刷新到磁盘
        except Exception:
            self.handleError(record)


def _archive_old_logs(log_file: Path) -> None:
    """
    将已存在的日志文件归档到 archive 目录下
    需在日志处理器创建前调用，确保文件未被锁定

    Args:
        log_file: 日志文件路径
    """
    if not log_file.exists():
        return

    # 创建归档目录
    archive_dir = log_file.parent / "archive"
    archive_dir.mkdir(parents=True, exist_ok=True)

    # 使用时间戳为旧日志重命名，避免冲突
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    archive_file = archive_dir / f"{log_file.stem}_{timestamp}{log_file.suffix}"

    try:
        # 先复制，再删除原文件（比直接移动更安全）
        shutil.copy2(str(log_file), str(archive_file))
        log_file.unlink()
    except Exception:
        # 如果失败（如权限问题），忽略错误，继续创建新日志
        pass


def setup_log_archival(log_file: Path) -> None:
    """
    在应用启动时调用，在创建日志处理器之前执行
    将旧日志文件归档

    Args:
        log_file: 主日志文件路径
    """
    # 确保日志文件的父目录存在
    log_file.parent.mkdir(parents=True, exist_ok=True)

    # 执行归档
    _archive_old_logs(log_file)


# 初始化 colorama（Windows 终端 ANSI 支持）
try:
    import colorama

    colorama.init()
    _COLORAMA_OK = True
except ImportError:
    _COLORAMA_OK = False


class ColoredFormatter(logging.Formatter):
    """带颜色的 Console 格式化器，提高可读性。

    格式：2024-01-15 10:23:45  INFO     napcat_adapter     NapCat WS connected
    """

    # 级别颜色
    LEVEL_STYLES: dict[str, tuple[str, str]] = {
        "DEBUG": ("\033[36m", "\033[0m"),  # 青色
        "INFO": ("\033[32m", "\033[0m"),  # 绿色
        "WARNING": ("\033[33m", "\033[0m"),  # 黄色
        "ERROR": ("\033[31m", "\033[0m"),  # 红色
        "CRITICAL": ("\033[1;37;41m", "\033[0m"),  # 白字红底加粗
    }

    # logger 名称颜色池（用于区分不同模块）
    NAME_COLORS = [
        "\033[34m",  # 蓝色
        "\033[35m",  # 紫色
        "\033[36m",  # 青色
        "\033[32m",  # 绿色
        "\033[33m",  # 黄色
        "\033[94m",  # 亮蓝
        "\033[95m",  # 亮紫
        "\033[96m",  # 亮青
    ]
    DIM = "\033[2m"
    RESET = "\033[0m"
    BRIGHT = "\033[1m"

    def __init__(
        self,
        fmt: str | None = None,
        datefmt: str | None = None,
        style: str = "%",
        validate: bool = True,
        *,
        defaults: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(fmt, datefmt, style, validate, defaults=defaults)  # type: ignore[arg-type]
        self._name_color_cache: dict[str, str] = {}

    @staticmethod
    def _short_name(name: str, width: int = 22) -> str:
        """智能缩短 logger 名称，保留辨识度最高的部分。"""
        if len(name) <= width:
            return name
        parts = name.split(".")
        # 尝试保留最后两段
        short = ".".join(parts[-2:])
        if len(short) <= width:
            return short
        # 仍太长，只保留最后一段
        short = parts[-1]
        if len(short) <= width:
            return short
        return short[: width - 1] + "…"

    def _name_color(self, name: str) -> str:
        """为 logger 名称分配一个稳定的颜色。"""
        if name not in self._name_color_cache:
            idx = hash(name) % len(self.NAME_COLORS)
            self._name_color_cache[name] = self.NAME_COLORS[idx]
        return self._name_color_cache[name]

    def format(self, record: logging.LogRecord) -> str:
        level_width = 8
        name_width = 26
        gap = "  "

        # logger 名称 + 行号
        display_name = self._short_name(record.name, name_width - 4) + ":" + str(record.lineno)
        if len(display_name) > name_width:
            display_name = display_name[: name_width - 1] + "…"

        # 前缀长度（无颜色时的纯文本长度，用于异常缩进对齐）
        prefix_len = 8 + len(gap) + level_width + len(gap) + name_width + len(gap)

        # 时间（dim）
        time_str = f"{self.DIM}{self.formatTime(record, '%H:%M:%S')}{self.RESET}"

        # 级别（彩色）
        level_color, level_reset = self.LEVEL_STYLES.get(record.levelname, ("", ""))
        level_str = f"{level_color}{record.levelname:<{level_width}}{level_reset}"

        # logger 名称（彩色）
        name_color = self._name_color(record.name)
        name_str = f"{name_color}{display_name:<{name_width}}{self.RESET}"

        # 消息
        msg = record.getMessage()

        # 异常信息
        exc = ""
        if record.exc_info and record.exc_info[0] is not None:
            exc = "\n" + self.formatException(record.exc_info)
            exc = exc.replace("\n", "\n" + " " * prefix_len)

        # 组装
        result = f"{time_str}{gap}{level_str}{gap}{name_str}{gap}{msg}{exc}"

        # 额外信息
        if hasattr(record, "task") or hasattr(record, "user_id"):
            extra_parts = []
            if hasattr(record, "task"):
                extra_parts.append(f"task={record.task}")
            if hasattr(record, "user_id"):
                extra_parts.append(f"user={record.user_id}")
            if extra_parts:
                result += f" {self.DIM}({', '.join(extra_parts)}){self.RESET}"

        return result


class PlainFormatter(logging.Formatter):
    """纯文本格式化器，用于日志文件（无颜色代码）"""

    @staticmethod
    def _short_name(name: str, width: int = 22) -> str:
        """智能缩短 logger 名称。"""
        if len(name) <= width:
            return name
        parts = name.split(".")
        short = ".".join(parts[-2:])
        if len(short) <= width:
            return short
        short = parts[-1]
        if len(short) <= width:
            return short
        return short[: width - 1] + "…"

    def format(self, record: logging.LogRecord) -> str:
        level_width = 8
        name_width = 26
        gap = "  "
        display_name = self._short_name(record.name, name_width - 4) + ":" + str(record.lineno)
        if len(display_name) > name_width:
            display_name = display_name[: name_width - 1] + "…"
        prefix_len = 8 + len(gap) + level_width + len(gap) + name_width + len(gap)
        time_str = self.formatTime(record, "%Y-%m-%d %H:%M:%S")
        level_str = record.levelname.ljust(level_width)
        name_str = display_name.ljust(name_width)
        msg = record.getMessage()
        exc = ""
        if record.exc_info and record.exc_info[0] is not None:
            exc = "\n" + self.formatException(record.exc_info)
            exc = exc.replace("\n", "\n" + " " * prefix_len)
        result = f"{time_str}{gap}{level_str}{gap}{name_str}{gap}{msg}{exc}"
        if hasattr(record, "task") or hasattr(record, "user_id"):
            extra_parts = []
            if hasattr(record, "task"):
                extra_parts.append(f"task={record.task}")
            if hasattr(record, "user_id"):
                extra_parts.append(f"user={record.user_id}")
            if extra_parts:
                result += f" ({', '.join(extra_parts)})"
        return result


class LoggerNamePrefixFilter(logging.Filter):
    """Allow only records whose logger name starts with one of the prefixes."""

    def __init__(self, prefixes: Sequence[str]) -> None:
        super().__init__()
        self.prefixes = tuple(prefixes)

    def filter(self, record: logging.LogRecord) -> bool:
        return any(record.name == prefix or record.name.startswith(prefix) for prefix in self.prefixes)


# 默认需要上调到 WARNING 的第三方库 logger 名称
_DEFAULT_THIRD_PARTY_LOGGERS = [
    "websockets",
    "aiohttp",
    "aiohttp.access",
    "aiohttp.client",
    "aiohttp.web",
    "urllib3",
    "urllib3.connectionpool",
    "httpx",
    "asyncio",
    "watchdog",
    "watchdog.observers",
    "watchdog.observers.inotify_buffer",
    "fsspec",
    "PIL",
]


def configure_logging(
    *,
    level: LogLevel = "INFO",
    format_type: LogFormat = "console",
    log_file: Path | str | None = None,
    enable_file_rotation: bool = False,
    model_calls_log_file: Path | str | None = None,
    third_party_level: LogLevel = "WARNING",
) -> None:
    """
    配置全局日志系统

    Args:
        level: 日志级别，可选值：DEBUG/INFO/WARNING/ERROR/CRITICAL
        format_type: 输出格式，可选值：console/json
        log_file: 可选的日志文件路径（若指定则同时输出到文件）
        enable_file_rotation: 是否启用日志文件循环（每日轮换）
        model_calls_log_file: 可选的模型调用日志文件路径（独立的专用日志）
        third_party_level: 第三方库日志级别上调到该值，默认 WARNING

    Example:
        ```python
        # 控制台输出（开发环境）
        configure_logging(level="DEBUG", format_type="console")

        # JSON输出到文件（生产环境）
        configure_logging(
            level="INFO",
            format_type="json",
            log_file="logs/app.log",
            enable_file_rotation=True,
            model_calls_log_file="logs/model_calls.log"
        )
        ```
    """
    # 获取根logger
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, level))

    # 清除已有的处理器
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    # 上调第三方库日志级别，避免 INFO/DEBUG 噪音
    third_party_levelno = getattr(logging, third_party_level)
    for logger_name in _DEFAULT_THIRD_PARTY_LOGGERS:
        tp_logger = logging.getLogger(logger_name)
        # 如果当前级别比目标更宽松（数值更小），则上调
        if tp_logger.level < third_party_levelno or tp_logger.level == 0:
            tp_logger.setLevel(third_party_levelno)

    # Console处理器
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(getattr(logging, level))

    if format_type == "json":
        console_handler.setFormatter(JSONFormatter())
    else:
        console_handler.setFormatter(ColoredFormatter())

    root_logger.addHandler(console_handler)

    # 主日志文件处理器（可选）
    if log_file:
        log_path = Path(log_file)
        log_path.parent.mkdir(parents=True, exist_ok=True)

        if enable_file_rotation:
            # 每日轮换，保留7个备份，实时刷新
            file_handler = FlushingTimedRotatingFileHandler(
                log_path,
                when="midnight",
                interval=1,
                backupCount=7,
                encoding="utf-8",
            )
        else:
            # 实时刷新的文件处理器
            file_handler = FlushingFileHandler(log_path, encoding="utf-8")  # type: ignore[assignment]

        file_handler.setLevel(getattr(logging, level))
        # 文件处理器使用纯文本格式化器（无颜色代码）
        if format_type == "json":
            file_handler.setFormatter(JSONFormatter())
        else:
            file_handler.setFormatter(PlainFormatter())
        root_logger.addHandler(file_handler)

    # 模型调用日志处理器（可选，专用日志文件）
    if model_calls_log_file:
        model_log_path = Path(model_calls_log_file)
        model_log_path.parent.mkdir(parents=True, exist_ok=True)

        # 为模型调用日志创建独立的处理器
        model_handler = FlushingFileHandler(model_log_path, encoding="utf-8")
        model_handler.setLevel(getattr(logging, "INFO"))
        # 文件处理器使用纯文本格式化器（无颜色代码）
        if format_type == "json":
            model_handler.setFormatter(JSONFormatter())
        else:
            model_handler.setFormatter(PlainFormatter())

        # 只处理 provider 相关的日志
        model_logger = logging.getLogger("sirius_pulse.providers")
        model_logger.addHandler(model_handler)
        model_logger.setLevel(getattr(logging, "INFO"))


def add_filtered_file_handler(
    log_file: Path | str,
    *,
    logger_prefixes: Sequence[str],
    level: LogLevel = "INFO",
    format_type: LogFormat = "console",
) -> logging.Handler:
    """Attach a flushing file handler that only writes selected logger prefixes."""
    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    handler = FlushingFileHandler(log_path, encoding="utf-8")
    handler.setLevel(getattr(logging, level))
    handler.addFilter(LoggerNamePrefixFilter(logger_prefixes))
    if format_type == "json":
        handler.setFormatter(JSONFormatter())
    else:
        handler.setFormatter(PlainFormatter())

    logging.getLogger().addHandler(handler)
    return handler


def get_logger(name: str) -> logging.Logger:
    """获取指定名称的logger实例"""
    return logging.getLogger(name)


# 便捷导出
__all__ = [
    "configure_logging",
    "add_filtered_file_handler",
    "setup_log_archival",
    "get_logger",
    "JSONFormatter",
    "PlainFormatter",
    "ColoredFormatter",
    "LoggerNamePrefixFilter",
    "LogLevel",
    "LogFormat",
]
