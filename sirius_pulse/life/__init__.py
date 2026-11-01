"""Her life: a sleep schedule, a daily plan, long-running projects, and a short
self-description that she revises each night.

This package is pure domain logic: it depends on neither ``core`` nor ``tools``,
so the engine (chat side) and the autonomy ticker (tool side) can both use it.
All data lives under ``memory/life/`` in the persona's work path.
"""

from __future__ import annotations

from pathlib import Path

from sirius_pulse.life.clock import DaySchedule
from sirius_pulse.utils.layout import WorkspaceLayout

__all__ = ["DaySchedule", "life_dir", "schedule_from_config"]

#: Config keys on the ``autonomy`` tool that hold her schedule.
WAKE_TIME_KEY = "wake_time"
SLEEP_TIME_KEY = "sleep_time"


def life_dir(work_path: Path | str) -> Path:
    """Where her life data lives: ``<work_path>/memory/life``."""
    return WorkspaceLayout(Path(work_path)).memory_dir() / "life"


def schedule_from_config(config: object) -> DaySchedule:
    """Build her schedule from a mapping holding ``wake_time`` / ``sleep_time``."""
    get = getattr(config, "get", None)
    if not callable(get):
        return DaySchedule()
    return DaySchedule.from_strings(get(WAKE_TIME_KEY), get(SLEEP_TIME_KEY))
