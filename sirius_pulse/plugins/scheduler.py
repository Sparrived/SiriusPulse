"""Plugin 定时调度器 —— 基于 cron 表达式和间隔秒数的定时触发。

支持：
    - cron 表达式：用于每日/每周等周期性事件
    - interval：固定间隔秒数

注意：完整 cron 解析需要安装 croniter 库。当前实现使用简化的分钟级轮询。
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Awaitable

logger = logging.getLogger(__name__)

# 定时任务连续失败时的退避策略
_MAX_CONSECUTIVE_FAILURES = 5          # 连续失败上限
_BACKOFF_BASE_SECONDS = 30             # 退避起始间隔（秒）


@dataclass
class ScheduledTask:
    """定时任务描述。"""

    name: str
    plugin_name: str
    cron: str = field(default="")                   # cron 表达式（简化支持）
    interval_seconds: float = field(default=0.0)    # 间隔秒数
    last_run: float = field(default=0.0)
    callback: Callable[[], Awaitable[None]] | None = field(default=None)
    consecutive_failures: int = field(default=0)    # 连续失败次数（用于退避）
    disabled: bool = field(default=False)           # 是否已停用


class PluginScheduler:
    """Plugin 定时调度器。

    使用 asyncio 事件循环周期性检查并触发到期的定时任务。
    """

    def __init__(self, check_interval: float = 10.0) -> None:
        self._tasks: list[ScheduledTask] = []
        self._check_interval = check_interval  # 检查粒度（秒）
        self._running = False
        self._task: asyncio.Task | None = None

    def add_task(self, task: ScheduledTask) -> None:
        """添加一个定时任务。"""
        self._tasks.append(task)
        logger.info("注册定时任务: %s（cron=%s, interval=%.1fs）", task.name, task.cron, task.interval_seconds)

    def remove_task(self, name: str) -> None:
        """移除一个定时任务。"""
        self._tasks = [t for t in self._tasks if t.name != name]

    def remove_plugin_tasks(self, plugin_name: str) -> None:
        """移除指定插件的所有定时任务。"""
        before = len(self._tasks)
        self._tasks = [t for t in self._tasks if t.plugin_name != plugin_name]
        removed = before - len(self._tasks)
        if removed > 0:
            logger.info("移除插件 %s 的 %d 个定时任务", plugin_name, removed)

    async def start(self) -> None:
        """启动调度器（后台循环）。"""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info("Plugin 定时调度器已启动，检查间隔 %.1fs", self._check_interval)

    async def stop(self) -> None:
        """停止调度器。"""
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        logger.info("Plugin 定时调度器已停止")

    async def _run_loop(self) -> None:
        """调度器主循环：周期性检查到期任务（带失败退避）。"""
        while self._running:
            now = time.time()
            for task in self._tasks:
                if task.disabled:
                    continue
                if not self._should_run(task, now):
                    continue
                if task.consecutive_failures > 0 and self._is_in_backoff(task, now):
                    continue

                task.last_run = now
                if task.callback:
                    try:
                        logger.debug("触发定时任务: %s", task.name)
                        await task.callback()
                        task.consecutive_failures = 0
                    except Exception as exc:
                        task.consecutive_failures += 1
                        if task.consecutive_failures >= _MAX_CONSECUTIVE_FAILURES:
                            task.disabled = True
                            logger.error(
                                "定时任务 %s 连续失败 %d 次，已自动停用",
                                task.name, task.consecutive_failures,
                            )
                        else:
                            logger.error(
                                "定时任务 %s 执行失败（第%d次）: %s",
                                task.name, task.consecutive_failures, exc,
                            )
            await asyncio.sleep(self._check_interval)

    def _is_in_backoff(self, task: ScheduledTask, now: float) -> bool:
        """检查任务是否处于退避期。"""
        if task.consecutive_failures <= 0:
            return False
        backoff = min(
            _BACKOFF_BASE_SECONDS * (2 ** (task.consecutive_failures - 1)),
            3600,  # 最大退避 1 小时
        )
        return (now - task.last_run) < backoff

    def _should_run(self, task: ScheduledTask, now: float) -> bool:
        """判断任务是否应该触发。"""
        if task.interval_seconds > 0:
            return (now - task.last_run) >= task.interval_seconds
        if task.cron:
            return self._check_simple_cron(task.cron, now, task.last_run)
        return False

    @staticmethod
    def _check_simple_cron(cron: str, now: float, last_run: float) -> bool:
        """简化 cron 检查（仅支持分钟级粒度）。

        支持格式：
            "* * * * *"（每分钟）
            "0 8 * * *"（每天 8:00）
            "*/5 * * * *"（每 5 分钟）
        """
        now_struct = time.localtime(now)
        last_struct = time.localtime(last_run) if last_run > 0 else None

        # 如果上次运行在同一分钟，跳过
        if last_struct and now_struct.tm_min == last_struct.tm_min and now_struct.tm_hour == last_struct.tm_hour and now_struct.tm_mday == last_struct.tm_mday:
            return False

        try:
            parts = cron.strip().split()
            if len(parts) != 5:
                return False

            minute, hour, day, month, weekday = parts

            # 检查分钟
            if minute != "*" and not _match_field(minute, now_struct.tm_min):
                return False
            # 检查小时
            if hour != "*" and not _match_field(hour, now_struct.tm_hour):
                return False
            # 检查日
            if day != "*" and not _match_field(day, now_struct.tm_mday):
                return False
            # 检查月
            if month != "*" and not _match_field(month, now_struct.tm_mon):
                return False
            # 检查星期
            if weekday != "*" and not _match_field(weekday, now_struct.tm_wday + 1):  # tm_wday: 0=Mon, cron: 0=Sun
                return False

            return True
        except Exception:
            return False


def _match_field(pattern: str, value: int) -> bool:
    """检查单个 cron 字段是否匹配。"""
    # */N 格式
    if pattern.startswith("*/"):
        try:
            step = int(pattern[2:])
            return value % step == 0
        except ValueError:
            return False
    # 逗号分隔
    if "," in pattern:
        return any(_match_field(p.strip(), value) for p in pattern.split(","))
    # 范围格式
    if "-" in pattern:
        try:
            lo, hi = pattern.split("-")
            return int(lo) <= value <= int(hi)
        except ValueError:
            return False
    # 精确值
    try:
        return int(pattern) == value
    except ValueError:
        return False
