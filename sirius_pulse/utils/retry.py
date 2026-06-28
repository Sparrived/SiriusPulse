"""通用重试工具。

提供统一的重试逻辑，消除各模块中重复的 for-attempt-try-except 模式。
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import time
from typing import Any, Callable, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


def is_transient_error(exc: Exception) -> bool:
    """判断异常是否为瞬时错误（值得重试）。"""
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True
    exc_name = type(exc).__name__.lower()
    return any(
        keyword in exc_name
        for keyword in ("timeout", "connection", "temporary", "network", "retry", "unreachable")
    )


def _log_retry(description: str, attempt: int, total: int, exc: Exception) -> None:
    logger.warning(
        "%s 失败 (attempt=%d/%d): %s",
        description,
        attempt,
        total,
        exc,
    )


async def async_retry(
    coro_factory: Callable[[], Any],
    *,
    max_retries: int = 0,
    delay: float = 1.0,
    should_retry: Callable[[Exception], bool] = is_transient_error,
    before_retry: Callable[[int, int, Exception], Any] | None = None,
    description: str = "操作",
    log_failures: bool = True,
) -> T:
    """通用异步重试工具。

    Args:
        coro_factory: 每次尝试时调用的协程工厂函数。
        max_retries: 最大重试次数（不含首次尝试）。
        delay: 重试间隔（秒）。
        should_retry: 判断异常是否值得重试的函数。
        before_retry: 可选 hook，在等待 delay 后、下一次尝试前执行。
        description: 日志描述。
        log_failures: 是否由工具统一记录失败日志。

    Returns:
        首次成功的结果。

    Raises:
        最后一次尝试的异常（如果所有重试均失败）。
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        attempt_no = attempt + 1
        total = max_retries + 1
        try:
            result = coro_factory()
            if inspect.isawaitable(result):
                return await result
            return result
        except Exception as exc:
            last_exc = exc
            if attempt < max_retries and should_retry(exc):
                if log_failures:
                    _log_retry(description, attempt_no, total, exc)
                if delay > 0:
                    await asyncio.sleep(delay)
                if before_retry is not None:
                    hook_result = before_retry(attempt_no, total, exc)
                    if inspect.isawaitable(hook_result):
                        await hook_result
                continue
            if log_failures:
                logger.error("%s 已耗尽 %d 次尝试: %s", description, total, exc)
            raise
    raise last_exc  # type: ignore[misc]


def sync_retry(
    func: Callable[[], T],
    *,
    max_retries: int = 0,
    delay: float = 0.0,
    should_retry: Callable[[Exception], bool] = is_transient_error,
    before_retry: Callable[[int, int, Exception], None] | None = None,
    description: str = "操作",
    log_failures: bool = True,
) -> T:
    """通用同步重试工具。"""
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        attempt_no = attempt + 1
        total = max_retries + 1
        try:
            return func()
        except Exception as exc:
            last_exc = exc
            if attempt < max_retries and should_retry(exc):
                if log_failures:
                    _log_retry(description, attempt_no, total, exc)
                if delay > 0:
                    time.sleep(delay)
                if before_retry is not None:
                    before_retry(attempt_no, total, exc)
                continue
            if log_failures:
                logger.error("%s 已耗尽 %d 次尝试: %s", description, total, exc)
            raise
    raise last_exc  # type: ignore[misc]
