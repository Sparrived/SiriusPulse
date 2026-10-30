"""统一人物视觉资产、版本、授权与人物生图编译。

公共入口是 :class:`VisualService`；它只依赖 ``work_path``、人物领域服务与共享
SQLite 连接，不读取 CWD、不从模型参数里推断人物或权限。
"""

from __future__ import annotations

from sirius_pulse.visual.errors import CODES, VisualError
from sirius_pulse.visual.service import VisualService

__all__ = ["VisualService", "VisualError", "CODES"]
