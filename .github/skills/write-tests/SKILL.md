---
name: write-tests
description: "编写新测试文件或为现有模块补充测试时使用，覆盖命名约定、速度要求、Mock 模式与断言规范。关键词：测试编写、单元测试、pytest、MockProvider、测试速度、精准测试。"
---

# 测试编写指南

## 目标

为 Sirius Pulse 编写正确、快速、可维护的 pytest 测试。测试必须从业务侧出发，依照业务逻辑和用户实际使用路径设计，优先验证用户输入、公开入口、系统处理与可观察结果之间的闭环。所有测试必须满足四个核心要求：

1. **快（Fast）**：单个测试用例应在 **1 秒以内** 完成；整个测试套件目标 **30 秒以内**。
2. **业务侧（Business-first）**：测试场景应对应用户实际使用方式，避免只验证私有方法、内部字段或临时实现细节。
3. **准（Precise）**：一个测试函数只验证一个业务概念，断言直接指向用户可感知的行为。
4. **稳（Stable）**：无真实网络调用、无随机时间依赖、无副作用残留。

---

## 零、业务侧测试原则

测试用例应先回答“用户如何使用这个能力”，再决定调用哪个入口和断言什么结果。优先从公开 API、引擎入口、CLI/配置入口、WebUI API、持久化结果等用户可触达路径验证行为。

### 推荐覆盖

- 用户发送消息后，引擎是否依据人格、上下文、记忆和策略给出合理响应。
- 用户配置 Provider、人格、技能或平台适配后，系统是否产生符合预期的业务行为。
- 记忆、事件、技能执行等内部模块是否在真实调用链中协作正确。
- 失败、空输入、非法配置、Provider 异常等场景是否返回用户可理解的降级结果。

### 禁止倾向

- 只为了覆盖率直接测试私有方法或内部字段。
- 把实现步骤逐行复刻成测试，导致重构时业务没变但测试大量失败。
- 断言无业务意义的中间变量、调用次数或临时数据结构。

---

## 一、速度红线

### ❌ 禁止的写法

```python
# 禁止：在测试中手动睡眠
import asyncio
await asyncio.sleep(8)   # 等同于浪费 8 秒
time.sleep(3)

# 禁止：启动耗时后台任务（除非测试的正是该任务）
engine.start_background_tasks()
# 后台任务会触发日记生成等 LLM 调用
```

### ✅ 标准配置模板（绝大多数测试应使用此配置）

```python
engine = EmotionalGroupChatEngine(
    work_path=tmp_path,
    persona=PersonaProfile(name="TestBot"),
    config={
        "sensitivity": 0.0,  # ← 降低回复概率，减少不必要的 LLM 调用
    },
)
```

---

## 二、标准 Provider 与引擎初始化

```python
from sirius_pulse.core.emotional_engine import EmotionalGroupChatEngine
from sirius_pulse.models.persona import PersonaProfile
from sirius_pulse.models.models import Message, Participant
from sirius_pulse.providers.mock import MockProvider

provider = MockProvider(
    responses=[
        "第一条回复",
        "第二条回复",
    ]
)
engine = EmotionalGroupChatEngine(
    work_path=tmp_path,
    persona=PersonaProfile(name="TestBot"),
    provider_async=provider,
)
```

`MockProvider` 按序返回 responses，用完后循环最后一条。可通过 `provider.requests` 检查引擎向 LLM 发出的所有请求。

---

## 三、标准 process_message 测试模式

```python
@pytest.mark.asyncio
async def test_process_message_replies_to_greeting(tmp_path):
    provider = MockProvider(responses=["你好呀！"])
    engine = EmotionalGroupChatEngine(
        work_path=tmp_path,
        persona=PersonaProfile(name="TestBot"),
        provider_async=provider,
    )

    participant = Participant(name="u1", user_id="u1")
    msg = Message(role="human", content="大家好", speaker="u1")

    reply = await engine.process_message(msg, [participant], "group_a")

    assert reply is not None
    assert "你好" in reply.content
```

---

## 四、异步测试写法

所有涉及引擎的测试必须使用 `pytest.mark.asyncio`：

```python
import pytest

@pytest.mark.asyncio
async def test_something(tmp_path):
    engine = EmotionalGroupChatEngine(work_path=tmp_path, ...)
    ...
```

非异步逻辑（纯数据模型、工具函数）可用普通 `def`。

---

## 五、断言规范

### 推荐断言模式

| 场景 | 推荐写法 | 不推荐 |
|------|---------|--------|
| 验证回复内容 | `assert "关键词" in reply.content` | 完整字符串匹配（易因微调而失败） |
| 验证事件类型 | `assert SessionEventType.X in types` | 仅验证事件数量 |
| 验证记忆写入 | `assert len(manager.entries) == 1` | 隐式依赖其他测试的数据 |
| 验证异常 | `pytest.raises(ValueError, match="...")` | `try/except` + `assert False` |

断言应优先落在业务结果上，例如回复内容、事件类型、记忆可检索性、配置生效结果、错误提示和持久化文件内容。只有当调用次数、内部字段或中间结构本身是业务契约时，才应直接断言这些实现细节。

---

## 六、文件组织规范

测试文件应按被测模块命名：

| 被测模块 | 测试文件 |
|---------|---------|
| `memory/basic/manager.py` | `tests/test_basic_memory.py` |
| `memory/diary/manager.py` | `tests/test_diary_memory.py` |
| `memory/context_assembler.py` | `tests/test_context_assembler.py` |
| `core/emotional_engine.py` | `tests/test_engine_event_stream.py` |
| `core/prompt_factory.py` | `tests/test_response_assembler.py` |

---

## 七、Mock 技巧速查

### MockProvider

```python
from sirius_pulse.providers.mock import MockProvider

provider = MockProvider(responses=["reply1", "reply2"])
# 第 3 次调用会循环回到 "reply1"

# 检查请求内容
assert len(provider.requests) == 2
assert "system" in provider.requests[0]["messages"][0]["role"]
```

### 手动 patch 依赖

```python
from unittest.mock import patch

with patch("sirius_pulse.memory.diary.generator.DiaryGenerator.generate") as mock_gen:
    mock_gen.return_value = DiaryEntry(...)
    # 执行被测代码...
```

---

## 八、完整示例

```python
"""Tests for BasicMemoryManager heat tracking."""

from __future__ import annotations

import pytest

from sirius_pulse.memory.basic.manager import BasicMemoryManager


class TestBasicMemoryHeat:
    def test_new_group_has_zero_heat(self):
        mgr = BasicMemoryManager()
        assert mgr.compute_heat("g1") == 0.0

    def test_heat_increases_after_messages(self):
        mgr = BasicMemoryManager()
        for i in range(5):
            mgr.add_entry("g1", f"u{i}", "user", f"msg{i}")
        assert mgr.compute_heat("g1") > 0.0
```
