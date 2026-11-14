"""长期运行的协程异常退出后自动重启。

所有人格跑在同一个进程里：一个人格启动失败、一个后台循环抛了异常，都不该让它
从此悄无声息地停摆。这里统一做三件事：异常立刻带堆栈记进日志、按指数退避重启、
稳定跑过一段时间后把退避重置回初值。
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable

logger = logging.getLogger(__name__)


async def run_with_restarts(
    name: str,
    run_once: Callable[[], Awaitable[None]],
    *,
    should_continue: Callable[[], bool],
    stop_event: asyncio.Event | None = None,
    before_restart: Callable[[], None] | None = None,
    initial_delay: float = 5.0,
    max_delay: float = 300.0,
    healthy_after: float = 300.0,
) -> None:
    """反复运行 ``run_once``，直到它正常返回或 ``should_continue()`` 为假。

    Args:
        name: 日志里显示的名字。
        run_once: 跑一次的协程工厂；正常返回表示主动结束，不再重启。
        should_continue: 每次重启前检查；为假时不再重启（例如正在关停）。
        stop_event: 等待重启期间若被置位，立即放弃重启。
        before_restart: 重启前的准备（例如重建一个干净的 Worker 对象）。
        initial_delay: 第一次重启前等多久（秒），之后每次翻倍。
        max_delay: 退避上限（秒）。
        healthy_after: 一次运行超过这么久才失败，就把退避重置回 ``initial_delay``。
    """
    delay = initial_delay
    while should_continue():
        started = time.monotonic()
        try:
            await run_once()
            return
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("%s 异常退出", name)
        if not should_continue():
            return
        if time.monotonic() - started > healthy_after:
            delay = initial_delay
        logger.warning("%s 将在 %.0f 秒后重启", name, delay)
        if stop_event is not None:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=delay)
                return
            except TimeoutError:
                pass
        else:
            await asyncio.sleep(delay)
        if not should_continue():
            return
        delay = min(delay * 2, max_delay)
        if before_restart is not None:
            before_restart()
