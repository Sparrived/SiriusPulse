# Plugin 开发指南（一）：从零开始

本文档带你从零开始编写你的第一个 Plugin，了解 Plugin 的生命周期和基础概念。

---

## 1. 什么是 Plugin

Plugin 是 Sirius Pulse 的可扩展指令系统。一个 Plugin 就是一个继承自 `PluginBase` 的 Python 类，通过类属性和 `@command` 装饰器声明元数据与指令。

**Plugin 能做什么？**

- 响应用户的指令（如 `/天气 北京`、`#roll 2d6`）
- 调用外部 API（天气、翻译、搜索等）
- 通过平台适配器直接发送消息、图片、文件
- 管理群成员（踢人、禁言、设置名片）
- 使用 LLM 人格引擎生成风格化回复
- 读写持久化数据（PluginDataStore）
- 注册定时任务或被动事件触发器

---

## 2. 最小 Plugin 示例

创建一个目录 `plugins/my_first_plugin/`，包含一个 Python 文件：

### 2.1 `hello_plugin.py` —— 完整插件

```python
from sirius_pulse.plugins import PluginBase, PluginResponse, CommandAST
from sirius_pulse.plugins.decorators import command


class MyPlugin(PluginBase):
    _plugin_name = "my_first_plugin"
    _plugin_display_name = "我的第一个插件"
    _plugin_description = "一个简单的示例插件"

    @command("hello", patterns=["/hello", "你好"], render_mode="direct",
             description="打个招呼")
    def hello(self) -> PluginResponse:
        return PluginResponse.ok(text="你好！我是 Sirius Pulse 的插件！")
```

所有配置都在 Python 类中：类属性声明元数据，`@command` 装饰器声明指令。

---

## 3. Plugin 生命周期

每个 Plugin 经历以下生命周期阶段：

```
┌──────────┐
│  加载     │  on_load()  ← 扫描到 PluginBase 子类后调用一次
└────┬─────┘
     │
┌────▼─────┐
│ 执行上下文 │  注入 ctx (engine, adapter, message, data_store)
│  注入     │
└────┬─────┘
     │
┌────▼─────┐  ←── 每次用户触发指令时循环
│ execute() │   或 execute_async()
└────┬─────┘
     │
┌────▼─────┐
│  卸载     │  on_unload()  ← 插件被移除/程序退出时调用一次
└──────────┘
```

### 3.1 `on_load()` —— 初始化

```python
class MyPlugin(PluginBase):
    def on_load(self) -> None:
        # 启动时调用一次，适合初始化连接、加载配置等
        self.logger.info("MyPlugin 已加载")
```

### 3.2 `execute()` 或 `execute_async()` —— 核心逻辑

每次用户指令匹配到你的 Plugin 时调用：

```python
def execute(self, cmd: CommandAST) -> PluginResponse:
    # cmd.command: 指令名（如 "hello"）
    # cmd.raw_text: 原始文本（如 "/hello world"）
    # cmd.kwargs: 命名参数字典
    # cmd.args: 位置参数列表
    ...
```

### 3.3 `on_unload()` —— 清理

```python
def on_unload(self) -> None:
    # 关闭连接、释放资源
    self.logger.info("MyPlugin 已卸载")
```

---

## 4. PluginResponse —— 返回值契约

每当你处理完一条指令，必须返回一个 `PluginResponse`：

```python
# 成功，直接输出文本
return PluginResponse.ok(text="操作完成！")

# 成功，带结构化数据（让 LLM 做人格化生成）
return PluginResponse.ok(data={"city": "北京", "temp": 25})

# 失败
return PluginResponse.fail("参数错误：缺少城市名")

# 带多模态输出（图片）
from sirius_pulse.adapters import MessageGroup, image

return PluginResponse.ok(
    text="这是今天的天气图",
    message_group=MessageGroup([image("/tmp/weather.png")])
)
```

| 字段 | 说明 |
|------|------|
| `text` | 纯文本输出（`render_mode=direct` 时直接发送） |
| `data` | 结构化数据（`render_mode=llm` 时交给人格引擎风格化） |
| `error` | 错误信息 |
| `render_mode` | 覆写 @command 中声明的渲染模式 |
| `mood_hint` | 情绪提示（如 "温暖关心"、"严肃正式"） |
| `tone_override` | 语气覆写 |
| `message_group` | 多模态输出（图片/语音/文件） |

---

## 5. 下一步

你已经理解了 Plugin 的基本结构。接下来：

- **指南（二）**：`@command` 装饰器 —— 声明式指令注册
- **指南（三）**：PluginContext —— 引擎、适配器与数据存储
- **指南（四）**：多模态输出 —— 图片、语音、文件发送
- **指南（五）**：进阶话题 —— 权限、事件、定时任务
