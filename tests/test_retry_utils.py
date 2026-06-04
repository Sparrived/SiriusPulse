"""Retry helper behavior tests."""

from __future__ import annotations

import pytest

from sirius_pulse.utils.retry import async_retry, sync_retry


def test_sync_retry_when_transient_error_then_retries_and_returns_value():
    attempts = 0

    def flaky() -> str:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise TimeoutError("try again")
        return "ok"

    assert sync_retry(flaky, max_retries=1, delay=0) == "ok"
    assert attempts == 2


def test_sync_retry_when_error_is_not_retryable_then_raises_without_retry():
    attempts = 0

    def broken() -> str:
        nonlocal attempts
        attempts += 1
        raise ValueError("bad input")

    with pytest.raises(ValueError):
        sync_retry(broken, max_retries=3, delay=0)

    assert attempts == 1


@pytest.mark.asyncio
async def test_async_retry_when_before_retry_updates_state_then_next_attempt_uses_it():
    value = "first"
    seen_values: list[str] = []

    async def flaky() -> str:
        seen_values.append(value)
        if len(seen_values) == 1:
            raise ConnectionError("temporary")
        return value

    def prepare_next_attempt(_attempt: int, _total: int, _exc: Exception) -> None:
        nonlocal value
        value = "second"

    result = await async_retry(
        flaky,
        max_retries=1,
        delay=0,
        before_retry=prepare_next_attempt,
    )

    assert result == "second"
    assert seen_values == ["first", "second"]
