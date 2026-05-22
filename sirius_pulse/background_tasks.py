"""
后台任务管理器 - 用于管理异步定时任务（内存压缩、数据清理、记忆归纳等）

特点：
- 轻量级（不依赖APScheduler）
- 基于asyncio的异步实现
- 支持优雅关闭
- 可配置的触发间隔
- 支持同步和异步回调
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import logging
from typing import Callable, Optional, Awaitable

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class BackgroundTaskConfig:
    """后台任务配置"""
    
    # 内存压缩配置
    compression_enabled: bool = True
    compression_interval_seconds: int = 3600  # 1小时
    compression_min_facts: int = 60  # 超过60个facts时触发
    compression_similarity_threshold: float = 0.8
    
    # 临时数据清理配置
    cleanup_enabled: bool = True
    cleanup_interval_seconds: int = 1800  # 30分钟
    cleanup_transient_max_age_minutes: int = 30
    
    # 记忆归纳配置
    consolidation_interval_seconds: int = 900 # 15分钟
    consolidation_min_entries: int = 6  # 最少条目数才触发归纳
    consolidation_min_notes: int = 4   # 最少摘要数才触发归纳
    consolidation_min_facts: int = 15  # 最少事实数才触发归纳

    # AI 自身记忆提取配置（日记 + 名词）
    self_memory_enabled: bool = False
    self_memory_interval_seconds: int = 360  # 默认 6 分钟

    # 是否启用日志
    verbose_logging: bool = False


class BackgroundTaskManager:
    """
    轻量级后台任务管理器
    
    用于运行异步定时任务，如内存压缩、数据清理、记忆归纳等。
    基于asyncio.create_task，不引入额外依赖。
    """
    
    def __init__(
        self,
        config: BackgroundTaskConfig | None = None,
    ):
        self.config = config or BackgroundTaskConfig()
        self.tasks: dict[str, asyncio.Task] = {}
        self._running = False
        self._memory_compressor_callback: Optional[Callable[[str], None]] = None
        self._transient_cleanup_callback: Optional[Callable[[str], None]] = None
        self._consolidation_callback: Optional[Callable[[], Awaitable[None]]] = None
        self._self_memory_callback: Optional[Callable[[], Awaitable[None]]] = None
    
    def set_memory_compressor_callback(
        self, 
        callback: Callable[[str], None]
    ) -> None:
        """设置内存压缩回调函数。
        
        回调函数签名: callback(user_id: str) -> None
        """
        self._memory_compressor_callback = callback
    
    def set_transient_cleanup_callback(
        self,
        callback: Callable[[str], None]
    ) -> None:
        """设置临时数据清理回调函数。
        
        回调函数签名: callback(user_id: str) -> None
        """
        self._transient_cleanup_callback = callback

    def set_consolidation_callback(
        self,
        callback: Callable[[], Awaitable[None]],
    ) -> None:
        """设置记忆归纳回调函数（异步）。

        回调函数签名: async callback() -> None
        该回调应负责调用 SemanticMemoryManager 或 DiaryManager 的归纳逻辑。
        """
        self._consolidation_callback = callback

    def set_self_memory_callback(
        self,
        callback: Callable[[], Awaitable[None]],
    ) -> None:
        """设置 AI 自身记忆提取回调函数（异步）。

        回调函数签名: async callback() -> None
        引擎在每个时间间隔结束后自动调用此回调，提取日记条目和名词解释。
        """
        self._self_memory_callback = callback
    
    async def start(self) -> None:
        """启动所有启用的后台任务"""
        if self._running:
            logger.warning("BackgroundTaskManager already running")
            return
        
        self._running = True
        logger.info("嗯... 后台的记忆整理小管家开始工作了，我会悄悄整理一下脑海里的东西。")
        
        if self.config.compression_enabled:
            task = asyncio.create_task(
                self._memory_compression_loop(),
                name="memory_compression"
            )
            self.tasks["memory_compression"] = task
        
        if self.config.cleanup_enabled:
            task = asyncio.create_task(
                self._transient_cleanup_loop(),
                name="transient_cleanup"
            )
            self.tasks["transient_cleanup"] = task

        task = asyncio.create_task(
            self._consolidation_loop(),
            name="memory_consolidation",
        )
        self.tasks["memory_consolidation"] = task

        if self.config.self_memory_enabled:
            task = asyncio.create_task(
                self._self_memory_loop(),
                name="self_memory_extract",
            )
            self.tasks["self_memory_extract"] = task
    
    async def stop(self) -> None:
        """停止所有后台任务"""
        if not self._running:
            return
        
        self._running = False
        logger.info("记忆整理任务先告一段落啦，小管家去休息一会儿～")
        
        # 取消所有任务
        for task_name, task in self.tasks.items():
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    logger.debug(f"Task {task_name} cancelled")
        
        self.tasks.clear()
    
    async def _memory_compression_loop(self) -> None:
        """内存压缩定时任务循环"""
        interval = self.config.compression_interval_seconds
        
        try:
            while self._running:
                await asyncio.sleep(interval)
                
                if not self._running:
                    break
                
                if self.config.verbose_logging:
                    logger.debug("Memory compression task triggered")
                
                if self._memory_compressor_callback:
                    try:
                        self._memory_compressor_callback("all_users")
                    except Exception as e:
                        logger.error(f"Error in memory compression: {e}", exc_info=True)
        
        except asyncio.CancelledError:
            logger.debug("Memory compression loop cancelled")
            raise
    
    async def _transient_cleanup_loop(self) -> None:
        """临时数据清理定时任务循环"""
        interval = self.config.cleanup_interval_seconds
        
        try:
            while self._running:
                await asyncio.sleep(interval)
                
                if not self._running:
                    break
                
                if self.config.verbose_logging:
                    logger.debug("Transient cleanup task triggered")
                
                if self._transient_cleanup_callback:
                    try:
                        self._transient_cleanup_callback("all_users")
                    except Exception as e:
                        logger.error(f"Error in transient cleanup: {e}", exc_info=True)
        
        except asyncio.CancelledError:
            logger.debug("Transient cleanup loop cancelled")
            raise

    async def _consolidation_loop(self) -> None:
        """记忆归纳定时任务循环"""
        interval = self.config.consolidation_interval_seconds

        try:
            while self._running:
                await asyncio.sleep(interval)

                if not self._running:
                    break

                if self.config.verbose_logging:
                    logger.debug("Memory consolidation task triggered")

                if self._consolidation_callback:
                    try:
                        await self._consolidation_callback()
                    except Exception as e:
                        logger.error(f"Error in memory consolidation: {e}", exc_info=True)

        except asyncio.CancelledError:
            logger.debug("Memory consolidation loop cancelled")
            raise

    async def _self_memory_loop(self) -> None:
        """AI 自身记忆定时提取任务循环（日记 + 名词）"""
        interval = self.config.self_memory_interval_seconds

        try:
            while self._running:
                await asyncio.sleep(interval)

                if not self._running:
                    break

                if self.config.verbose_logging:
                    logger.debug("Self-memory extraction task triggered")

                if self._self_memory_callback:
                    try:
                        await self._self_memory_callback()
                    except Exception as e:
                        logger.error(f"Error in self-memory extraction: {e}", exc_info=True)

        except asyncio.CancelledError:
            logger.debug("Self-memory extraction loop cancelled")
            raise
    
    async def trigger_compression_now(self, user_id: str = "all_users") -> None:
        """立即触发一次内存压缩"""
        if self._memory_compressor_callback:
            try:
                self._memory_compressor_callback(user_id)
            except Exception as e:
                logger.error(f"Error triggering compression: {e}", exc_info=True)
    
    async def trigger_cleanup_now(self, user_id: str = "all_users") -> None:
        """立即触发一次临时数据清理"""
        if self._transient_cleanup_callback:
            try:
                self._transient_cleanup_callback(user_id)
            except Exception as e:
                logger.error(f"Error triggering cleanup: {e}", exc_info=True)

    async def trigger_consolidation_now(self) -> None:
        """立即触发一次记忆归纳"""
        if self._consolidation_callback:
            try:
                await self._consolidation_callback()
            except Exception as e:
                logger.error(f"Error triggering consolidation: {e}", exc_info=True)

    async def trigger_self_memory_now(self) -> None:
        """立即触发一次 AI 自身记忆提取"""
        if self._self_memory_callback:
            try:
                await self._self_memory_callback()
            except Exception as e:
                logger.error(f"Error triggering self-memory extraction: {e}", exc_info=True)
    
    def is_running(self) -> bool:
        """检查后台任务是否在运行"""
        return self._running


__all__ = [
    "BackgroundTaskConfig",
    "BackgroundTaskManager",
]
