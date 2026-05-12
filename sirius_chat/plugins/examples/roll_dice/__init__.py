"""骰子机器人 Plugin —— 示例插件，展示 direct 渲染模式。

通过 #roll <表达式> 触发，直接返回掷骰结果，
不经过 LLM 风格化（确定性结果，直接输出最快）。

支持标准 D&D 骰子表达式：
    - d20：掷一个 20 面骰子
    - 2d6+3：掷两个 6 面骰子，结果加 3
    - 3d8-1：掷三个 8 面骰子的和减 1
    - 4d6k3：掷四个 6 面骰子，取最大的三个（D&D 属性生成）
"""

from __future__ import annotations

import random
import re

from sirius_chat.plugins import PluginBase, PluginResult, CommandAST


class RollDicePlugin(PluginBase):
    """骰子机器人插件。

    演示 direct 渲染模式：Plugin 直接返回最终文本，
    零 LLM 成本，确定性输出。
    """

    # 骰子表达式正则：可选数量 + d + 面数 + 可选修正
    _DICE_PATTERN = re.compile(
        r"(?P<count>\d+)?d(?P<sides>\d+)(?:k(?P<keep>\d+))?\s*(?P<mod>[+-]\s*\d+)?"
    )

    def on_load(self) -> None:
        self.logger.info("骰子插件已加载")

    def execute(self, cmd: CommandAST) -> PluginResult:
        """掷骰子。

        Args:
            cmd: 包含 expression 参数的 CommandAST

        Returns:
            PluginResult 包含掷骰结果文本
        """
        expression = cmd.get_str("expression", "")
        if not expression:
            return PluginResult.fail("请指定骰子表达式，例如：#roll 2d6+3")

        result = self._roll(expression)
        if result is None:
            return PluginResult.fail(f"无效的骰子表达式: {expression}")

        return PluginResult.ok(text=result, render_mode="direct")

    def _roll(self, expression: str) -> str | None:
        """解析并掷骰子。

        Args:
            expression: 骰子表达式，如 "2d6+3"

        Returns:
            格式化的掷骰结果字符串，如 "🎲 2d6+3 = [4, 3] + 3 = 10"
        """
        # 清理表达式
        expr_clean = expression.strip().replace(" ", "")
        match = self._DICE_PATTERN.fullmatch(expr_clean)
        if match is None:
            return None

        count = int(match.group("count") or 1)
        sides = int(match.group("sides") or 6)
        keep = int(match.group("keep")) if match.group("keep") else None
        mod_str = match.group("mod")

        if count <= 0 or sides <= 0:
            return None
        if count > 100:
            return "一次最多掷 100 个骰子哦~"
        if sides > 1000:
            return "骰子面数不能超过 1000~"

        # 掷骰
        rolls = [random.randint(1, sides) for _ in range(count)]

        # 处理 k (keep highest)
        if keep is not None:
            if keep > count:
                keep = count
            kept = sorted(rolls, reverse=True)[:keep]
            dropped = sorted(rolls)[: count - keep]
        else:
            kept = list(rolls)
            dropped = []

        total = sum(kept)

        # 处理修正值
        mod = 0
        if mod_str:
            mod_str = mod_str.replace(" ", "")
            try:
                mod = int(mod_str)
            except ValueError:
                return None
            total += mod

        # 格式化输出
        parts: list[str] = []
        parts.append(f"🎲 {expression}")

        if len(rolls) == 1:
            parts.append(f"= {rolls[0]}")
        else:
            rolls_str = ", ".join(str(r) for r in rolls)
            parts.append(f"= [{rolls_str}]")
            if dropped:
                dropped_str = ", ".join(str(r) for r in dropped)
                parts.append(f"(取前{keep}个, 丢弃[{dropped_str}])")

        if mod > 0:
            parts.append(f"+ {mod}")
        elif mod < 0:
            parts.append(f"- {abs(mod)}")

        parts.append(f"= {total}")

        # 暴击/大失败提示（仅 d20 时）
        if count == 1 and sides == 20:
            if rolls[0] == 20:
                parts.append(" ✨ 大成功！")
            elif rolls[0] == 1:
                parts.append(" 💀 大失败...")

        return " ".join(parts)
