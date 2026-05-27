"""通用异步重试工具。

提供统一的重试逻辑，消除各模块中重复的 for-attempt-try-except 模式。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)


def _is_transient(exc: Exception) -> bool:
    """判断异常是否为瞬时错误（值得重试）。"""
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return True
    exc_name = type(exc).__name__.lower()
    return any(
        keyword in exc_name
        for keyword in ("timeout", "connection", "temporary", "network", "retry", "unreachable")
    )


async def async_retry(
    coro_factory: Callable[[], Any],
    *,
    max_retries: int = 0,
    delay: float = 1.0,
    should_retry: Callable[[Exception], bool] = _is_transient,
    description: str = "操作",
) -> Any:
    """通用异步重试工具。

    Args:
        coro_factory: 每次尝试时调用的协程工厂函数。
        max_retries: 最大重试次数（不含首次尝试）。
        delay: 重试间隔（秒）。
        should_retry: 判断异常是否值得重试的函数。
        description: 日志描述。

    Returns:
        首次成功的结果。

    Raises:
        最后一次尝试的异常（如果所有重试均失败）。
    """
    last_exc: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            return await coro_factory()
        except Exception as exc:
            last_exc = exc
            if attempt < max_retries and should_retry(exc):
                logger.warning(
                    "%s 失败 (attempt=%d/%d): %s",
                    description, attempt + 1, max_retries + 1, exc,
                )
                await asyncio.sleep(delay)
            else:
                logger.error(
                    "%s 已耗尽 %d 次重试: %s",
                    description, max_retries + 1, exc,
                )
                raise
    raise last_exc  # type: ignore[misc]
