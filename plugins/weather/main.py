"""天气查询 Plugin —— 查询指定城市的实时天气信息。

通过 /天气 <城市名> 触发，调用 Open-Meteo API 获取真实天气数据，
由引擎通过 LLM 做人格化风格生成。
"""
from __future__ import annotations

import json
from typing import Any
from urllib.parse import quote
from urllib.request import Request, urlopen

from sirius_pulse.plugins import PluginBase, PluginResponse, command

# ── Open-Meteo API 端点 ──
_GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
_WEATHER_URL = "https://api.open-meteo.com/v1/forecast"

# ── WMO 天气代码映射 ──
_WMO_CODES = {
    0: "晴", 1: "大部晴朗", 2: "局部多云", 3: "多云",
    45: "雾", 48: "雾凇",
    51: "毛毛雨", 53: "中度毛毛雨", 55: "强毛毛雨",
    56: "冻毛毛雨", 57: "强冻毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨",
    66: "冻雨", 67: "强冻雨",
    71: "小雪", 73: "中雪", 75: "大雪", 77: "雪粒",
    80: "小阵雨", 81: "中阵雨", 82: "强阵雨",
    85: "小阵雪", 86: "强阵雪",
    95: "雷雨", 96: "雷雨伴小冰雹", 99: "雷雨伴大冰雹",
}

# ── 风向角度 → 文字 ──
_WIND_DIRS = ["北", "东北", "东", "东南", "南", "西南", "西", "西北"]


def _wmo_desc(code: int | None) -> str:
    """将 WMO 天气代码转换为中文描述。"""
    if code is None:
        return "未知"
    return _WMO_CODES.get(code, f"天气代码 {code}")


def _fetch_json(url: str, timeout: int = 15) -> dict[str, Any]:
    """发送 GET 请求并返回 JSON 数据。"""
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json",
    }
    req = Request(url, headers=headers, method="GET")
    with urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


class WeatherPlugin(PluginBase):
    """天气查询插件。

    使用 @command 装饰器声明式注册指令，框架自动按 cmd.command 路由。
    方法参数的类型注解（city: str, extended: bool, forecast: bool）
    自动用于参数校验与注入。
    """

    # ── 插件元数据（类属性）──
    _plugin_name = "weather"
    _plugin_display_name = "天气查询"
    _plugin_description = "查询指定城市的实时天气，返回温度、天气状况、风速、湿度等信息。支持国内城市名或国际城市名。"
    _plugin_version = "1.0.0"
    _plugin_author = "sirius-chat"
    _plugin_nl_examples = [
        "帮我查一下{city}的天气",
        "{city}今天天气怎么样",
        "{city}天气预报",
    ]
    _plugin_nl_slots = {
        "city": {"type": "str", "description": "城市名称"},
    }
    _plugin_prompt_inject = (
        "查天气：群友可以让我查询任意城市的实时天气、温度、风速、湿度，"
        "以及未来几天的天气预报"
    )

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
        examples=["/天气 北京", "/weather Shanghai", "/天气 东京 --extended --forecast"],
        system_prompt_suffix="请以关心的语气告诉用户天气情况，提醒注意穿衣和出行。",
        max_tokens=400,
        temperature=0.8,
    )
    def query_weather(
        self,
        city: str,
        extended: bool = True,
        forecast: bool = False,
    ) -> PluginResponse:
        """查询指定城市的实时天气。

        Args:
            city: 城市名称，如北京、上海、广州、东京、纽约等
            extended: 是否返回扩展信息（体感温度、湿度、气压等），默认开启
            forecast: 是否返回未来3天天气预报，默认关闭
        """
        if not city or not isinstance(city, str):
            return PluginResponse.fail("请指定城市名称，例如：/天气 北京")

        city_clean = city.strip()

        # 1. 地理编码：城市名 → 经纬度
        try:
            geo_data = _fetch_json(
                f"{_GEO_URL}?name={quote(city_clean)}&count=1&language=zh&format=json"
            )
        except Exception as exc:
            return PluginResponse.fail(f"地理编码请求失败: {exc}")

        results = geo_data.get("results") or []
        if not results:
            return PluginResponse.fail(f"未找到城市「{city_clean}」，请检查城市名称")

        loc = results[0]
        lat = loc["latitude"]
        lon = loc["longitude"]
        loc_name = loc.get("name", city_clean)
        loc_country = loc.get("country", "")
        loc_admin1 = loc.get("admin1", "")

        # 组装显示名称
        display_name = loc_name
        if loc_admin1 and loc_admin1 != loc_name:
            display_name = f"{loc_admin1} {loc_name}"
        if loc_country and loc_country not in ("中国", "China"):
            display_name = f"{display_name} ({loc_country})"

        # 2. 请求天气数据
        weather_params = [
            f"latitude={lat}",
            f"longitude={lon}",
            "current_weather=true",
            "timezone=auto",
        ]
        if extended:
            weather_params.extend([
                "hourly=temperature_2m,relative_humidity_2m,apparent_temperature,surface_pressure,visibility",
                "daily=temperature_2m_max,temperature_2m_min,weathercode",
            ])
        if forecast:
            weather_params.extend([
                "daily=temperature_2m_max,temperature_2m_min,weathercode",
                "forecast_days=3",
            ])

        try:
            weather_data = _fetch_json(f"{_WEATHER_URL}?{'&'.join(weather_params)}")
        except Exception as exc:
            return PluginResponse.fail(f"天气数据请求失败: {exc}")

        current = weather_data.get("current_weather", {})
        temp = current.get("temperature")
        wmo = current.get("weathercode")
        wind_speed = current.get("windspeed")
        wind_dir = current.get("winddirection")
        is_day = current.get("is_day", 1)

        # 组装人类可读的文本
        lines: list[str] = []
        day_emoji = "☀️" if is_day else "🌙"
        lines.append(f"{day_emoji} 📍 {display_name} 天气")
        lines.append(f"🌤️ {_wmo_desc(wmo)}")
        if temp is not None:
            lines.append(f"🌡️ 当前温度: {temp}°C")

        # 扩展信息：取当前小时的数据
        if extended:
            hourly = weather_data.get("hourly", {})
            daily = weather_data.get("daily", {})
            current_time = current.get("time", "")
            h_times = hourly.get("time", [])
            h_idx = 0
            if h_times and current_time:
                for i, t in enumerate(h_times):
                    if t >= current_time:
                        h_idx = i
                        break

            if hourly.get("apparent_temperature"):
                feels = hourly["apparent_temperature"][h_idx]
                lines.append(f"🤒 体感温度: {feels}°C")
            if hourly.get("relative_humidity_2m"):
                hum = hourly["relative_humidity_2m"][h_idx]
                lines.append(f"💧 湿度: {hum}%")
            if wind_speed is not None:
                lines.append(f"💨 风速: {wind_speed} km/h")
            if wind_dir is not None:
                dir_idx = round(wind_dir / 45) % 8
                lines.append(f"🧭 风向: {_WIND_DIRS[dir_idx]} ({wind_dir}°)")
            if hourly.get("surface_pressure"):
                pressure = hourly["surface_pressure"][h_idx]
                lines.append(f"🌫️ 气压: {pressure} hPa")
            if hourly.get("visibility"):
                vis = hourly["visibility"][h_idx]
                lines.append(f"👁️ 能见度: {vis} km")

            # 今日最高最低温
            if daily.get("temperature_2m_max") and daily.get("temperature_2m_min"):
                lines.append(
                    f"📈 今日: {daily['temperature_2m_min'][0]}°C ~ {daily['temperature_2m_max'][0]}°C"
                )

        # 未来3天预报
        if forecast:
            daily = weather_data.get("daily", {})
            dates = daily.get("time", [])
            max_temps = daily.get("temperature_2m_max", [])
            min_temps = daily.get("temperature_2m_min", [])
            codes = daily.get("weathercode", [])
            if dates:
                lines.append("")
                lines.append("📅 未来天气预报:")
                for i in range(min(len(dates), 3)):
                    date = dates[i]
                    tmax = max_temps[i] if i < len(max_temps) else "—"
                    tmin = min_temps[i] if i < len(min_temps) else "—"
                    code = codes[i] if i < len(codes) else None
                    desc = _wmo_desc(code)
                    lines.append(f"  {date}: {desc} {tmin}°C ~ {tmax}°C")

        lines.append("")
        lines.append(f"📊 数据更新时间: {current.get('time', '—')}")

        text = "\n".join(lines)
        summary = f"{display_name} {_wmo_desc(wmo)} {temp}°C" if temp is not None else display_name

        return PluginResponse.ok(
            text=text,
            data={
                "city": display_name,
                "temperature": temp,
                "weather": _wmo_desc(wmo),
                "wind_speed": wind_speed,
                "wind_direction": wind_dir,
                "is_day": is_day,
                "raw": weather_data,
            },
            mood_hint="温暖、关心",
        )
