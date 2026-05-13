# Sirius Chat Plugin 编写规范（AI 参考）

> 面向 AI 编码助手的精简参考。包含所有必需的代码模式、API 签名和项目约定。

---

## 1. 文件结构

```
plugins/<plugin_dir>/
└── *.py          # 任意 .py 文件，包含 PluginBase 子类即可
```

无需 `plugin.json`，所有元数据通过类属性声明。

---

## 2. 最小模板

```python
from sirius_chat.plugins import PluginBase, PluginResponse
from sirius_chat.plugins.decorators import command


class XxxPlugin(PluginBase):
    _plugin_name = "xxx"
    _plugin_display_name = "XXX"

    @command("xxx", patterns=["/xxx"], render_mode="direct")
    def xxx(self, arg1: str) -> PluginResponse:
        return PluginResponse.ok(text=f"结果: {arg1}")
```

---

## 3. 类属性速查

```python
class XxxPlugin(PluginBase):
    _plugin_name: str = ""                          # 必需（缺省用类名）
    _plugin_display_name: str = ""
    _plugin_description: str = ""
    _plugin_version: str = "1.0.0"
    _plugin_author: str = ""
    _plugin_events: list[dict] = []                 # [{"type":"timer.daily","cron":"0 8 * * *"}]
    _plugin_permissions: dict | None = None         # {"developer_only":False, "adapter_types":[], ...}
    _plugin_nl_examples: list[str] = []             # ["帮我查{city}的天气"]
    _plugin_nl_slots: dict[str, dict] = {}          # {"city": {"type":"str"}}
    _plugin_dependencies: list[str] = []            # ["requests>=2.28"]
```

---

## 4. @command 装饰器

```python
@command(
    name: str,                              # 必需，映射到 CommandAST.command
    *,
    prefix: str = "",                       # "/" | "#" | "!" 等，自动拼接到 patterns
    patterns: list[str] | None = None,      # 触发词列表（不含 prefix）
    pattern_type: str = "prefix",           # "prefix" | "keyword" | "regex"
    render_mode: str = "direct",            # "direct" | "llm" | "silent"
    description: str = "",
    examples: list[str] | None = None,
    system_prompt_suffix: str = "",         # LLM 模式追加 prompt
    max_tokens: int = 500,
    temperature: float = 0.8,
    mood_hint: str = "",                    # 情绪提示
)
```

### 方法参数 → 自动注入规则

| 类型注解 | 来源 | 示例 |
|---------|------|------|
| `name: str` | `cmd.kwargs["name"]` 或位置参数 | `/xxx foo` → `name="foo"` |
| `count: int` | 同上，自动 `int()` | `/xxx --count=5` → `count=5` |
| `verbose: bool` | 同上，`"true"/"1"/"yes"` | `/xxx --verbose` → `verbose=True` |
| `unit: str = "celsius"` | 同上，不传用默认值 | 可选参数 |

---

## 5. PluginResponse 构造

```python
# 成功，纯文本输出
PluginResponse.ok(text="Hello")

# 成功，LLM 人格化输出
PluginResponse.ok(data={"city": "北京", "temp": 25})

# 失败
PluginResponse.fail("缺少参数: city")

# 完整字段
PluginResponse(
    success=True,
    text="文本",                             # direct 模式直接发送
    data={"结构化数据"},                     # llm 模式交人格引擎
    error="",
    render_mode="llm",                      # 覆写 @command 中的设置
    mood_hint="温暖关心",
    tone_override="",
    message_group=MessageGroup([image("/tmp/x.png")]),  # 多模态
    metadata={},
)
```

---

## 6. ctx 完整 API

### 6.1 ctx.engine (EngineProxy)

```python
# 让引擎生成人格化文本
text = await self.ctx.engine.generate_text(
    prompt="请用幽默风格告诉用户结果",
    group_id=self.ctx.message.group_id,
)

name = self.ctx.engine.get_persona_name()
info = self.ctx.engine.get_persona_info()
self.ctx.engine.emit_event("event_type", {"key": "value"})
```

### 6.2 ctx.adapter (BaseAdapter)

```python
# 发送
await self.ctx.adapter.send_group_message(group_id, "text")
await self.ctx.adapter.send_group_message(group_id, MessageGroup([...]))
await self.ctx.adapter.send_private_message(user_id, "text")

# 群管理
await self.ctx.adapter.set_group_kick(group_id, user_id)
await self.ctx.adapter.set_group_ban(group_id, user_id, duration)
await self.ctx.adapter.set_group_admin(group_id, user_id, True)
await self.ctx.adapter.set_group_card(group_id, user_id, "新名片")
await self.ctx.adapter.set_group_name(group_id, "新群名")

# 群信息
await self.ctx.adapter.get_group_member_list(group_id)
await self.ctx.adapter.get_group_member_info(group_id, user_id)

# 消息操作
await self.ctx.adapter.delete_message(message_id)

# 文件
await self.ctx.adapter.upload_group_file(group_id, "/path/file", "name")
await self.ctx.adapter.upload_private_file(user_id, "/path/file", "name")

# 通用 API
await self.ctx.adapter.call_api("action", {"param": "value"})
```

### 6.3 ctx.message

```python
self.ctx.message.group_id        # str  群号（私聊为空）
self.ctx.message.user_id         # str  发送者 ID
self.ctx.message.channel_user_id # str  平台原生 ID（如 QQ 号）
self.ctx.message.content         # str  纯文本
self.ctx.message.speaker_name    # str  昵称
```

### 6.4 ctx.data_store

```python
store = self.ctx.data_store       # PluginDataStore | None
if store:
    store.set("key", "value")
    val = store.get("key", default="")
    store.delete("key")
    all_data = store.all()
```

### 6.5 ctx.logger

```python
self.ctx.logger.info(...)
self.ctx.logger.debug(...)
self.ctx.logger.warning(...)
```

---

## 7. MessageGroup 多模态

```python
from sirius_chat.adapters import (
    MessageGroup, text, at, image, voice, file, reply
)

# 文本
MessageGroup([text("你好")])

# @提及 + 文本 + 图片
MessageGroup([
    at("123456789"),
    text(" 天气如下："),
    image("/tmp/weather.png"),
])

# 带贴纸类型标记
image("/tmp/sticker.gif", sub_type="1")

# 语音
voice("/tmp/audio.amr")

# 文件
file("/tmp/report.pdf", name="月报.pdf")

# 回复引用
reply("msg_id_12345")
```

---

## 8. 流式输出（async generator）

```python
@command("search", prefix="/", patterns=["搜索"], render_mode="llm")
async def search(self, query: str):
    yield "正在搜索..."                          # 即时输出
    data = await self._do_search(query)
    yield PluginResponse.ok(data=data)           # 最终结果
```

---

## 9. 同步 handler

```python
@command("ping", prefix="/", patterns=["ping"])
def ping(self) -> PluginResponse:
    return PluginResponse.ok(text="pong!")
```

框架通过 `asyncio.to_thread()` 执行。

---

## 10. 模板渲染

```python
# templates/report.txt:
# === {title} ===
# 日期：{date}

rendered = self.render_template("report.txt", {"title": "日报", "date": "2025-01-15"})
```

---

## 11. 完整示例

```python
from sirius_chat.plugins import PluginBase, PluginResponse
from sirius_chat.plugins.decorators import command
from sirius_chat.adapters import MessageGroup, image


class WeatherPlugin(PluginBase):
    _plugin_name = "weather"
    _plugin_display_name = "天气查询"
    _plugin_description = "查询城市天气并生成图表"
    _plugin_events = [
        {"type": "timer.daily", "cron": "0 8 * * *", "description": "早间天气推送"},
    ]
    _plugin_permissions = {
        "developer_only": False,
        "rate_limit": {"calls_per_minute": 30, "calls_per_hour": 500},
    }
    _plugin_nl_examples = [
        "查{city}天气",
        "{city}今天天气怎么样",
    ]
    _plugin_nl_slots = {"city": {"type": "str"}}
    _plugin_dependencies = ["httpx>=0.24"]

    @command("weather", prefix="/", patterns=["天气", "weather"],
             render_mode="llm", description="查询城市天气",
             examples=["/天气 北京", "/weather Shanghai"])
    async def query_weather(self, city: str, unit: str = "celsius") -> PluginResponse:
        data = await self._fetch(city, unit)
        if data.get("error"):
            return PluginResponse.fail(data["error"])

        chart_path = await self._generate_chart(data)
        return PluginResponse.ok(
            data=data,
            render_mode="llm",
            mood_hint="温暖关心",
            message_group=MessageGroup([image(chart_path)]),
        )

    async def on_timer_daily(self) -> PluginResponse:
        city = self.ctx.data_store.get("default_city", "北京")
        data = await self._fetch(city)
        await self.ctx.adapter.send_group_message("123456", f"今日{city}天气: {data['summary']}")
        return PluginResponse.ok()

    async def _fetch(self, city: str, unit: str = "celsius"):
        import httpx
        async with httpx.AsyncClient() as client:
            resp = await client.get(f"https://api.weather.com/{city}?unit={unit}")
            return resp.json()

    async def _generate_chart(self, data: dict) -> str:
        # ...生成图表，返回文件路径
        return "/tmp/weather.png"
```
