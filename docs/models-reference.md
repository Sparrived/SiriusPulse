# 核心数据模型参考

> **类型契约层** — 所有模块共享的数据结构定义，使用 `@dataclass(slots=True)` 保证类型安全和内存紧凑。

## 一句话定位

`sirius_chat/models/` 目录定义了引擎、记忆、平台和 provider 层之间传递的**所有核心数据结构**，是跨模块协作的契约基础。

## 为什么需要它

当消息从 QQ 流入引擎、再流出到 LLM provider 时，需要经过多个模块。如果每个模块用自己的 dict 表示"一条消息"，字段名、类型、默认值都会漂移。集中定义 dataclass 后：
- 所有模块对"消息是什么"有统一理解
- IDE 和类型检查器能捕获字段拼写错误
- 序列化/反序列化逻辑只需写一次（通过 `JsonSerializable` mixin）
- 新增字段时旧数据自动 fallback 到默认值

---

## 模型目录结构

```
sirius_chat/models/
├── models.py           # 核心聊天原语：Message、Participant、Transcript
├── persona.py          # 人格定义：PersonaProfile
├── emotion.py          # 情绪状态：EmotionState、AssistantEmotionState、EmpathyStrategy
├── intent_v3.py        # 意图分析：IntentAnalysisV3、SocialIntent
└── response_strategy.py # 响应策略：StrategyDecision、ResponseStrategy
```

> `emotion.py`、`intent_v3.py`、`response_strategy.py` 已在《认知层》文档中详细覆盖。本文档聚焦 `models.py` 和 `persona.py`。

---

## models.py — 聊天原语

### Message（单条消息）

```python
@dataclass(slots=True)
class Message:
    role: str                    # "user" / "assistant" / "system"
    content: str
    speaker: str = ""            # 发言者昵称
    user_id: str = ""            # 发言者唯一 ID
    channel: str = ""            # 频道/群号
    adapter_type: str = ""       # 来源适配器类型（napcat / discord / ...）
    multimodal_inputs: list[MultimodalInput] = field(default_factory=list)
    reply_mode: str = "auto"     # auto / never / mention_only
    sender_type: str = "human"   # human / ai / system
```

`Message` 是引擎处理的最小单位。平台适配器（如 `NapCatAdapter`）负责把平台特定事件转换为 `Message`。

### Participant（参与者）

```python
@dataclass(slots=True)
class Participant:
    name: str
    user_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    aliases: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
```

代表群聊中的一个人类用户。`is_developer` 是计算属性，通过 `metadata_declares_developer(metadata)` 判断。

**User 是 Participant 的别名**：外部 API 使用 `User` 名称，内部实现统一为 `Participant`。

### ReplyRuntimeState（回复运行时）

```python
@dataclass(slots=True)
class ReplyRuntimeState:
    last_reply_at: float = 0.0
    reply_count_in_window: int = 0
    window_start_at: float = 0.0
```

跟踪回复频率限制状态。`RhythmAnalyzer` 和 `ResponseStrategyEngine` 用它决定是否需要冷却。

### Transcript（完整会话）

```python
@dataclass
class Transcript:
    messages: list[Message] = field(default_factory=list)
    user_memory: Any = None            # UserManager 实例
    reply_runtime: ReplyRuntimeState = field(default_factory=ReplyRuntimeState)
    session_summary: str = ""
    orchestration_stats: dict = field(default_factory=dict)
    token_usage_records: list[TokenUsageRecord] = field(default_factory=list)
```

一个 `Transcript` 包含一次完整会话的全部状态。`session/store.py` 负责它的持久化。

**关键方法**：
- `add(msg)` — 添加消息
- `remember_participant(participant)` — 注册参与者到 `user_memory`
- `find_user_by_channel_uid(channel, uid)` — 按频道 UID 查找用户
- `compress_for_budget(budget)` — 当消息过多时压缩旧消息为摘要
- `as_chat_history()` — 格式化为 OpenAI 风格的 `{"role": ..., "content": ...}` 列表

---

## persona.py — 人格定义

### PersonaProfile

```python
@dataclass(slots=True)
class PersonaProfile:
    # Identity
    name: str = ""
    aliases: list[str] = field(default_factory=list)
    persona_summary: str = ""
    backstory: str = ""

    # Personality
    personality_traits: list[str] = field(default_factory=list)
    core_values: list[str] = field(default_factory=list)
    flaws: list[str] = field(default_factory=list)
    motivations: list[str] = field(default_factory=list)

    # Expression
    communication_style: str = ""
    speech_rhythm: str = ""
    catchphrases: list[str] = field(default_factory=list)
    emoji_preference: str = ""
    humor_style: str = ""
    typical_greetings: list[str] = field(default_factory=list)
    typical_signoffs: list[str] = field(default_factory=list)

    # Emotional
    emotional_baseline: dict = field(default_factory=dict)   # {"valence": 0.1, "arousal": 0.4}
    emotional_range: dict = field(default_factory=dict)
    empathy_style: str = ""
    stress_response: str = ""

    # Behavior
    boundaries: list[str] = field(default_factory=list)
    taboo_topics: list[str] = field(default_factory=list)
    preferred_topics: list[str] = field(default_factory=list)
    social_role: str = ""

    # Runtime
    max_tokens_preference: int = 0
    temperature_preference: float = 0.0
    reply_frequency: str = "moderate"   # high / moderate / low / selective

    # Override
    full_system_prompt: str = ""        # 若设置则完全覆盖自动生成的 prompt
```

### build_system_prompt()

`PersonaProfile` 的核心输出方法，将人格字段转换为引擎注入的系统提示词：

```
【角色】{name}

身份锚点：{persona_summary}

【背景故事】
{backstory}

【人格底色】
{personality_traits} / {core_values} / {flaws}

【情绪反应】
基线：{emotional_baseline}
压力反应：{stress_response}
共情风格：{empathy_style}

【关系模式】
社交角色：{social_role}
边界：{boundaries}

【说话方式】
风格：{communication_style}
节奏：{speech_rhythm}
口头禅：{catchphrases}
幽默：{humor_style}

【回应习惯】
回复频率：{reply_frequency}
禁忌话题：{taboo_topics}
偏好话题：{preferred_topics}

【场景行为】
你在一个多人聊天场景里...
```

如果设置了 `full_system_prompt`，则完全覆盖上述自动生成的内容。

### 三条生成路径

| 路径 | 入口 | 说明 |
|------|------|------|
| **Template** | `PersonaGenerator.from_template(id)` | 零成本，6 种内置模板 |
| **Keywords** | `PersonaGenerator.from_keywords(...)` | 低成本，关键词映射到 trait → 自动补全 |
| **Interview** | `PersonaGenerator.from_interview(...)` | 高成本，8 道问卷 → LLM 生成完整人格 |

---

## JsonSerializable（序列化 mixin）

定义在 `mixins.py`：

```python
class JsonSerializable:
    def to_dict(self) -> dict: ...
    @classmethod
    def from_dict(cls, data: dict) -> Self: ...
```

使用 `dataclasses.fields()` 和 `dataclasses.asdict()` 实现反射式序列化：
- `to_dict()`：递归将 dataclass 树转为纯 dict
- `from_dict()`：按字段名还原对象；缺失字段使用声明的 `default` 或 `default_factory`

**继承者**：`Message`、`ReplyRuntimeState`、`Participant`、`PersonaProfile` 等大多数模型类。`Transcript` 由于内含复杂对象（`UserManager`），实现了自定义的 `to_dict()` / `from_dict()`。

---

## 异常体系（exceptions.py）

```
SiriusException
├── ProviderError
│   ├── ProviderConnectionError      # 网络超时/断连（可重试）
│   ├── ProviderAuthError            # API key 无效（不可重试）
│   └── ProviderResponseError        # HTTP/格式错误（429/503 可重试）
├── TokenError
│   ├── TokenBudgetExceededError
│   └── TokenEstimationError
├── ParseError
│   ├── JSONParseError
│   └── ContentValidationError
├── ConfigError
│   ├── InvalidConfigError
│   ├── MissingConfigError
│   └── OrchestrationConfigError
└── MemoryError
    ├── UserNotFoundError
    └── ConflictingMemoryError
```

所有异常携带：
- `error_code`：机器可读错误码
- `context`：上下文字典
- `is_retryable`：中间件/重试逻辑据此决策
- `to_dict()`：序列化为结构化日志

---

## Trait Taxonomy（trait_taxonomy.py）

静态字典 `TRAIT_TAXONOMY`，将 7 个高维 trait 类别映射到约 350 个中文关键词：

| 类别 | 优先级 | 示例关键词 |
|------|--------|-----------|
| Learning | 1 | 好奇、编程、技能提升、在线课程 |
| Social | 2 | 社交、聚会、团队、合作、约会 |
| Lifestyle | 3 | 饮食、睡眠、运动、作息 |
| Creative | 4 | 艺术、音乐、画画、写作、摄影 |
| Practical | 5 | 工具、效率、优化、务实 |
| Emotional | 6 | 心情、压力、焦虑、倾诉、安慰 |
| Leisure | 7 | 游戏、看电影、旅游、爱好 |

被 `PersonaGenerator` 和认知分析器用作标准化的兴趣/性格标签词汇表。

---

## DeveloperProfiles（developer_profiles.py）

单一函数 `metadata_declares_developer(metadata)`，判断用户 metadata 是否声明了开发者身份：

1. 检查 `is_developer` / `developer` 直接标志
2. 检查 `role` / `roles` 字段是否包含 `developer` / `dev` / `engineer`
3. 支持 bool、int、float、str 多种类型的规范化

被 `Participant.is_developer` 属性委托调用，是 SKILL 的 `developer_only` 权限判断的源头。
