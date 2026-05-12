"""天气查询 Plugin —— 示例插件，展示 llm 渲染模式。

通过 /天气 <城市名> 触发，返回结构化天气数据，
由引擎通过 LLM 做人格化风格生成。

由于是示例插件，使用模拟数据，实际使用时可替换为真实天气 API。
"""

from __future__ import annotations

import random
from typing import Any

from sirius_chat.plugins import PluginBase, PluginContext, PluginResult, CommandAST


class WeatherPlugin(PluginBase):
    """天气查询插件。

    演示 llm 渲染模式：Plugin 返回结构化数据，
    由 OutputDispatcher 委托引擎做人格化生成。
    """

    def on_load(self) -> None:
        """加载时的初始化操作。"""
        self.logger.info("天气插件已加载")

    def on_unload(self) -> None:
        """卸载时的清理操作。"""
        self.logger.info("天气插件已卸载")

    def execute(self, cmd: CommandAST) -> PluginResult:
        """查询城市天气。

        Args:
            cmd: 包含 city 参数的 CommandAST

        Returns:
            PluginResult 包含结构化天气数据
        """
        city = cmd.get_str("city", "")
        if not city:
            return PluginResult.fail("请指定城市名称，例如：/天气 北京")

        # 模拟天气数据（实际使用时可替换为真实 API 调用）
        weather_data = self._get_mock_weather(city)

        self.logger.info("查询 %s 天气: %s", city, weather_data)

        return PluginResult.ok(
            text=f"{city}天气：{weather_data['weather']}，{weather_data['temperature']}",
            data=weather_data,
            mood_hint="关心、温暖",  # 传递给 LLM 风格化生成的情绪提示
        )

    @staticmethod
    def _get_mock_weather(city: str) -> dict[str, Any]:
        """生成模拟天气数据。

        实际使用时替换为调用天气 API（如和风天气、OpenWeatherMap 等）。
        """
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
