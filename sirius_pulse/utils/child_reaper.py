"""孤儿子进程回收。

容器内 ``sirius-pulse run`` 是 **PID 1**。内核把失去父进程的进程重新挂到 PID 1
名下，而 PID 1 只有在调用 ``waitpid()`` 时才会回收它们；不回收就永久停留在
``Z``（zombie）状态，持续占用 PID 表项。

线上实测：18 个 ``[chrome-headless] <defunct>``，父进程全部是 PID 1。它们来自
Playwright 启动 Chromium 时创建的中间进程——中间进程退出后，其浏览器子进程被
重新挂到 PID 1，而框架从未回收。

本模块用一个**轮询任务**（而不是 SIGCHLD 处理器）来回收，原因是不与 asyncio 的
child watcher 抢占 SIGCHLD，避免影响未来可能引入的 ``asyncio.create_subprocess_*``。
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys

logger = logging.getLogger(__name__)

#: 轮询间隔（秒）。回收僵尸只是清理内核表项，不需要高频。
DEFAULT_REAP_INTERVAL_SECONDS = 60.0


def reap_orphaned_children() -> list[int]:
    """回收所有已退出但尚未被 wait 的子进程。

    Returns:
        本次回收到的 PID 列表。没有可回收对象时返回空列表。

    Notes:
        使用 ``wnohang`` 循环到再无僵尸为止。``ChildProcessError`` 表示当前进程
        没有子进程，属正常终止条件；Windows 上没有僵尸概念，直接返回空列表。
    """
    if sys.platform == "win32":
        return []

    reaped: list[int] = []
    # Windows 无此常量；用 getattr 兜底，也让测试能在任意平台注入 waitpid。
    wnohang = getattr(os, "WNOHANG", 0)
    while True:
        try:
            pid, _status = os.waitpid(-1, wnohang)
        except ChildProcessError:
            break
        except OSError as exc:  # pragma: no cover - 内核/权限异常，记录后放弃本轮
            logger.warning("回收子进程失败: %s", exc)
            break
        if pid == 0:
            break
        reaped.append(pid)
    return reaped


async def child_reaper_loop(
    interval: float = DEFAULT_REAP_INTERVAL_SECONDS,
) -> None:
    """周期性回收孤儿子进程，直到被取消。"""
    while True:
        reaped = reap_orphaned_children()
        if reaped:
            logger.info("已回收 %d 个僵尸子进程: %s", len(reaped), reaped)
        await asyncio.sleep(interval)


def start_child_reaper(
    interval: float = DEFAULT_REAP_INTERVAL_SECONDS,
) -> asyncio.Task[None] | None:
    """在事件循环中启动孤儿回收任务。

    Returns:
        启动的任务；Windows 或当前进程并非 PID 1 且不是容器内主进程时返回 ``None``。
        PID 1 之外的场景不做回收，避免抢走其他代码正要 ``wait()`` 的子进程。
    """
    if sys.platform == "win32":
        return None
    if os.getpid() != 1:
        logger.debug("当前进程不是 PID 1，跳过孤儿回收任务")
        return None
    return asyncio.create_task(child_reaper_loop(interval), name="child-reaper")
