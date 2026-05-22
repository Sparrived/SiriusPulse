"""
日志配置模块的单元测试
"""

from __future__ import annotations

import json
import logging
import tempfile
from pathlib import Path

import pytest

from sirius_pulse.logging_config import (
    ColoredFormatter,
    JSONFormatter,
    configure_logging,
    get_logger,
)


def test_configure_logging_console_debug() -> None:
    """配置控制台DEBUG级别日志"""
    configure_logging(level="DEBUG", format_type="console")

    logger = get_logger(__name__)
    assert logger.level == logging.DEBUG or logging.root.level == logging.DEBUG


def test_configure_logging_console_info() -> None:
    """配置控制台INFO级别日志"""
    configure_logging(level="INFO", format_type="console")

    logger = logging.getLogger("test")
    assert logger.parent.level == logging.INFO or logging.root.level == logging.INFO


def test_configure_logging_with_json() -> None:
    """配置JSON格式日志"""
    configure_logging(level="INFO", format_type="json")

    logger = logging.getLogger("test_json")
    handlers = logging.root.handlers
    assert any(isinstance(h.formatter, JSONFormatter) for h in handlers)


def test_get_logger() -> None:
    """获取logger实例"""
    logger1 = get_logger("test.module")
    logger2 = get_logger("test.module")

    # 应该返回相同的logger实例
    assert logger1 is logger2


def test_json_formatter() -> None:
    """JSON格式化器测试"""
    formatter = JSONFormatter()
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="test.py",
        lineno=10,
        msg="Test message",
        args=(),
        exc_info=None,
    )

    formatted = formatter.format(record)

    # 应该能解析为JSON
    parsed = json.loads(formatted)
    assert parsed["level"] == "INFO"
    assert parsed["message"] == "Test message"
    assert parsed["logger"] == "test"


def test_json_formatter_with_extra_context() -> None:
    """JSON格式化器包含额外信息"""
    formatter = JSONFormatter()
    record = logging.LogRecord(
        name="test",
        level=logging.ERROR,
        pathname="test.py",
        lineno=20,
        msg="Error occurred",
        args=(),
        exc_info=None,
    )
    # 添加额外信息
    record.task = "memory_extract"  # type: ignore
    record.user_id = "user_123"  # type: ignore

    formatted = formatter.format(record)
    parsed = json.loads(formatted)

    assert parsed["task"] == "memory_extract"
    assert parsed["user_id"] == "user_123"


def test_colored_formatter() -> None:
    """彩色格式化器测试"""
    formatter = ColoredFormatter()
    record = logging.LogRecord(
        name="test",
        level=logging.INFO,
        pathname="test.py",
        lineno=10,
        msg="Test message",
        args=(),
        exc_info=None,
    )

    formatted = formatter.format(record)

    # 应该包含基本的日志信息
    assert "Test message" in formatted
    assert "test" in formatted.lower()


def test_configure_logging_with_file() -> None:
    """配置带文件输出的日志"""
    with tempfile.TemporaryDirectory() as tmpdir:
        log_file = Path(tmpdir) / "test.log"

        configure_logging(
            level="INFO",
            format_type="json",
            log_file=log_file,
        )

        logger = logging.getLogger("file_test")
        logger.info("Test file logging", extra={"test": "value"})

        # 验证文件已创建
        assert log_file.exists()

        # 验证文件内容
        content = log_file.read_text()
        assert len(content) > 0

        # 清理所有日志处理器避免文件锁定
        root_logger = logging.getLogger()
        for handler in root_logger.handlers[:]:
            handler.close()
            root_logger.removeHandler(handler)

        # 应该能解析为JSON
        parsed = json.loads(content.strip())
        assert parsed["message"] == "Test file logging"
        assert parsed["test"] == "value"


def test_logger_hierarchy() -> None:
    """logger命名空间层级测试"""
    configure_logging(level="INFO", format_type="console")

    parent_logger = get_logger("sirius_pulse")
    child_logger = get_logger("sirius_pulse.core")

    # 子logger应该继承父logger的配置
    assert child_logger.parent is logging.getLogger("sirius_pulse")
