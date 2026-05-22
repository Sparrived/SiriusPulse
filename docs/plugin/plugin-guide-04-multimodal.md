# Plugin 开发指南（四）：多模态输出

Plugin 不仅可以发送文本，还可以发送图片、语音、文件，以及 @ 提及用户。
所有这些通过 `MessageGroup` 统一完成。

---

## 1. MessageGroup 是什么

`MessageGroup` 是一组有序的消息片段，跨平台统一表示。

```python
from sirius_pulse.adapters import (
    MessageGroup, text, at, image, voice, file, reply
)
```

每个 `MessageGroup` 由若干 `MessageSegment` 组成。每个适配器（NapCat / 未来的 Discord）负责将其转换为平台特定格式。

---

## 2. 构造消息片段

### 2.1 纯文本

```python
msg = MessageGroup([text("你好，这是你要的结果")])
await self.ctx.adapter.send_group_message(group_id, msg)
```

`MessageGroup` 也可以直接接受字符串：
```python
await self.ctx.adapter.send_group_message(group_id, "纯文本消息")
```

### 2.2 @ 提及

```python
msg = MessageGroup([
    at("123456789"),            # @ QQ 号 123456789
    text(" 请查看附件"),
])
await self.ctx.adapter.send_group_message(group_id, msg)
```

### 2.3 图片

```python
# 本地文件
msg = MessageGroup([
    text("这是今天的天气图："),
    image("/tmp/weather.png"),
])

# 子类型标记（如 QQ 贴纸）
msg = MessageGroup([
    image("/tmp/sticker.gif", sub_type="1"),  # QQ 动画表情
])
```

| 参数 | 说明 |
|------|------|
| `file_path` | **必填**。本地文件绝对路径 |
| `url` | 可选。远程 URL（用于回退） |
| `sub_type` | 可选。QQ 贴纸标记 `"1"` |

### 2.4 语音

```python
msg = MessageGroup([
    voice("/tmp/audio.amr"),
])
```

### 2.5 文件

```python
msg = MessageGroup([
    text("这是转换后的 PDF："),
    file("/tmp/report.pdf", name="月报.pdf"),
])
```

| 参数 | 说明 |
|------|------|
| `file_path` | **必填**。本地文件绝对路径 |
| `name` | 可选。显示的文件名 |

### 2.6 回复引用

```python
msg = MessageGroup([
    reply("msg_id_12345"),       # 引用某条消息
    text("收到！"),
])
```

---

## 3. 组合示例

### 3.1 天气卡片

```python
async def send_weather_card(self, city: str) -> PluginResponse:
    # 生成天气图片
    chart_path = await self._generate_weather_chart(city)

    msg = MessageGroup([
        at(self.ctx.message.channel_user_id),
        text(f" {city}今日天气："),
        image(chart_path),
        text("\n数据来源：中国气象局"),
    ])

    await self.ctx.adapter.send_group_message(
        self.ctx.message.group_id,
        msg,
    )
    return PluginResponse.ok()
```

### 3.2 通过 PluginResponse 返回多模态

默认情况下 `render_mode=direct` 时，你可以把 `message_group` 放在 `PluginResponse` 中：

```python
@command("chart", prefix="/", patterns=["图表"])
async def chart(self) -> PluginResponse:
    chart_path = await self._generate_chart()

    return PluginResponse.ok(
        text="这是生成的图表",
        message_group=MessageGroup([
            image(chart_path),
        ]),
    )
```

如果只需要多模态（不需要文本），可以只设 `message_group`：

```python
return PluginResponse.ok(
    message_group=MessageGroup([image("/tmp/result.png")]),
)
```

---

## 4. 快捷构造函数速查

```python
from sirius_pulse.adapters import text, at, image, voice, file, reply

# text(text: str) → TextSegment
# at(user_id: str) → AtSegment
# image(file_path, url="", sub_type="") → ImageSegment
# voice(file_path) → VoiceSegment
# file(file_path, name="") → FileSegment
# reply(message_id) → ReplySegment
```

---

## 5. 下一步

- **指南（五）**：进阶话题 —— 权限、事件、定时任务
