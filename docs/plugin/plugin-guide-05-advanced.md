# Plugin 开发指南（五）：进阶话题

本文档涵盖权限控制、事件监听、定时任务、自然语言触发等进阶功能。

---

## 1. 权限控制

Plugin 可以通过类属性 `_plugin_permissions` 限制可调用者：

```python
class AdminPlugin(PluginBase):
    _plugin_permissions = {
        "developer_only": True,
        "adapter_types": ["napcat"],
        "group_whitelist": ["123456789"],
        "group_blacklist": ["987654321"],
        "user_whitelist": ["10001"],
        "rate_limit": {
            "calls_per_minute": 10,
            "calls_per_hour": 100
        }
    }
```

| 字段 | 说明 |
|------|------|
| `developer_only` | 仅开发者可用 |
| `adapter_types` | 限制平台（如 `["napcat"]`） |
| `group_whitelist` | 仅这些群可用 |
| `group_blacklist` | 这些群不可用 |
| `user_whitelist` | 仅这些用户可用 |
| `rate_limit.calls_per_minute` | 每分钟调用上限 |
| `rate_limit.calls_per_hour` | 每小时调用上限 |

---

## 2. 事件触发器

除了指令触发（`commands`），Plugin 还可以通过事件触发：

### 2.1 类属性配置

```python
class DailyReportPlugin(PluginBase):
    _plugin_events = [
        {"type": "timer.hourly", "description": "每小时整点执行一次"},
        {"type": "timer.daily", "cron": "0 8 * * *", "description": "每天早上 8 点执行"},
        {"type": "timer.interval", "interval_seconds": 300, "description": "每 5 分钟执行一次"},
        {"type": "engine.startup", "description": "引擎启动时执行"},
    ]
```

| 事件类型 | 说明 |
|---------|------|
| `timer.daily` | 每天定时（通过 `cron` 表达式指定） |
| `timer.hourly` | 每小时整点 |
| `timer.interval` | 按固定间隔 |
| `engine.startup` | 引擎启动时 |
| `webhook` | Webhook 触发 |

### 2.2 实现事件方法

在 Plugin 类中实现对应方法：

```python
class DailyReportPlugin(PluginBase):

    def on_timer_daily(self) -> PluginResponse:
        """每天早上 8 点自动生成日报。"""
        report = self._generate_daily_report()

        # 直接通过 adapter 发送到群
        import asyncio
        asyncio.create_task(
            self.ctx.adapter.send_group_message("123456789", report)
        )
        return PluginResponse.ok()

    def on_timer_interval(self) -> PluginResponse:
        """每 5 分钟检查一次。"""
        ...
        return PluginResponse.ok()
```

---

## 3. 自然语言触发

除了固定前缀（`/天气`）外，还可以通过自然语言匹配触发：

```python
class WeatherPlugin(PluginBase):
    _plugin_nl_examples = [
        "帮我查一下{city}的天气",
        "{city}今天天气怎么样",
        "我想知道{city}的温度"
    ]
    _plugin_nl_slots = {
        "city": {"type": "str", "description": "城市名"}
    }
```

当框架的 CognitionAnalyzer 检测到用户意图匹配 `natural_language.examples` 的语义时，会自动触发对应的 Plugin 指令。

---

## 4. 模板渲染

Plugin 支持 Jinja2 风格的简单模板（`{变量}` 替换）：

### 4.1 放置模板文件

在 Plugin 目录下创建 `templates/` 文件夹：

```
plugins/my_plugin/
├── hello_plugin.py
└── templates/
    └── report.txt
```

`templates/report.txt`：
```
=== {title} ===
日期：{date}
内容：{content}
```

### 4.2 使用模板

```python
@command("report", prefix="/", patterns=["报告"])
async def daily_report(self) -> PluginResponse:
    rendered = self.render_template("report.txt", {
        "title": "日报",
        "date": "2025-01-15",
        "content": "今日群活跃度 +30%",
    })
    return PluginResponse.ok(text=rendered)
```

---

## 5. 依赖声明

如果你的 Plugin 依赖第三方 Python 库：

```python
class MyPlugin(PluginBase):
    _plugin_dependencies = ["requests>=2.28", "pillow>=10.0"]
```

---

## 6. RenderMode（输出模式）详解

Plugin 有三种输出模式，控制回复的生成方式：

### 6.1 `direct` —— 直出文本

```python
@command("ping", render_mode="direct")
def ping(self) -> PluginResponse:
    return PluginResponse.ok(text="pong!")
```

最直接的模式。`PluginResponse.text` 直接发送给用户，不经过 LLM。

### 6.2 `llm` —— 人格引擎风格化

```python
@command("weather", render_mode="llm",
         system_prompt_suffix="请用地道的方言表达",
         mood_hint="温暖关心")
async def weather(self, city: str) -> PluginResponse:
    data = {"city": city, "temp": 25, "weather": "晴"}
    return PluginResponse.ok(data=data)
```

框架将 `data` 转化为 JSON，注入人格的 system prompt，由 LLM 生成自然的风格化回复。

### 6.3 `silent` —— 静默执行

```python
@command("log", render_mode="silent")
async def log_activity(self) -> PluginResponse:
    # 做点事情，但不输出任何内容
    self.ctx.data_store.set("last_active", time.time())
    return PluginResponse.ok()
```

无输出，仅执行副作用。适合后台操作、日志记录等。

### 6.4 动态覆写 render_mode

`PluginResponse.render_mode` 可以动态覆写 `@command` 中的设定：

```python
async def weather(self, city: str) -> PluginResponse:
    data = await self._fetch(city)

    if data["error"]:
        return PluginResponse.fail(data["error"])

    return PluginResponse.ok(
        data=data,
        render_mode="llm",      # 即使声明了 direct，这里也走 llm
        mood_hint="温暖关心"
    )
```

---

## 7. 完整的类属性参考

```python
class MyPlugin(PluginBase):
    # ── 元数据 ──
    _plugin_name = "my_plugin"              # 内部标识名（必需，缺省时用类名）
    _plugin_display_name = "我的插件"        # 显示名称
    _plugin_description = "插件描述"
    _plugin_version = "1.0.0"
    _plugin_author = "作者名"

    # ── 事件 ──
    _plugin_events = [
        {"type": "timer.daily", "cron": "0 8 * * *"}
    ]

    # ── 自然语言 ──
    _plugin_nl_examples = ["查{city}天气"]
    _plugin_nl_slots = {"city": {"type": "str"}}

    # ── 权限 ──
    _plugin_permissions = {
        "developer_only": False,
        "adapter_types": [],
        "group_whitelist": [],
        "group_blacklist": [],
        "user_whitelist": [],
        "rate_limit": {
            "calls_per_minute": 60,
            "calls_per_hour": 1000
        }
    }

    # ── 依赖 ──
    _plugin_dependencies = []

    # ── 指令（通过 @command 装饰器） ──
    @command("cmd1", patterns=["/cmd1"], render_mode="direct")
    def cmd1(self) -> PluginResponse: ...
```

---

## 8. 框架入口文件结构

一个完整的 Plugin 目录：

```
plugins/my_plugin/
├── hello_plugin.py      # 必需的 Python 文件（定义 PluginBase 子类）
├── templates/           # 可选：模板文件
│   └── report.txt
└── resources/           # 可选：静态资源
    └── font.ttf
```

---

## 9. 下一步

- 返回 **指南（一）** 复习 Plugin 生命周期
- 查看 `sirius_pulse/plugins/` 目录下的源码了解更多细节
