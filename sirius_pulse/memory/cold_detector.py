"""两阶段冷检测器。

替代现有 HeatCalculator.is_cold() 的单一阈值逻辑。
- 暂冷（warm）：5 分钟无消息 → 触发情景压缩
- 冷寂（cold）：30 分钟无消息 → 触发日记总结
"""

from __future__ import annotations

import logging

from sirius_pulse.core.constants import COLD_HEAT_THRESHOLD

logger = logging.getLogger(__name__)

__all__ = ["ColdDetector", "ColdState"]


class ColdState:
    """冷检测状态常量。"""

    HOT = "hot"  # 活跃，不做任何处理
    WARM = "warm"  # 暂冷，触发情景压缩（Layer 2）
    COLD = "cold"  # 冷寂，触发日记总结（Layer 3）


class ColdDetector:
    """两阶段冷检测器。

    基于热度和静默时长判断群聊所处阶段：
    - HOT:  热度 >= 阈值，或静默 < 5 分钟
    - WARM: 热度 < 阈值，且静默 >= 5 分钟
    - COLD: 热度 < 阈值，且静默 >= 30 分钟

    与现有 HeatCalculator.calculate() 配合使用，
    不替代热度计算逻辑，只替代 is_cold() 的判断。
    """

    # 暂冷阈值：5 分钟
    WARM_SILENCE_SECONDS = 300

    # 冷寂阈值：30 分钟
    COLD_SILENCE_SECONDS = 1800

    @staticmethod
    def check(heat: float, seconds_since_last: float) -> str:
        """判断群聊所处阶段。

        Args:
            heat: 当前热度 [0.0, 1.0]，来自 HeatCalculator.calculate()
            seconds_since_last: 距最后一条消息的秒数

        Returns:
            ColdState.HOT / WARM / COLD
        """
        # 热度足够高 → 活跃
        if heat >= COLD_HEAT_THRESHOLD:
            return ColdState.HOT

        # 热度低，检查静默时长
        if seconds_since_last >= ColdDetector.COLD_SILENCE_SECONDS:
            return ColdState.COLD
        if seconds_since_last >= ColdDetector.WARM_SILENCE_SECONDS:
            return ColdState.WARM

        return ColdState.HOT

    @staticmethod
    def should_extract_situation(
        heat: float,
        seconds_since_last: float,
        candidate_count: int,
        min_candidates: int = 5,
    ) -> bool:
        """判断是否应该触发情景提取。

        条件：
        1. 处于 WARM 状态（暂冷）
        2. 候选消息数 >= 最小阈值
        """
        if ColdDetector.check(heat, seconds_since_last) != ColdState.WARM:
            return False
        if candidate_count < min_candidates:
            return False
        return True

    @staticmethod
    def should_generate_diary(
        heat: float,
        seconds_since_last: float,
        situation_count: int,
        min_situations: int = 1,
    ) -> bool:
        """判断是否应该触发日记生成。

        条件：
        1. 处于 COLD 状态（冷寂）
        2. 待处理的 Situation 数 >= 最小阈值
        """
        if ColdDetector.check(heat, seconds_since_last) != ColdState.COLD:
            return False
        if situation_count < min_situations:
            return False
        return True
