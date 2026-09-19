"""Runtime contracts shared by the Tool and Plugin extension systems."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

logger = logging.getLogger(__name__)

# How often a not-yet-started background task re-checks whether its host is up.
# Small enough that a starting engine feels immediate, large enough to be free
# while the engine is still being built.
_STARTUP_POLL_SECONDS = 0.1


@dataclass(slots=True)
class BackgroundTaskSpec:
    """Describe a framework-managed periodic extension task."""

    name: str
    interval_seconds: float
    task_func: Callable[..., Awaitable[None]]

    async def run_loop(self, running_check: Callable[[], bool]) -> None:
        """Run ``task_func`` periodically until ``running_check`` is false.

        The loop waits for the host to become ready before its first beat.
        Passive tools are registered while the engine is still being built —
        before ``running_check`` turns true — and the event loop starts running
        the freshly created task during that window.  Returning immediately on a
        false check would therefore kill the task permanently: nothing ever
        restarts it, so the tool silently never runs.
        """
        while not running_check():
            await asyncio.sleep(_STARTUP_POLL_SECONDS)
        while running_check():
            await asyncio.sleep(self.interval_seconds)
            if not running_check():
                break
            try:
                await self.task_func()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Background task '%s' failed", self.name)
