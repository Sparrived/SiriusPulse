"""骰子机器人 Plugin —— 示例插件，使用 @command 装饰器模式。

通过 #roll <表达式> 触发，直接返回掷骰结果（direct 模式）。
支持标准 D&D 骰子表达式。
"""

from __future__ import annotations

import random
import re

from sirius_chat.plugins import PluginBase, PluginResult, command


class RollDicePlugin(PluginBase):
    """骰子机器人插件。

    使用 @command 装饰器声明式注册，type hint expression: str 自动注入。
    """

    _DICE_PATTERN = re.compile(
        r"(?P<count>\d+)?d(?P<sides>\d+)(?:k(?P<keep>\d+))?\s*(?P<mod>[+-]\s*\d+)?"
    )

    def on_load(self) -> None:
        self.logger.info("骰子插件已加载")

    @command(
        "roll_dice",
        patterns=["#roll", "/roll", "掷骰", "roll"],
        pattern_type="prefix",
        render_mode="direct",
        description="掷骰子",
        examples=["#roll 2d6+3", "/roll d20"],
    )
    def do_roll(self, expression: str) -> PluginResult:
        """掷骰子指令。

        Args:
            expression: 骰子表达式（自动从 CommandAST 注入）
        """
        if not expression:
            return PluginResult.fail("请指定骰子表达式，例如：#roll 2d6+3")

        result = self._roll(expression)
        if result is None:
            return PluginResult.fail(f"无效的骰子表达式: {expression}")

        return PluginResult.ok(text=result)

    def _roll(self, expression: str) -> str | None:
        """解析并掷骰子。"""
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

        rolls = [random.randint(1, sides) for _ in range(count)]
        if keep is not None:
            if keep > count:
                keep = count
            kept = sorted(rolls, reverse=True)[:keep]
            dropped = sorted(rolls)[: count - keep]
        else:
            kept = list(rolls)
            dropped = []

        total = sum(kept)
        mod = 0
        if mod_str:
            mod_str = mod_str.replace(" ", "")
            try:
                mod = int(mod_str)
            except ValueError:
                return None
            total += mod

        parts: list[str] = [f"🎲 {expression}"]
        if len(rolls) == 1:
            parts.append(f"= {rolls[0]}")
        else:
            parts.append(f"= [{', '.join(str(r) for r in rolls)}]")
            if dropped:
                parts.append(f"(取前{keep}个, 丢弃[{', '.join(str(r) for r in dropped)}])")
        if mod > 0:
            parts.append(f"+ {mod}")
        elif mod < 0:
            parts.append(f"- {abs(mod)}")
        parts.append(f"= {total}")

        if count == 1 and sides == 20:
            if rolls[0] == 20:
                parts.append(" ✨ 大成功！")
            elif rolls[0] == 1:
                parts.append(" 💀 大失败...")

        return " ".join(parts)
