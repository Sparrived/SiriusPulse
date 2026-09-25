"""Tests for logging configuration helpers."""

from __future__ import annotations

import ast
import json
import logging
from pathlib import Path

import pytest

from sirius_pulse.cli import PERSONA_LOGGER_PREFIXES, WEBUI_LOGGER_PREFIXES
from sirius_pulse.logging_config import (
    JSONFormatter,
    add_filtered_file_handler,
    configure_logging,
    setup_log_archival,
)


@pytest.fixture(autouse=True)
def restore_root_logging():
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    original_level = root.level

    yield

    for handler in root.handlers[:]:
        if handler not in original_handlers:
            root.removeHandler(handler)
            handler.close()
    for handler in original_handlers:
        if handler not in root.handlers:
            root.addHandler(handler)
    root.setLevel(original_level)


def test_json_formatter_when_record_has_extra_then_includes_context_fields():
    record = logging.LogRecord(
        name="sirius.test",
        level=logging.INFO,
        pathname=__file__,
        lineno=10,
        msg="hello %s",
        args=("world",),
        exc_info=None,
    )
    record.user_id = "u1"
    record.task = "unit-test"

    payload = json.loads(JSONFormatter().format(record))

    assert payload["level"] == "INFO"
    assert payload["logger"] == "sirius.test"
    assert payload["message"] == "hello world"
    assert payload["user_id"] == "u1"
    assert payload["task"] == "unit-test"


def test_configure_logging_when_json_file_is_enabled_then_writes_structured_line(tmp_path):
    log_file = tmp_path / "app.log"

    configure_logging(level="INFO", format_type="json", log_file=log_file)
    logger = logging.getLogger("sirius.test.file")
    logger.info("stored event", extra={"user_id": "u1"})

    payload = json.loads(log_file.read_text(encoding="utf-8").splitlines()[0])
    assert payload["message"] == "stored event"
    assert payload["user_id"] == "u1"


def test_add_filtered_file_handler_when_prefixes_are_configured_then_splits_records(tmp_path):
    configure_logging(level="INFO", format_type="console")
    persona_log = tmp_path / "persona.log"
    webui_log = tmp_path / "webui.log"

    add_filtered_file_handler(
        persona_log,
        logger_prefixes=("sirius.persona_worker", "core."),
        level="INFO",
    )
    add_filtered_file_handler(
        webui_log,
        logger_prefixes=("sirius.webui",),
        level="INFO",
    )

    logging.getLogger("sirius.persona_worker").info("persona ready")
    logging.getLogger("core.engine").info("engine ready")
    logging.getLogger("sirius.webui").info("webui ready")

    persona_text = persona_log.read_text(encoding="utf-8")
    webui_text = webui_log.read_text(encoding="utf-8")
    assert "persona ready" in persona_text
    assert "engine ready" in persona_text
    assert "webui ready" not in persona_text
    assert "webui ready" in webui_text
    assert "persona ready" not in webui_text


def test_setup_log_archival_when_log_exists_then_moves_old_content_to_archive(tmp_path):
    log_file = tmp_path / "app.log"
    log_file.write_text("old content", encoding="utf-8")

    setup_log_archival(log_file)

    archived = list((tmp_path / "archive").glob("app_*.log"))
    assert log_file.exists() is False
    assert len(archived) == 1
    assert archived[0].read_text(encoding="utf-8") == "old content"


_PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "sirius_pulse"

# 这两个 logger 不属于人格进程：``sirius.migrate`` 只在 CLI 迁移命令里用，
# ``sirius.main`` 系列的 WebUI/管理器日志由 WEBUI 前缀负责。前者落在控制台是
# 预期行为，不是漏配。
_PERSONA_PROCESS_EXEMPT = frozenset({"sirius.migrate"})


def _declared_logger_names() -> set[str]:
    """收集全量 ``logging.getLogger(...)`` 名称（``__name__`` 按模块路径展开）。"""
    names: set[str] = set()
    for path in _PACKAGE_ROOT.rglob("*.py"):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8-sig"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "getLogger"):
                continue
            if not node.args:
                continue
            arg = node.args[0]
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                names.add(arg.value)
            elif isinstance(arg, ast.Name) and arg.id == "__name__":
                names.add(".".join(path.relative_to(_PACKAGE_ROOT.parent).with_suffix("").parts))
    return names


def test_every_logger_in_the_persona_process_reaches_a_log_file():
    """每个模块 logger 都必须被某个文件处理器接住，否则失败会凭空消失。

    这条守护来自一次真实故障：``sirius_pulse.extension_runtime`` 不在任何人格
    日志前缀里，于是「Background task 'autonomy_tick' failed」整整两天只写进
    ``docker logs``，人格日志里一条都没有——自主行为什么都没做，却没有任何人
    能从那本日志里看出来。前缀表是人工维护的，所以这里核对全量名称。
    """
    prefixes = tuple(PERSONA_LOGGER_PREFIXES) + tuple(WEBUI_LOGGER_PREFIXES)

    missed = sorted(
        name
        for name in _declared_logger_names()
        if name not in _PERSONA_PROCESS_EXEMPT
        and not any(name == prefix or name.startswith(prefix) for prefix in prefixes)
    )

    assert missed == [], f"这些 logger 的日志不会落进任何日志文件: {missed}"


def test_a_failing_background_task_lands_in_the_persona_log(tmp_path):
    """守护的实际效果：后台任务异常要能在人格日志里被读到，含堆栈。"""
    configure_logging(level="INFO", format_type="console")
    persona_log = tmp_path / "persona.log"
    add_filtered_file_handler(
        persona_log,
        logger_prefixes=PERSONA_LOGGER_PREFIXES,
        level="INFO",
    )
    logger = logging.getLogger("sirius_pulse.extension_runtime")

    try:
        raise RuntimeError("heartbeat blew up")
    except RuntimeError:
        logger.exception("Background task '%s' failed", "autonomy_tick")

    text = persona_log.read_text(encoding="utf-8")
    assert "Background task 'autonomy_tick' failed" in text
    assert "RuntimeError: heartbeat blew up" in text
