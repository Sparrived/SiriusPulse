# LLM 交互中枢（Brain）

> **v1.2 新增** — 统一管理所有 LLM API 调用，两条通道 + hook 扩展。

## 一句话定位

Brain 是项目所有 LLM 调用的**唯一入口**。任何模块想要调用 LLM，必须通过 Brain 的两条通道之一，
不再允许直接调用 `provider_async.generate_async()`。

## 设计原则

> 项目本质 = 组装消息 → 喂给 API → 拿到原生文本。哪怕 `[SKILL_CALL:]` 也只是原生文本里的一种标记。

Brain 将这一流程标准化为 `pre → call → post` 三段式，并支持外部 hook 注入。

## 两条通道

| 通道 | 方法 | 用途 | 人格注入 | 上下文 | SKILL 解析 | 入口参数 |
|------|------|------|---------|--------|-----------|---------|
| **原生调用** | `raw_call()` | Cognition 情感/意图分析 | ❌ | ❌ | ❌ | `RawRequest` |
| **对话生成** | `chat()` | 回复生成、Plugin 风格化、SKILL 循环 | ✅ 自动 | ✅ 自动 | ✅ | `ChatRequest` |

## chat() 处理链

```
外部调用 chat(ChatRequest)
    │
    ├── pre-hooks（按 priority 升序，task_filter 过滤）
    │
    ├── 默认 pre 步骤：
    │   1. 人格注入        persona.build_system_prompt() + sticker 选项
    │   2. 语气对齐        _get_tone_alignment(group_id)
    │   3. 当前时间注入    UTC+8 时间戳
    │   4. 模型路由        ModelRouter.resolve(task_name, urgency, heat_level)
    │   5. 风格覆盖        StyleParams / ChatRequest.{temperature, max_tokens}
    │
    ├── LLM 调用          provider.generate_async(GenerationRequest)
    │
    ├── 默认 post 步骤：
    │   6. XML 剥离        移除模型回显的 <conversation_history>
    │   7. SKIP 检测       识别 <skip/> 标签
    │   8. SKILL 解析      parse_skill_calls()
    │   9. 表情包解析      _parse_sticker_tags()
    │  10. token 记录      TokenUsageRecord 构建 + 持久化
    │
    └── post-hooks（按 priority 升序，task_filter 过滤）
         ↓
     返回 ChatResult
```

## 参数类

### ChatRequest — chat() 入口

```python
@dataclass
class ChatRequest:
    group_id: str              # 目标群/私聊 ID
    user_id: str               # 目标用户 ID
    system_prompt: str         # 业务 prompt（人格由 Brain 自动注入）
    messages: list[dict]       # 对话历史

    # 任务控制
    task_name: str = "response_generate"
    urgency: int = 0

    # 风格覆盖（可选，不传则用默认路由）
    temperature: float | None = None
    max_tokens: int | None = None
    style_params: StyleParams | None = None

    # SKILL 控制
    enable_skills: bool = True
    caller_is_developer: bool = False

    # 对话深度（由引擎维护）
    last_reply_at: float = 0.0
    last_reply_depth: int = 0

    # 后处理总闸（True 才执行 hook）
    post_process: bool = False
```

### ChatResult — chat() 返回值

```python
@dataclass
class ChatResult:
    raw_text: str              # LLM 原始输出（含 SKILL_CALL 标记）
    clean_text: str            # 清理后文本（无 SKILL_CALL，无 sticker 标签）
    model_name: str            # 实际使用的模型
    duration_ms: float         # 调用耗时
    token_record: Any          # TokenUsageRecord
    sticker_names: list[str]   # 解析到的表情包名称
    has_skill_call: bool       # 是否存在 SKILL_CALL
    skill_calls: list          # 解析到的 (name, params) 列表
```

### RawRequest — raw_call() 入口

```python
@dataclass
class RawRequest:
    model: str
    system_prompt: str
    messages: list[dict]
    temperature: float = 0.2
    max_tokens: int = 512
    timeout_seconds: float = 30.0
    purpose: str = "cognition_analyze"
```

## 便捷方法

### generate_text()

```python
async def generate_text(
    system_prompt: str,
    messages: list[dict],
    group_id: str,
    *,
    style_params=None,
    task_name="response_generate",
    urgency=0,
    enable_skills=False,
    post_process=False,
) -> str:
```

单轮 `chat()` 的简化包装，直接返回 `raw_text`（字符串）。供外部模块使用，无需处理 `ChatResult`。

## Hook 系统

### 类型

```python
PreHook  = Callable[[Brain, ChatRequest, dict], None]
PostHook = Callable[[Brain, ChatRequest, ChatResult, dict], None]
```

- `request` — 可修改的 ChatRequest
- `result` — 可修改的 ChatResult（仅 post-hook）
- `ctx` — 跨 hook 共享字典，Brain 注入 `ctx["task_name"]`

### 注册 API

```python
def register_pre_hook(hook, priority=0, task_filter=None)
def register_post_hook(hook, priority=100, task_filter=None)
```

- **priority**：越大越晚执行。pre-hook 默认 0（最早），post-hook 默认 100（最晚，用户自定义）
- **task_filter**：`None` = 对所有 `task_name` 生效；`set[str]` = 仅匹配的 task 触发

### 执行条件

Hook 执行需同时满足：
1. `ChatRequest.post_process = True`（总闸）
2. `task_filter is None` 或 `ctx["task_name"] in task_filter`（过滤）

### 引擎内置 post-hook 优先级阶梯

| priority | hook | task_filter | 职责 |
|----------|------|-------------|------|
| 0 | `_hook_depth` | `{response_generate, proactive_generate}` | 对话深度追踪 |
| 20 | `_hook_stickers` | `{response_generate, proactive_generate}` | 表情包发送 |
| 30 | `_hook_dedup` | `{response_generate}` | 回复去重（可能清空 clean_text） |
| 40 | `_hook_memory` | `{response_generate, proactive_generate}` | 记忆记录（basic + semantic） |
| 50 | `_hook_timestamp` | `{response_generate, proactive_generate}` | 回复时间戳 + 状态持久化 |

### 外部注册示例

```python
# 插件 hook — 对所有 task_name 生效（不传 task_filter）
def my_post_hook(brain, request, result, ctx):
    if result.clean_text:
        result.clean_text = result.clean_text.replace("敏感词", "***")

brain.register_post_hook(my_post_hook, priority=90)
```

## 调用方一览

| 调用方 | 使用通道 | post_process | task_name |
|--------|---------|-------------|-----------|
| engine.process_message → _generate | `generate_text()` | ✅ True | `response_generate` |
| bg_tasks.proactive_check | `generate_text()` | ✅ True | `response_generate` |
| bg_tasks._generate_developer_chat | `generate_text()` | ✅ True | `response_generate` |
| bg_tasks.tick_delayed_queue（SKILL loop） | `generate_text()` | ❌ False* | `response_generate` |
| helpers._analyze_user_interest | `generate_text()` | ❌ False | `cognition_analyze` |
| plugins/dispatcher._handle_llm | `generate_text()` | ❌ False | `plugin_render` |
| plugins/context.generate_text | `generate_text()` | ❌ False | `plugin_generate` |
| plugins/context.generate_text_analysis | `generate_text()` | ❌ False | `plugin_analyze` |
| skill_engine_context.generate_text | `generate_text()` | ❌ False | `passive_skill` |
| cognition.CognitionAnalyzer | `raw_call()` | N/A | `cognition_analyze` |

> *`tick_delayed_queue` 不启用 post_process 是因为 SKILL 反馈循环多轮生成，
> 只在最后一轮才需要后处理。目前由调用方手动处理最后一轮的 sticker/dedup/memory。

## 与 Provider 的关系

```
                   ┌─────────────┐
外部模块 ──────────→   Brain     │
                   │  .chat()    │──→ ModelRouter.resolve()
                   │  .raw_call()│──→ GenerationRequest
                   │  .generate  │──→ provider.generate_async()
                   │  _text()    │──→ TokenUsageRecord
                   └─────────────┘
```

Brain **封装**了 Provider，外部不再直接接触 `provider_async`。当 Brain 不存在时（如测试环境），`generate_text()` 等会通过 Brain 内部的 `_provider_call()` 安全处理。

## 设计决策

1. **人格无条件注入**：`chat()` 通道**始终**注入 `persona.build_system_prompt()`，不提供开关。
   人格是对话通道的本质属性，正如 HTTP 请求一定有 `Host` 头。

2. **post_process 总闸**：默认关闭。只有引擎对话类调用方显式传 `True`。
   分析类调用（cognition、plugin analyze、skill context）不触发引擎副作用。

3. **task_filter 在注册时声明**：不在 hook 内部检查，由 Brain 调度层统一过滤。
   好处：外部注册时不传 `task_filter`（默认 `None`）= 始终生效，零配置。

4. **SKILL 反馈循环由调用方管理**：Brain 的 `chat()` 只负责单轮。多轮 SKILL 循环
   由调用方（当前是 `tick_delayed_queue`）自行控制。

## 相关文件

| 文件 | 角色 |
|------|------|
| `core/brain.py` | Brain 类定义、参数类、hook 注册 API |
| `core/engine_core.py` | 引擎初始化 Brain，注册引擎 post-hook |
| `core/cognition.py` | 使用 `brain.raw_call()` 做认知分析 |
| `core/bg_tasks.py` | 使用 `brain.generate_text()` 做延迟/主动/开发者对话 |
| `plugins/dispatcher.py` | 使用 `engine.brain.generate_text()` 做 Plugin 风格化 |
| `plugins/context.py` | 使用 `engine.brain.generate_text()` 做 Plugin 内部分析 |
