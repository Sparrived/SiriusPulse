"""天气查询 Plugin —— 示例插件，使用 @command 装饰器模式。

通过 /天气 <城市名> 触发，返回结构化天气数据，
由引擎通过 LLM 做人格化风格生成。
"""

from __future__ import annotations

import random
from typing import Any

from sirius_chat.plugins import PluginBase, PluginResult, CommandAST, command


class WeatherPlugin(PluginBase):
    """天气查询插件。

    使用 @command 装饰器声明式注册指令，框架自动按 cmd.command 路由。
    方法参数的类型注解（city: str）自动用于参数校验与注入。
    """

    def on_load(self) -> None:
        self.logger.info("天气插件已加载")

    def on_unload(self) -> None:
        self.logger.info("天气插件已卸载")

    @command(
        "weather",
        prefix="/",
        patterns=["天气", "weather"],
        pattern_type="prefix",
        render_mode="llm",
        description="查询城市天气",
        examples=["/天气 北京", "/weather Shanghai"],
        system_prompt_suffix="请以关心的语气告诉用户天气情况，提醒注意穿衣和出行。",
        max_tokens=300,
        temperature=0.8,
    )
    async def query_weather(self, city: str):
        """查询指定城市的天气（流式输出演示）。

        使用 async generator yield 实现渐进输出：
            1. yield "正在查询..." → 立即发送
            2. yield PluginResult.ok(data=...) → LLM 人格化生成

        Args:
            city: 城市名称（由框架从 CommandAST 自动注入）
        """
        if not city:
            yield PluginResult.fail("请指定城市名称，例如：/天气 北京")
            return

        # 第一步：立即告知用户正在查询
        yield f"🔍 正在查询 {city} 的天气..."

        # 模拟异步查询延迟
        import asyncio
        await asyncio.sleep(0.3)

        # 第二步：返回结构化数据让引擎做人格化生成
        weather_data = self._get_mock_weather(city)
        self.logger.info("查询 %s 天气: %s", city, weather_data)

        yield PluginResult.ok(
            text=f"{city}天气：{weather_data['weather']}，{weather_data['temperature']}",
            data=weather_data,
            mood_hint="温暖、关心",
        )

    @staticmethod
    def _get_mock_weather(city: str) -> dict[str, Any]:
        """生成模拟天气数据。"""
        conditions = ["晴", "多云", "阴", "小雨", "阵雨", "雷阵雨"]
        temps = list(range(5, 36))
        humidities = list(range(30, 95))

        return {
            "city": city,
            "weather": random.choice(conditions),
            "temperature": f"{random.choice(temps)}°C",
            "humidity": f"{random.choice(humidities)}%",
            "wind": f"{random.choice(['微风', '北风2级', '南风3级', '东风1级'])}",
            "aqi": random.choice(["优", "良", "轻度污染"]),
            "tips": [
                "紫外线较弱，无需特殊防护" if random.random() > 0.5 else "紫外线较强，注意防晒",
                "适宜户外活动" if random.random() > 0.3 else "建议携带雨具",
            ],
            "update_time": "2026-05-12 08:00",
        }
