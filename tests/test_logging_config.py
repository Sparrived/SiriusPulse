"""Tests for logging configuration helpers."""

from __future__ import annotations

import json
import logging

import pytest

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
