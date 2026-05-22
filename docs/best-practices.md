# Sirius Pulse 最佳实践指南（v1.1+）

## 1. 并发会话管理

### Emotional Engine 的并发安全

`EmotionalGroupChatEngine` 是**群隔离**的——不同 `group_id` 之间完全独立，可以安全并发：

```python
import asyncio
from sirius_pulse import create_emotional_engine, Message, Participant

async def handle_group(engine, group_id: str, messages: list[tuple[str, str]]):
    """为单个群聊处理消息。"""
    results = []
    for user_id, content in messages:
        result = await engine.process_message(
            message=Message(role="human", content=content, speaker=user_id),
            participants=[Participant(name=user_id, user_id=user_id)],
            group_id=group_id,
        )
        results.append(result)
    return results

async def main():
    engine = create_emotional_engine(
        work_path="./data",
        provider=provider,
        persona="warm_friend",
    )
    engine.start_background_tasks()

    # 并发处理多个群聊
    tasks = [
        handle_group(engine, f"group_{i}", [(f"user_{j}", f"消息 {j}") for j in range(5)])
        for i in range(3)
    ]
    results = await asyncio.gather(*tasks)

    engine.save_state()
```

### 会话隔离

为不同群聊使用不同的 `group_id` 即可自动隔离：

```python
# 群 A 的用户记忆不会泄漏到群 B
result_a = await engine.process_message(msg_a, participants_a, group_id="group_a")
result_b = await engine.process_message(msg_b, participants_b, group_id="group_b")
```

## 2. 错误处理

### 捕获和处理异常

```python
import asyncio
from sirius_pulse.exceptions import SiriusChatException, ProviderError

async def safe_process_message(engine, message, participants, group_id):
    try:
        return await engine.process_message(message, participants, group_id)
    except ProviderError as e:
        print(f"Provider 错误: {e}")
        return {"strategy": "silent", "reply": None}
    except SiriusChatException as e:
        print(f"引擎错误: {e}")
        return {"strategy": "silent", "reply": None}
```

## 3. 性能调优

### 敏感度调节

`config["sensitivity"]` 控制回复频率：

```python
# 低敏感度：只在高 urgency 时回复
config = {"sensitivity": 0.3}

# 高敏感度：容易回复
config = {"sensitivity": 0.8}
```

### 模型选择

通过 `task_model_overrides` 按任务分配模型：

```python
config = {
    "task_model_overrides": {
        # 认知分析用便宜模型
        "cognition_analyze": {"model": "gpt-4o-mini", "max_tokens": 384},
        # 回复生成用好模型
        "response_generate": {"model": "gpt-4o", "max_tokens": 512},
    }
}
```

### 基础记忆窗口

`BasicMemoryManager` 使用硬限制（默认 30）和上下文窗口（默认 5）。上下文窗口决定嵌入 system prompt 的近期消息数，硬限制决定内存中保留的总消息数。

```python
# 高活跃群聊：增大硬限制
config = {"basic_memory_hard_limit": 50}

# 低活跃群聊：减小硬限制
config = {"basic_memory_hard_limit": 20}
```

## 4. 生产环境部署

### 最小生产配置

```json
{
  "providers": [
    {
      "type": "openai-compatible",
      "base_url": "https://api.openai.com",
      "api_key": "${OPENAI_API_KEY}",
      "healthcheck_model": "gpt-4o-mini"
    }
  ],
  "persona": "warm_friend",
  "emotional_engine": {
    "sensitivity": 0.5,
    "proactive_silence_minutes": 60,
    "delayed_queue_tick_interval_seconds": 10,
    "task_model_overrides": {
      "response_generate": { "model": "gpt-4o", "max_tokens": 256 },
      "cognition_analyze": { "model": "gpt-4o-mini", "max_tokens": 384 }
    }
  }
}
```

### 状态持久化

```python
# 定期保存（例如每 5 分钟）
async def periodic_save(engine):
    while True:
        await asyncio.sleep(300)
        engine.save_state()

# 退出时保存
try:
    ...
finally:
    engine.stop_background_tasks()
    engine.save_state()
```

### 监控

```python
from sirius_pulse.core.events import SessionEventType

async def monitor(engine):
    async for event in engine.event_bus.subscribe():
        if event.type == SessionEventType.EXECUTION_COMPLETED:
            has_reply = event.data.get("has_reply")
            print(f"回复生成: {has_reply}")
        elif event.type == SessionEventType.DECISION_COMPLETED:
            strategy = event.data.get("strategy")
            print(f"决策策略: {strategy}")
```

## 5. 常见问题

### Q: 引擎不回复怎么办？

检查：
1. `sensitivity` 是否过低？
2. 消息 urgency_score 是否太低？
3. provider 是否正确配置？
4. 后台任务是否已启动（`start_background_tasks()`）？

### Q: 回复太频繁怎么办？

降低 `sensitivity`，或选择 `reply_frequency="low"` 的人格模板。

### Q: 如何关闭主动发言？

```python
config = {"proactive_silence_minutes": 999999}
```

### Q: 助手回复会被记录到工作记忆中吗？

会。引擎会将 assistant 回复以标准 OpenAI messages 格式写入工作记忆，并赋予动态 importance，供后续上下文使用。

### Q: 如何让 SKILL 结果不显示在回复中？

在 `SKILL_META` 中设置 `silent: true`。此时 SKILL 的执行结果不会追加到回复文本，但会通过 `internal_metadata` 保留在事件数据中，供下游处理。
