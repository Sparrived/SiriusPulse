"""每日推送 Plugin —— 示例插件，展示 llm 渲染 + 定时事件模式。

通过 /daily 手动触发或每日定时自动推送。
通过 LLM 人格化生成早安消息。
"""

from __future__ import annotations

import random
from datetime import datetime

from sirius_chat.plugins import PluginBase, PluginResult, CommandAST


class DailyPushPlugin(PluginBase):
    """每日推送插件。

    演示 llm 渲染 + 事件触发 + 模板渲染的完整模式。
    """

    def on_load(self) -> None:
        self.logger.info("每日推送插件已加载")

    def execute(self, cmd: CommandAST) -> PluginResult:
        """生成每日推送内容。

        Args:
            cmd: 命令 AST

        Returns:
            PluginResult 包含推送数据结构
        """
        topic = cmd.get_str("topic", "daily")

        # 获取当前时间上下文
        now = datetime.now()
        hour = now.hour
        weekday = now.strftime("%A")
        date_str = now.strftime("%Y年%m月%d日")

        # 根据时间确定推送类型
        if hour < 10:
            greeting = "早安"
            time_context = "清晨"
        elif hour < 12:
            greeting = "上午好"
            time_context = "上午"
        elif hour < 14:
            greeting = "中午好"
            time_context = "中午"
        elif hour < 18:
            greeting = "下午好"
            time_context = "下午"
        else:
            greeting = "晚上好"
            time_context = "傍晚"

        # 随机选择一条语录或小知识
        tips = [
            "每天都是新的一天，保持好心情哦~",
            "记得按时吃早餐，身体最重要！",
            "今天也要元气满满地度过！",
            "生活不止眼前的代码，还有诗和远方~",
            "别忘了喝水，一天八杯水的目标！",
        ]

        push_data = {
            "greeting": greeting,
            "time_context": time_context,
            "date": date_str,
            "weekday": weekday,
            "topic": topic,
            "tip": random.choice(tips),
            "template_rendered": self.render_template("daily_default.txt", {
                "greeting": greeting,
                "date": date_str,
                "weekday": weekday,
                "tip": random.choice(tips),
            }) if self.source_path else "",
        }

        return PluginResult.ok(
            text=f"{greeting}！今天是{date_str}，{random.choice(tips)}",
            data=push_data,
            mood_hint="温暖、活泼、元气满满",
            render_mode="llm",
        )
