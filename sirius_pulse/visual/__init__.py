"""统一人物视觉资产、版本、授权与人物生图编译，并提供多角色参考拼图工具。

公共入口是 :class:`VisualService`；它只依赖 ``work_path``、人物领域服务与共享
SQLite 连接，不读取 CWD、不从模型参数里推断人物或权限。拼图工具见
:mod:`sirius_pulse.visual.collage`，不参与授权判断，只负责把已知角色的参考图排成
少量拼图并生成位置说明。
"""

from __future__ import annotations

from sirius_pulse.visual.collage import (
    MAX_PEOPLE_PER_COLLAGE,
    MAX_REFERENCE_IMAGES,
    ReferenceCollage,
    build_reference_collages,
    reference_note,
)
from sirius_pulse.visual.errors import CODES, VisualError
from sirius_pulse.visual.service import VisualService

__all__ = [
    "CODES",
    "MAX_PEOPLE_PER_COLLAGE",
    "MAX_REFERENCE_IMAGES",
    "ReferenceCollage",
    "VisualError",
    "VisualService",
    "build_reference_collages",
    "reference_note",
]
