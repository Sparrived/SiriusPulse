# Plugin 开发指南（三）：PluginContext —— 引擎、适配器与数据

PluginContext 是 Plugin 执行时的"工具箱"。通过 `self.ctx` 你可以访问引擎能力、调用平台 API、读写持久化数据。

---

## 1. PluginContext 全景

```python
self.ctx.engine       # EngineProxy   — 引擎能力代理
self.ctx.adapter      # BaseAdapter   — 平台适配器（可直接调用 API）
self.ctx.message      # MessageContext — 当前消息上下文
self.ctx.data_store   # PluginDataStore — 独立 JSON 数据存储
self.ctx.config       # dict — 插件配置
self.ctx.plugin_name  # str — 插件名称
self.ctx.logger       # Logger — 插件专用日志
```

---

## 2. `ctx.engine` —— 引擎能力

通过 `EngineProxy` 调用引擎的 LLM 能力和事件系统：

### 2.1 生成人格化文本

```python
async def search(self, query: str) -> PluginResponse:
    # 让 LLM 生成一段风格化的回复
    text = await self.ctx.engine.generate_text(
        prompt="请用幽默的风格告诉用户搜索结果",
        group_id=self.ctx.message.group_id,  # 注入当前群聊上下文
    )
    return PluginResponse.ok(text=text)
```

### 2.2 获取人格信息

```python
def get_persona_name(self) -> str:
    """当前角色叫什么名字？"""
    return self.ctx.engine.get_persona_name()
    # 返回 "小星星" 之类的

def get_persona_info(self) -> dict:
    """当前角色的人设信息。"""
    info = self.ctx.engine.get_persona_info()
    # {
    #     "name": "小星星",
    #     "persona_summary": "...",
    #     "personality_traits": ["活泼", "好奇"],
    #     "communication_style": "轻松幽默"
    # }
    return info
```

### 2.3 发射事件

```python
def notify_background_job_done(self) -> None:
    """通知其他插件：任务完成。"""
    self.ctx.engine.emit_event("custom_event", {
        "plugin": self.name,
        "status": "done"
    })
```

---

## 3. `ctx.adapter` —— 平台能力

`ctx.adapter` 直接持有 `BaseAdapter` 实例（NapCatAdapter 等），可以调用平台原生 API。

### 3.1 发送消息

```python
async def announce(self, msg: str) -> PluginResponse:
    await self.ctx.adapter.send_group_message(
        self.ctx.message.group_id,
        msg,
    )
    return PluginResponse.ok()  # 已通过 adapter 发送，返回空响应
```

### 3.2 发送多模态消息

```python
from sirius_pulse.adapters import MessageGroup, text, at, image

async def send_weather_card(self, city: str) -> PluginResponse:
    msg = MessageGroup([
        at(self.ctx.message.user_id),              # @发送者
        text(f" {city}的天气如下："),
        image("/tmp/weather.png"),
    ])
    await self.ctx.adapter.send_group_message(
        self.ctx.message.group_id,
        msg,
    )
    return PluginResponse.ok()
```

### 3.3 群管理

```python
async def kick_user(self, target_uid: str) -> PluginResponse:
    await self.ctx.adapter.set_group_kick(
        self.ctx.message.group_id, target_uid
    )
    return PluginResponse.ok(text=f"已踢出用户 {target_uid}")

async def ban_user(self, target_uid: str, duration: int = 600) -> PluginResponse:
    await self.ctx.adapter.set_group_ban(
        self.ctx.message.group_id, target_uid, duration
    )
    return PluginResponse.ok(text=f"已禁言 {duration} 秒")

async def set_admin(self, target_uid: str) -> PluginResponse:
    await self.ctx.adapter.set_group_admin(
        self.ctx.message.group_id, target_uid, True
    )
    return PluginResponse.ok(text=f"已设置管理员")
```

### 3.4 群信息查询

```python
async def list_members(self) -> PluginResponse:
    members = await self.ctx.adapter.get_group_member_list(
        self.ctx.message.group_id
    )
    # members = [{"user_id": "123", "nickname": "小明", ...}, ...]
    names = [m.get("nickname", "") for m in members]
    return PluginResponse.ok(text="当前群成员: " + ", ".join(names))

async def get_member_card(self, target_uid: str) -> PluginResponse:
    info = await self.ctx.adapter.get_group_member_info(
        self.ctx.message.group_id, target_uid
    )
    card = info.get("card", "") or info.get("nickname", "")
    return PluginResponse.ok(text=f"{target_uid} 的名片: {card}")
```

### 3.5 消息操作

```python
async def delete_my_last_message(self) -> PluginResponse:
    # 撤回消息
    await self.ctx.adapter.delete_message(self.ctx.message.message_id)
    return PluginResponse.ok()
```

### 3.6 通用 API 调用

```python
async def call_raw_api(self) -> PluginResponse:
    # 调用底层 OneBot/其他平台的原始 API
    result = await self.ctx.adapter.call_api(
        "get_stranger_info",
        {"user_id": 123456}
    )
    return PluginResponse.ok(text=str(result))
```

### BaseAdapter 完整 API 速查

| 方法 | 说明 |
|------|------|
| `send_group_message(group_id, message)` | 发送群聊消息（支持 `MessageGroup \| str`） |
| `send_private_message(user_id, message)` | 发送私聊消息 |
| `delete_message(message_id)` | 撤回消息 |
| `get_group_member_list(group_id)` | 获取群成员列表 |
| `get_group_member_info(group_id, user_id)` | 获取成员详情 |
| `get_group_info(group_id)` | 获取群信息 |
| `set_group_kick(group_id, user_id)` | 踢出群成员 |
| `set_group_ban(group_id, user_id, duration)` | 禁言（0=解除） |
| `set_group_admin(group_id, user_id, enable)` | 设置/取消管理员 |
| `set_group_card(group_id, user_id, card)` | 修改群名片 |
| `set_group_name(group_id, name)` | 修改群名称 |
| `set_group_whole_ban(group_id, enable)` | 全员禁言 |
| `upload_group_file(group_id, file_path, name)` | 上传群文件 |
| `upload_private_file(user_id, file_path, name)` | 上传私聊文件 |
| `call_api(action, params)` | 调用平台原生 API |

---

## 4. `ctx.message` —— 消息上下文

当前触发的消息的详细信息：

```python
class MessageContext:
    group_id: str         # 群号（私聊时为空）
    user_id: str          # 发送者 ID
    channel: str          # 平台标识（如 "qq_native_sirius_pulse"）
    channel_user_id: str  # 平台原生用户 ID（如 QQ 号）
    message_id: str       # 消息 ID
    content: str          # 消息纯文本内容
    speaker_name: str     # 发送者昵称
```

使用示例：

```python
@command("whoami", prefix="/", patterns=["whoami"])
async def whoami(self) -> PluginResponse:
    msg = self.ctx.message
    return PluginResponse.ok(
        text=f"你是 {msg.speaker_name}，你的 QQ 号是 {msg.channel_user_id}，"
             f"你在群 {msg.group_id} 中。"
    )
```

---

## 5. `ctx.data_store` —— 数据持久化

每个 Plugin 有独立的 JSON 文件存储，适合保存配置、计数、缓存等。

```python
@command("count", prefix="#", patterns=["计数"])
async def count_usage(self) -> PluginResponse:
    store = self.ctx.data_store
    if store is None:
        return PluginResponse.fail("数据存储不可用")

    count = store.get("usage_count", 0) + 1
    store.set("usage_count", count)
    return PluginResponse.ok(text=f"你已使用本指令 {count} 次")

@command("reset", prefix="#", patterns=["重置计数"])
async def reset_count(self) -> PluginResponse:
    store = self.ctx.data_store
    if store:
        store.delete("usage_count")
    return PluginResponse.ok(text="计数已重置")
```

**API**：

| 方法 | 说明 |
|------|------|
| `store.get(key, default)` | 读取值 |
| `store.set(key, value)` | 写入值（自动持久化） |
| `store.delete(key)` | 删除值 |
| `store.all()` | 获取所有数据字典 |

---

## 6. `ctx.logger` —— 日志

```python
class MyPlugin(PluginBase):
    def on_load(self):
        self.ctx.logger.info("插件已加载，配置: %s", self.ctx.config)
        self.ctx.logger.debug("这是一条调试信息")
        self.ctx.logger.warning("这是一条警告")
```

---

## 7. 下一步

- **指南（四）**：多模态输出 —— 图片、语音、文件发送
