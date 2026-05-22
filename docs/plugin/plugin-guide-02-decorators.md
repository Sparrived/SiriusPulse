# Plugin 开发指南（二）：`@command` 装饰器

在指南（一）中我们用传统的 `execute()` 覆写 + `if/elif` 分支来分发指令。
从 v1.2 开始，推荐使用 `@command` 装饰器——声明式、类型安全的指令注册方式。

---

## 1. 为什么用 `@command`

| 传统方式 (`execute` 覆写) | `@command` 装饰器 |
|---|---|
| 手动 `if/elif` 分支 | 自动按 `cmd.command` 路由 |
| 手动从 `cmd.kwargs` 取参 + 类型转换 | 类型注解自动校验 + 注入 |
| 所有指令塞在一个方法里 | 每个指令独立方法，单一职责 |
| 参数没有校验 | 缺少必填参数时报错 |

---

## 2. 第一个 `@command`

```python
from sirius_pulse.plugins import PluginBase, PluginResponse
from sirius_pulse.plugins.decorators import command


class WeatherPlugin(PluginBase):

    @command(
        "weather",
        prefix="/",                                  # 指令前缀
        patterns=["天气", "weather"],                 # 触发词
        render_mode="llm",                           # 使用 LLM 人格化输出
        description="查询城市天气",
        examples=["/天气 北京", "/weather Shanghai"]
    )
    async def query_weather(self, city: str, unit: str = "celsius") -> PluginResponse:
        """查询天气。

        参数 city 和 unit 从用户输入中自动提取。
        unit 有默认值 "celsius"，所以是可选的。
        """
        data = await self._call_weather_api(city, unit)
        return PluginResponse.ok(
            data=data,                               # LLM 会把这段数据转成自然语言
            mood_hint="温暖关心"
        )

    async def _call_weather_api(self, city: str, unit: str):
        # 你的 API 调用逻辑...
        return {"city": city, "temperature": 22, "unit": unit}
```

**用户输入**：
```
/天气 北京
/天气 北京 --unit=fahrenheit
```

框架自动：
1. 匹配 `prefix="/"` + `pattern="天气"` → 路由到 `query_weather`
2. `北京` → 注入参数 `city="北京"`
3. 不传 `--unit` 时 → 使用默认值 `"celsius"`

---

## 3. `@command` 参数详解

```python
@command(
    name,                           # 必填：指令名
    *,
    prefix="/",                     # 指令前缀（"/" "#" "!" 等）
    patterns=["天气", "weather"],    # 触发词列表（不含前缀）
    pattern_type="prefix",          # "prefix" | "keyword" | "regex"
    render_mode="direct",           # "direct" | "llm" | "silent"
    description="",                 # 指令描述
    examples=[],                    # 使用示例
    system_prompt_suffix="",        # LLM 模式追加的 system prompt
    max_tokens=500,                 # LLM 最大 token
    temperature=0.8,                # LLM 温度
    mood_hint="",                   # 情绪提示
)
```

### `prefix` 详解

```python
@command("ban", prefix="#", patterns=["禁言", "ban"])
async def ban_user(self, target: str, duration: int = 600) -> PluginResponse:
    ...
```

prefix 自动拼接到每个 pattern 前：
- `prefix="#"` + `patterns=["禁言"]` → 实际匹配 `#禁言`
- `prefix=""` → 直接匹配 patterns（无前缀要求）

### `pattern_type` 详解

```python
# prefix: 前缀匹配（默认）
@command("search", patterns=["/search", "/搜索"], pattern_type="prefix")
# 用户输入 "/搜索 北京" → 匹配

# keyword: 关键词包含匹配
@command("help", patterns=["帮助", "help"], pattern_type="keyword")
# 用户输入 "怎么使用帮助" → 匹配（因为包含"帮助"）

# regex: 正则匹配
@command("dice", patterns=[r"^\.r\b"], pattern_type="regex")
# 用户输入 ".r 2d6" → 匹配
```

---

## 4. 方法参数 —— 类型感知注入

框架根据方法的类型注解自动提取 + 校验参数：

| Python 类型注解 | 用户输入 | 注入行为 |
|---------------|---------|---------|
| `city: str` | `/天气 北京` | `city = "北京"` |
| `days: int` | `/预报 北京 --days=7` | `days = 7` |
| `verbose: bool` | `/搜索 北京 --verbose` | `verbose = True` |
| `unit: str = "celsius"` | 不传 `--unit` | `unit = "celsius"`（默认值） |

### 参数映射规则

用户输入中的命令行参数自动映射到方法参数：

```
用户输入: /天气 北京 --unit=celsius -v

CommandAST:
  cmd.command = "weather"
  cmd.args[0] = ArgNode(value="北京", raw="北京")
  cmd.kwargs = {
      "unit": ArgNode(value="celsius", raw="celsius"),
      "v": ArgNode(value=True, raw="-v")
  }

方法调用: query_weather(city="北京", unit="celsius")
```

---

## 5. 多个 `@command` 共存

一个 Plugin 可以有多个 `@command`，框架自动路由：

```python
class MyPlugin(PluginBase):

    @command("weather", prefix="/", patterns=["天气"])
    async def weather(self, city: str) -> PluginResponse:
        """查询天气"""
        ...

    @command("roll", prefix="#", patterns=["roll", "掷骰"])
    async def roll_dice(self, expression: str) -> PluginResponse:
        """掷骰子"""
        ...

    @command("ban", prefix="#", patterns=["禁言"])
    async def ban_user(self, target: str, duration: int = 600) -> PluginResponse:
        """禁言用户"""
        ...
```

不需要覆写 `execute()` 方法。

### 也支持同步方法

```python
@command("ping", prefix="/", patterns=["ping"])
def ping(self) -> PluginResponse:
    return PluginResponse.ok(text="pong!")
```

同步方法会通过 `asyncio.to_thread()` 在线程池中执行。

---

## 6. 流式输出

如果你的 handler 是一个 async generator，可以 `yield` 中间输出：

```python
@command("search", prefix="/", patterns=["搜索"], render_mode="llm")
async def search(self, query: str):
    """搜索并流式输出结果。"""
    yield "正在搜索 {query}..."         # 立即发送（框架自动转为 PluginResponse）

    results = await self._do_search(query)

    yield PluginResponse.ok(            # 最终结果，LLM 人格化
        data=results,
        render_mode="llm"
    )
```

每次 `yield` 都是独立的 PluginResponse，框架会依次处理。最后一个 `yield` 的结果决定最终回复。

---

## 7. 不需要 `plugin.json`

使用 `@command` 后，**不再需要 `plugin.json`**。框架自动从类属性和 `@command` 装饰器中读取所有配置。

---

## 8. 下一步

- **指南（三）**：PluginContext —— 引擎、适配器与数据存储
