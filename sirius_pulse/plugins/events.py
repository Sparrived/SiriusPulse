"""Plugin 事件系统 —— 定义 Plugin 可绑定的事件类型和事件对象。

事件类型：
    - timer.daily: 每日定时（cron 表达式）
    - timer.interval: 间隔触发（每 N 秒）
    - engine.started: 引擎启动
    - engine.stopped: 引擎停止
    - engine.group_joined: 新群加入
    - custom: 自定义事件
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any


class PluginEventType(enum.Enum):
    """Plugin 事件类型枚举。"""

    TIMER_DAILY = "timer.daily"
    TIMER_INTERVAL = "timer.interval"
    TIMER_ONE_TIME = "timer.one_time"
    ENGINE_STARTED = "engine.started"
    ENGINE_STOPPED = "engine.stopped"
    ENGINE_GROUP_JOINED = "engine.group_joined"
    CUSTOM = "custom"


@dataclass(slots=True)
class PluginEvent:
    """Plugin 事件对象。"""

    type: PluginEventType
    plugin_name: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: float = 0.0


@dataclass(slots=True)
class TimerEvent(PluginEvent):
    """定时器事件。"""

    cron: str = ""  # cron 表达式
    interval_seconds: float = 0.0  # 间隔秒数


@dataclass(slots=True)
class EngineEvent(PluginEvent):
    """引擎生命周期事件。"""

    group_id: str = ""
    user_id: str = ""
