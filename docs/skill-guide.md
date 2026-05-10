# SKILL 系统指南

> **可扩展能力层** — 让 AI 角色能"动手"而不只是"动嘴"。
>
> 本文档合并了原 `skill-system.md`（系统介绍）和 `skill-authoring.md`（编写指南），提供从原理到实践的一站式参考。

---

## 第一章：核心设计哲学

### 1.1 文件即插件

一个 SKILL 就是一个 `.py` 文件，不需要注册表、不需要装饰器、不需要复杂的包结构。把文件丢进 `skills/` 目录，引擎启动时自动发现、自动加载、自动安装依赖。

### 1.2 AI 自主调用

SKILL 不是人手动触发的，而是 AI 在生成回复时**自己决定**要不要调用。引擎把可用的 SKILL 列表注入系统提示词，AI 在需要时会输出 `[SKILL_CALL: skill_name | {...}]` 标记，引擎解析并执行。

### 1.3 安全隔离

每个 SKILL 有自己的 JSON 数据存储、自己的依赖、自己的错误边界。一个 SKILL 崩溃不会拖垮引擎。

---

## 第二章：最小可用 SKILL

```python
"""一句话描述这个 SKILL 的用途。"""
from __future__ import annotations
from typing import Any

from sirius_chat import SkillInvocationContext

SKILL_META = {
    "name": "hello",
    "description": "向指定用户打招呼",
    "version": "1.0.0",
    "developer_only": False,
    "parameters": {
        "username": {
            "type": "str",
            "description": "用户名",
            "required": True,
        },
    },
}

def run(
    username: str,
    data_store: Any = None,
    invocation_context: SkillInvocationContext | None = None,
    **kwargs: Any,
) -> dict[str, Any]:
    return {"greeting": f"你好，{username}！"}
```

---

## 第三章：SKILL 文件格式

### 3.1 文件约定

| 约定 | 说明 |
|------|------|
| 存放位置 | `skills/` 目录下的 `.py` 文件；位于人格目录（`data/personas/{name}/skills/`） |
| 必须导出 | `SKILL_META` 字典 + `run()` 函数（主动模式）；被动模式可仅导出 `create_background_tasks()` / `create_triggers()` |
| 命名规则 | 文件名建议与 `SKILL_META["name"]` 一致（如 `hello.py`） |
| 编码 | UTF-8 |
| 跳过规则 | 以 `_` 或 `.` 开头的文件会被自动跳过 |

框架先预加载包内置 SKILL，再加载人格目录的 `skills/`。同名人格级文件会覆盖内置实现。

### 3.2 SKILL_META 字段

| 字段 | 必填 | 说明 |
|------|------|------|
| `name` | ✅ | SKILL 唯一标识，字母下划线 |
| `description` | ✅ | 给 AI 看的功能描述，决定 AI 会不会调用它 |
| `version` | ❌ | 语义化版本，默认 "1.0.0" |
| `developer_only` | ❌ | `True` 时只有开发者身份的用户能调用 |
| `silent` | ❌ | `True` 时技能结果不追加到回复文本 |
| `tags` | ❌ | 技能标签列表，用于分类和动态过滤 |
| `adapter_types` | ❌ | 适配器类型列表，限定只在特定 adapter 下可用 |
| `dependencies` | ❌ | 第三方依赖列表，自动安装 |
| `parameters` | ❌ | 参数 Schema（dict 或 list） |

### 3.3 parameters 的两种写法

**字典格式**（推荐）：

```python
"parameters": {
    "query": {
        "type": "str",
        "description": "搜索关键词",
        "required": True,
    },
    "limit": {
        "type": "int",
        "description": "最大返回条数",
        "required": False,
        "default": 10,
    },
}
```

**列表格式**：

```python
"parameters": [
    {"name": "query", "type": "str", "description": "搜索关键词", "required": True},
    {"name": "limit", "type": "int", "description": "最大返回条数", "required": False, "default": 10},
]
```

### 3.4 支持的参数类型

| type | Python 类型 | 自动转换规则 |
|------|-------------|-------------|
| `str` | `str` | 原样传递 |
| `int` | `int` | `int(value)`，失败则原样传递 |
| `float` | `float` | `float(value)`，失败则原样传递 |
| `bool` | `bool` | `"true"`/`"1"`/`"yes"` → `True`，其余 → `False` |
| `list[str]` / `list` | `list` | JSON 数组或逗号分割字符串 |
| `dict` | `dict` | 原样传递 |

---

## 第四章：run() 函数规范

```python
def run(
    param1: str,           # 与 parameters 中定义的参数名一一对应
    param2: int = 10,      # 可选参数需有默认值
    data_store: Any = None, # 自动注入的持久化存储
    invocation_context: SkillInvocationContext | None = None, # 自动注入的调用上下文
    **kwargs: Any,          # 吸收未知参数，保持前向兼容
) -> Any:
    """执行函数。返回值会被封装为 SkillResult.data。"""
    ...
```

**关键规则**：
- 参数名必须与 `SKILL_META["parameters"]` 中的 `name` 完全匹配
- `data_store` 与 `invocation_context` 由框架按函数签名自动注入，无需在 `parameters` 中声明
- `**kwargs` 建议始终保留
- 返回值推荐使用 `dict`，便于 AI 理解结构化结果
- 抛出异常会被捕获，AI 会收到 `[SKILL执行失败] {异常信息}`
- **执行超时**：默认 30 秒，超时后 SKILL 会被终止

---

## 第五章：AI 调用机制

### 5.1 调用语法

AI 在生成回复时嵌入标记：

```
[SKILL_CALL: system_info | {}]
[SKILL_CALL: desktop_screenshot | {"region": "full"}]
```

### 5.2 引擎处理流程

```
process_message() → _execution() → _generate() → 拿到回复
                                            ↓
                                    _process_skill_calls()
                                            ↓
                                    解析标记 → 剥离 → 执行 → 追加结果
```

引擎在收到回复后：
1. 用正则解析所有 `[SKILL_CALL: ...]` 标记
2. 把标记从回复文本中**剥离**，得到干净的自然语言回复
3. 对每个 SKILL 调用：
   - 校验参数类型和必填项
   - 校验开发者权限（`developer_only`）
   - 在 `asyncio.to_thread()` 中执行（防止阻塞事件循环）
4. 把执行结果追加到回复末尾

### 5.3 上下文保留

为避免"一次调用后下一轮就忘记"，框架会把最近几轮内的 SKILL 内部结果继续作为隐藏上下文保留，方便模型在短期追问里复用刚拿到的事实或观察。

---

## 第六章：数据存储（SkillDataStore）

每个 SKILL 有一个独立的 JSON 文件：`{work_path}/skill_data/{skill_name}.json`

```python
def run(data_store: Any = None, **kwargs: Any) -> dict:
    count = data_store.get("call_count", 0)
    data_store.set("call_count", count + 1)
    data_store.delete("old_key")
    all_keys = data_store.keys()
    all_data = data_store.all()
    return {"call_count": count + 1}
```

**特性**：
- 懒加载（第一次访问时才读文件）
- 脏检测（`set()` 自动标记 dirty，只有修改过才写回磁盘）
- 原子写入（temp file + replace）

**Artifact 目录**：二进制文件（如截图）存到 `{work_path}/skill_data/artifacts/{skill_name}/`

---

## 第七章：内置 SKILL

框架自带以下内置 SKILL：

| SKILL | 权限 | 功能 | 标签 |
|-------|------|------|------|
| `system_info` | 所有人 | 返回 CPU、内存、磁盘、网络、OS 信息 | `system`, `info` |
| `desktop_screenshot` | **仅开发者** | 截取桌面屏幕，返回图片 + 文字摘要 | `system`, `image` |
| `learn_term` | 所有人 | 将术语、俚语、黑话记录到 glossary | `memory`, `learning` |
| `url_content_reader` | 所有人 | 读取指定网页的文本内容 | `web`, `content` |
| `bing_search` | 所有人 | 通过 Bing 搜索网络内容 | `web`, `search` |
| `file_read` | 所有人 | 读取任意路径下的文本文件 | `file`, `io` |
| `file_list` | 所有人 | 列出或搜索文件和目录 | `file`, `io` |
| `file_write` | **仅开发者** | 创建或修改文本文件 | `file`, `io` |
| `reminder` | 所有人 | 设置定时提醒（支持主动创建 + 被动后台检查） | `utility`, `time` |

内置 SKILL 存放在 `sirius_chat/skills/builtin/`。

---

## 第八章：开发者权限

`developer_only=True` 的 SKILL 有两道防线：

1. **注册时过滤**：非开发者用户看不到该 SKILL 的工具描述（不会被注入系统提示词）
2. **执行时校验**：即使 AI 输出了调用标记，执行前会检查 `invocation_context.caller_is_developer`，未授权返回中文错误信息

开发者身份由 `UserProfile.is_developer` 决定，通常在 `primary_user.json` 中配置。

---

## 第九章：依赖自动安装

SKILL 加载时自动检测并安装缺失依赖：

1. 读取 `SKILL_META["dependencies"]`（优先）
2. 用 AST 静态分析 `run()` 文件中的所有 `import`，推断依赖（补充）
3. 过滤标准库和已安装的包
4. 调用 `uv pip install`（优先）或 `pip install`
5. 刷新 `importlib` 缓存

常见 import-name 到 package-name 的映射已内置：
- `PIL` → `Pillow`
- `bs4` → `beautifulsoup4`
- `cv2` → `opencv-python`

**推荐做法**：始终显式声明 `dependencies`，尤其是 import 名与包名不一致的库。

**关闭自动安装**：
```python
OrchestrationPolicy(
    enable_skills=True,
    auto_install_skill_deps=False,
)
```

---

## 第十章：结构化结果通道

当普通 `dict` 不足以表达技能结果时，可返回以下结构化字段：

```python
def run(**kwargs: Any) -> dict[str, Any]:
    return {
        "summary": "可选的普通字段",
        "text_blocks": [
            {"type": "text", "value": "检测到蓝天和少量白云。", "label": "summary"},
        ],
        "multimodal_blocks": [
            {
                "type": "image",
                "value": "https://example.com/sky.png",
                "mime_type": "image/png",
                "label": "source",
            }
        ],
        "internal_metadata": {
            "trace_id": "debug-only",
        },
    }
```

- `text_blocks`：供模型内部推理使用的附加文本块
- `multimodal_blocks`：图片输入；`value` 可以是公网 URL、本地文件路径或 `file://` URI
- `internal_metadata`：仅供内部链路使用，不应面向用户输出

若 SKILL 会生成本地图片，优先把文件写到 `data_store.artifact_dir`，再把路径写入 `multimodal_blocks`。

---

## 第十一章：被动 SKILL（Passive SKILL）

### 11.1 概述

被动 SKILL 是不由模型直接调用的能力。它通过**后台任务**（周期执行）或**事件触发器**（响应引擎事件）自主运行，并通过 `SkillEngineContext` 协议与引擎交互。

典型场景：
- 定时提醒检查（`reminder` SKILL 的被动模式）
- 周期性数据同步
- 事件驱动的消息处理

### 11.2 双模式 SKILL

一个 SKILL 可以同时具备主动和被动能力。例如 `reminder`：
- **主动**：AI 调用 `run()` 创建/查询/取消提醒
- **被动**：`create_background_tasks(ctx)` 注册每 15 秒的提醒检查任务

### 11.3 工厂函数

SKILL 文件通过导出以下工厂函数注册被动行为：

```python
from sirius_chat import BackgroundTaskSpec, TriggerSpec, SkillEngineContext

def create_background_tasks(ctx: SkillEngineContext) -> list[BackgroundTaskSpec]:
    """返回要注册的周期性后台任务列表。"""
    return [
        BackgroundTaskSpec(
            name="my_periodic_task",
            interval_seconds=60,
            run_loop=my_async_checker,
        ),
    ]

def create_triggers(ctx: SkillEngineContext) -> list[TriggerSpec]:
    """返回要注册的事件触发器列表。"""
    return [
        TriggerSpec(
            name="my_event_handler",
            event_type="COGNITION_COMPLETED",
            handler=my_async_handler,
        ),
    ]
```

### 11.4 BackgroundTaskSpec

| 字段 | 类型 | 说明 |
|------|------|------|
| `name` | `str` | 任务唯一标识 |
| `interval_seconds` | `float` | 执行间隔（秒） |
| `run_loop` | `Callable[[SkillEngineContext], Awaitable[None]]` | 异步回调，每次循环调用 |

`run_loop` 内部自行决定 sleep 时机，引擎仅负责创建 `asyncio.Task` 并管理生命周期。

### 11.5 TriggerSpec

| 字段 | 类型 | 说明 |
|------|------|------|
| `name` | `str` | 触发器唯一标识 |
| `event_type` | `str` | 监听的事件类型（对应 `SessionEventType` 的值） |
| `handler` | `Callable[[SkillEngineContext, SessionEvent], Awaitable[None]]` | 事件处理回调 |

引擎在 `helpers.py` 中通过 `_wrap_event_bus_for_triggers()` 包装 `event_bus.emit`，每次事件发射时自动分发给匹配的 trigger handler。

### 11.6 SkillEngineContext 协议

被动 SKILL 通过 `SkillEngineContext` 与引擎交互，主要方法：

| 方法 | 说明 |
|------|------|
| `generate_text(system_prompt, user_content, group_id)` | 调用 LLM 生成文本 |
| `queue_pending_message(group_id, text)` | 将消息放入待发送队列 |
| `emit_event(event)` | 发射引擎事件 |
| `get_data_store(skill_name)` | 获取指定 SKILL 的数据存储 |
| `get_active_groups()` | 获取当前活跃群列表 |
| `get_config_value(key, default)` | 读取引擎配置 |
| `get_persona()` | 获取当前人格信息 |
| `add_memory_entry(...)` | 写入基础记忆 |
| `get_skill_descriptions(caller_is_developer)` | 获取 SKILL 描述列表 |
| `get_current_adapter_type()` | 获取当前适配器类型 |

### 11.7 注册与生命周期

1. 引擎启动时，`helpers.py` 的 `_register_passive_skills()` 扫描所有注册的 SKILL
2. 对每个有 `create_background_tasks` / `create_triggers` 的 SKILL，调用工厂函数获取 spec 列表
3. 后台任务通过 `asyncio.create_task()` 启动，存入 `_passive_skill_tasks`
4. 触发器存入 `_passive_skill_triggers`，由 `_dispatch_triggers()` 分发
5. 引擎停止时，所有被动任务和触发器被自动清理

### 11.8 最小被动 SKILL 示例

```python
"""周期性清理过期缓存的被动 SKILL。"""
from __future__ import annotations
import asyncio
from sirius_chat import BackgroundTaskSpec, SkillEngineContext

SKILL_META = {
    "name": "cache_cleaner",
    "description": "周期性清理过期缓存（被动运行，无需 AI 调用）",
    "version": "1.0.0",
    "tags": ["utility"],
}

async def _clean_loop(ctx: SkillEngineContext) -> None:
    while True:
        await asyncio.sleep(3600)
        store = ctx.get_data_store("cache_cleaner")
        # 清理逻辑...

def create_background_tasks(ctx: SkillEngineContext) -> list[BackgroundTaskSpec]:
    return [
        BackgroundTaskSpec(
            name="cache_cleanup",
            interval_seconds=3600,
            run_loop=_clean_loop,
        ),
    ]
```

---

## 第十二章：Token 优化

当注册的技能数量 **超过 5 个** 时，`PromptFactory` 会自动切换到**紧凑描述模式**：

```
# 完整模式（≤5 个技能）
- bing_search: 使用必应搜索引擎检索指定关键词的网页摘要
    - query (str, 必填): 搜索关键词
    - count (int, 可选, 默认=3): 返回结果条数

# 紧凑模式（>5 个技能）
- bing_search(query:str, count:int=3): 使用必应搜索引擎检索指定关键词的网页摘要
```

也可在调用 `build_tool_descriptions(compact=True)` 时强制启用紧凑模式。

---

## 第十三章：完整示例

### 天气查询 SKILL

```python
"""查询指定城市的天气信息（示例，使用模拟数据）。"""
from __future__ import annotations
from datetime import datetime
from typing import Any

SKILL_META = {
    "name": "weather",
    "description": "查询指定城市的当前天气信息，包括温度、湿度和天气状况",
    "version": "1.0.0",
    "parameters": {
        "city": {
            "type": "str",
            "description": "城市名称，如 北京、上海",
            "required": True,
        },
    },
}

def run(city: str, data_store: Any = None, **kwargs: Any) -> dict[str, Any]:
    weather_data = {
        "city": city,
        "temperature": "22°C",
        "humidity": "65%",
        "condition": "多云",
        "wind": "东南风 3级",
        "updated_at": datetime.now().strftime("%H:%M"),
    }

    if data_store is not None:
        history = data_store.get("history", [])
        history.append({"city": city, "time": datetime.now().isoformat()})
        data_store.set("history", history[-50:])

    return weather_data
```

### Adapter 隔离示例

```python
SKILL_META = {
    "name": "qq_group_rank",
    "description": "查询当前 QQ 群的活跃度排行",
    "adapter_types": ["napcat"],  # 只有 napcat 来源的消息才会注入该 skill
    "parameters": {},
}

def run(**kwargs):
    return {"rank": "本周最活跃：Alice, Bob, Charlie"}
```

---

## 第十三章：启用与配置

在 `SessionConfig` 中配置：

```python
from sirius_chat import SessionConfig, OrchestrationPolicy

config = SessionConfig(
    orchestration=OrchestrationPolicy(
        enable_skills=True,              # 启用 SKILL 系统
        max_skill_rounds=3,              # 每轮最多连续调用次数
        skill_execution_timeout=30,      # SKILL 最大执行秒数
        auto_install_skill_deps=True,    # 自动安装 SKILL 依赖
    ),
)
```

---

## 第十四章：检查清单

编写完成后，对照以下清单确认：

- [ ] 文件放在人格目录的 `skills/` 目录下
- [ ] `SKILL_META` 包含 `name` 和 `description`
- [ ] `name` 仅包含字母、数字、下划线
- [ ] `description` 足够清晰，AI 能根据它判断何时调用
- [ ] `run()` 函数存在且参数名与 `parameters` 定义匹配
- [ ] `run()` 至少保留 `**kwargs`；若需要持久化或审计，显式接收 `data_store` / `invocation_context`
- [ ] 若是受限能力，已显式设置 `developer_only=True`
- [ ] 若技能结果无需展示给用户，已显式设置 `silent=True`
- [ ] 返回值为 `dict` 或可序列化对象
- [ ] 不依赖未安装的第三方库（或在 `dependencies` 中显式声明）
- [ ] 不包含长时间阻塞操作（注意 30 秒超时限制）
- [ ] 若是被动 SKILL，`create_background_tasks()` / `create_triggers()` 工厂函数签名正确
- [ ] 被动 SKILL 的 `run_loop` / `handler` 回调包含异常处理（`try/except` + 日志），不会因未捕获异常导致任务静默终止
