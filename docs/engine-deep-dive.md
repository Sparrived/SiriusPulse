# EmotionalGroupChatEngine 深度解析

> **v1.0 唯一引擎** — Sirius Chat 的核心对话编排引擎。
>
> 本文档合并了原 `engine-emotional.md`（引擎整体流程）和 `emotion-intent-analysis.md`（认知层细节），提供从宏观架构到微观实现的完整视角。

## 第一章：引擎定位与设计哲学

### 一句话定位

EmotionalGroupChatEngine 是一个**让 AI 角色像真人一样在群聊里说话**的引擎——它会看气氛、挑话题、等时机、有情绪、记仇也记好。

### 为什么需要四层架构

想象你在一个微信群里：

1. **感知**：你眼睛扫过屏幕，看到"小明"发了一条消息"今天工作好累"
2. **认知**：你立刻感觉到小明情绪低落（valence 负、arousal 高），他在倾诉（emotional intent），可能需要安慰
3. **决策**：群里正在热烈讨论周末计划，你现在插嘴安慰会不会打断别人？还是等话题间隙？或者干脆不回，让其他人处理？
4. **执行**：你决定等 30 秒，话题稍微冷却后发一句"辛苦了，周末好好放松下"

引擎把这四个步骤**显式拆分**，每层职责单一、可独立测试、可独立调优。

### 核心设计原则

- **感知层零 LLM 成本**：高吞吐场景下，感知层只做内存操作，不调用模型
- **认知层联合推断**：情绪和意图共享规则引擎、共享上下文、共享 LLM fallback，消除异步边界
- **决策层纯规则**：阈值计算、策略选择完全本地完成，零延迟
- **执行层按需调用**：只有决定回复时才调用 LLM，且按任务选择最经济的模型

---

## 第二章：四层认知管线详解

### 2.1 感知层（Perception）—— 眼睛和耳朵

**职责**：接收消息，更新所有上下文状态。这一步**不碰 LLM**，纯内存操作。

```
外部消息 (Message + Participant[] + group_id)
    │
    ▼
┌─────────────────────────────────────────┐
│ 1. IdentityResolver.resolve()           │
│    speaker_name → user_id → platform_uid │
│    跨平台身份追踪（QQ/Discord/微信统一）   │
├─────────────────────────────────────────┤
│ 2. UserManager.register()               │
│    注册/更新用户档案（群隔离存储）         │
│    {group_id: {user_id: UserProfile}}   │
├─────────────────────────────────────────┤
│ 3. BasicMemoryManager.add_entry()       │
│    写入按群滑动窗口                      │
│    硬限制 30 条 / 上下文窗口 5 条         │
├─────────────────────────────────────────┤
│ 4. RhythmAnalyzer.analyze()             │
│    更新群体热度 (0~1)                    │
│    heat = 消息速率 × 独特发言者 × 最近度  │
├─────────────────────────────────────────┤
│ 5. 更新 group_last_message_at           │
│    记录最后活跃时间戳                    │
└─────────────────────────────────────────┘
    │
    ▼
emit PERCEPTION_COMPLETED
```

**关键设计**：群隔离是 P0 要求。所有记忆操作必须携带 `group_id`，不同群聊的数据完全隔离。

---

### 2.2 认知层（Cognition）—— 大脑的理解

**职责**：统一分析情绪、意图、检索记忆。单层联合推断，情绪结果自然流入意图评分。

#### 2.2.1 统一认知分析器（CognitionAnalyzer）

**旧架构的问题**：
```
旧流程： emotion = await analyze_emotion(msg)      # 第1次 LLM 调用
         intent = await analyze_intent(msg, emotion) # 第2次 LLM 调用
```
- 两次异步调用增加延迟
- 两次独立规则引擎重复扫描文本
- 两次独立 LLM fallback（2× token 成本）

**新架构的改进**：
```
新流程： emotion, intent, empathy = await analyze(msg)  # 单次联合调用
```

#### 三层内部架构

**第一层：联合规则引擎（零 LLM 成本，~90% 命中率）**

同时扫描消息文本，一次性完成：
- 情感词典匹配 → valence / arousal / intensity
- 意图模式匹配 → social_intent / subtype
- 紧急度关键词 → urgency_score
- 12维指向性信号 → directed_score
- 讽刺检测 → sarcasm_score
- 资格感判断 → entitlement_score

**第二层：单次 LLM fallback（~10% 命中率）**

当规则引擎置信度不足时，一次性调用轻量模型请求联合 JSON：

```json
{
  "valence": -0.3,
  "arousal": 0.7,
  "intensity": 0.8,
  "basic_emotion": "anger",
  "social_intent": "emotional",
  "intent_subtype": "venting",
  "urgency_score": 65,
  "relevance_score": 0.7,
  "confidence": 0.85,
  "directed_score": 0.75,
  "sarcasm_score": 0.1,
  "entitlement_score": 0.6
}
```

**第三层：上下文融合**

- 情感轨迹：用户最近 5 条情绪的趋势外推
- 群体氛围：EMA 平滑的群体愉悦度
- 助手情绪：从 persona baseline 初始化，受用户情绪影响

#### 2.2.2 12维指向性分析

判断消息是否指向当前 AI，由规则引擎 + LLM 联合推断：

| 维度 | 说明 | 权重 |
|------|------|------|
| `mention_score` | 是否 @ 了 AI | 高 |
| `reference_score` | 是否回复了 AI 的消息 | 高 |
| `name_match_score` | 消息中是否出现 AI 的名字/别名 | 高 |
| `second_person_score` | 第二人称代词密度 | 中 |
| `question_score` | 问句特征 | 中 |
| `imperative_score` | 祈使句特征 | 中 |
| `topic_relevance_score` | 与 AI 擅长话题的重叠度 | 中 |
| `emotional_disclosure_score` | 情感表露强度 | 低 |
| `attention_seeking_score` | 寻求关注的语言标记 | 低 |
| `recency_score` | 与最近对话主题的关联度 | 低 |
| `turn_taking_score` | 轮次交接信号 | 低 |

12 维信号经加权合成 `directed_score`（0~1），≥0.6 视为"被指向"。

#### 2.2.3 讽刺检测

5 类启发式规则并行检测：
1. **正面词 + 负面标点**："真好。" → 句号弱化热情
2. **引号强调**："太棒"了
3. **过度笑声**："哈哈哈哈"伴随负面内容
4. **反讽句式**："我可太喜欢了"用于抱怨场景
5. **emoji-文本矛盾**：😊 + "气死我了"

`sarcasm_score ≥ 0.4` 时，`directed_score` 额外上浮 15%（讽刺通常暗含对 AI 的期待）。

#### 2.2.4 资格感判断

计算 AI persona 与消息话题的重叠度：
- `entitlement_score < threshold` → 决策阈值 ×1.5（不擅长的话题更克制）
- 例如：一个"程序员" persona 看到"今天天气真好" → entitlement 低 → 更不容易回复

#### 2.2.5 情绪模型（EmotionState）

**2D valence-arousal 坐标**：

```
        高唤醒
           │
    兴奋 ←─┼─→ 愤怒
    (0.7,0.8)  (-0.6,0.8)
           │
低愉悦 ────┼──── 高愉悦
           │
    悲伤 ←─┼─→ 满足
    (-0.5,-0.2) (0.6,-0.1)
           │
        低唤醒
```

- **valence（愉悦度）**：-1（极负面）~ +1（极正面）
- **arousal（唤醒度/紧张度）**：0（平静）~ 1（激动）
- **intensity（强度）**：0~1，表示情绪的明显程度

**19 种基本情绪映射**：

| 情绪 | valence | arousal | 典型触发 |
|------|---------|---------|---------|
| joy | >0.5 | >0.3 | 好消息、被夸奖 |
| anger | <-0.3 | >0.5 | 被冒犯、不公平 |
| sadness | <-0.3 | <0.3 | 失落、告别 |
| fear | <-0.3 | >0.6 | 威胁、未知 |
| disgust | <-0.4 | 0.2~0.6 | 厌恶、反感 |
| surprise | ~0 | >0.5 | 意外信息 |
| trust | >0.4 | 0.2~0.5 | 被倾诉秘密 |
| anticipation | 0.2~0.6 | 0.3~0.6 | 期待、计划 |
| neutral | ~0 | ~0 | 信息陈述 |

#### 2.2.6 意图分析（IntentAnalysisV3）

**目的驱动分类**——不问"这是什么"，而问"对方想要什么"：

| 意图 | 含义 | 典型句式 | urgency 基线 |
|------|------|---------|-------------|
| **help_seeking** | 求助 | "有人知道这个怎么弄吗" | 60 |
| **emotional** | 情感表达 | "今天好烦""太开心了" | 50 |
| **social** | 社交互动 | "哈哈哈""同意" | 20 |
| **silent** | 无明确目的 | "转发了一条新闻" | 10 |

**量化评分**：
- `urgency_score`（0~100）：多快需要回应
- `relevance_score`（0~1）：与当前 AI 角色的相关度
- `confidence`（0~1）：分析置信度

#### 2.2.7 共情策略（EmpathyStrategy）

基于情绪状态自动选择：

| 情绪状态 | 策略 | 行为 |
|---------|------|------|
| valence < -0.5, arousal > 0.7 | **confirm_action** | 先确认感受，再提供行动建议 |
| valence < -0.3 | **cognitive** | 帮助重新理解情境 |
| valence > 0.5 | **share_joy** | 积极回应，放大正面情绪 |
| 其他 | **presence** | 安静陪伴，不过度干预 |

#### 2.2.8 群体情感

**情感轨迹**：跟踪每个用户在一段时间内的情感变化，用于检测情感孤岛（某个用户长时间情绪低落）。

**群体氛围快照（AtmosphereSnapshot）**：
- `group_valence`：群整体愉悦度
- `group_arousal`：群整体活跃度
- `heat_level`：cold / warm / hot / overheated

快照在每条消息处理后更新，存入语义记忆的 `atmosphere_history`（保留最近 1000 条）。

#### 2.2.9 记忆检索

认知层同时触发记忆检索（零 LLM 成本）：
- `BasicMemoryManager.get_context()` → 最近窗口消息
- `DiaryManager.retrieve()` → 相关日记条目（token 预算 aware）

检索结果随情绪/意图一起流入决策层。

---

### 2.3 决策层（Decision）—— 判断与选择

**职责**：纯规则计算，零 LLM 成本，决定"回不回复"和"怎么回复"。

#### 2.3.1 节奏分析（RhythmAnalyzer）

计算四个指标：

| 指标 | 含义 | 取值 |
|------|------|------|
| `heat_level` | 群聊热度 | cold / warm / hot / overheated |
| `pace` | 消息增速趋势 | accelerating / steady / decelerating / silent |
| `topic_stability` | 话题是否稳定 | 0~1 |
| `turn_gap_readiness` | 对话自然转折就绪度 | 0~1 |

**turn_gap_readiness 的影响因素**：
- **提高**：问句结尾、话题转换词、低稳定性、长沉默
- **降低**：消息爆发、连续独白

#### 2.3.2 动态阈值（ThresholdEngine）

```
threshold = base × activity_factor × engagement_factor × time_factor
```

- `base`：基准阈值（默认 ~0.45）
- `activity_factor`：`heat_level` 越热阈值越高（群里刷消息时更谨慎）
- `engagement_factor`：基于用户历史互动率（AI 发言后用户是否定向回应），互动率越高阈值越低
- `time_factor`：深夜阈值更高（不想打扰）

#### 2.3.3 单旋钮活泼度（Expressiveness）

`experience.json` 中的 `expressiveness`（0~1）是行为风格的"主旋钮"，自动推导 8 个内部阈值：

| 阈值 | expressiveness=0 | expressiveness=1 |
|------|-----------------|-----------------|
| `directed_threshold` | 0.8 | 0.4 |
| `cooldown_seconds` | 90 | 5 |
| `sarcasm_boost` | 低 | 高 |

高级用户可用 `overrides` 字典单独覆盖任意阈值。

#### 2.3.4 人格偏移

`reply_frequency` 直接乘在阈值上：
- `high`（话痨）×0.8 — 更容易回复
- `low`（安静）×1.3 — 更谨慎
- `selective`（挑剔）×1.6 — 只回高相关性消息

#### 2.3.5 话题间隙降级

当 `turn_gap_readiness < gap_readiness_threshold` 且消息未被强指向时：
- **IMMEDIATE → DELAYED**：避免打断自然对话流

#### 2.3.6 表达去重

生成回复后检查与历史回复的字符二元组 Jaccard 相似度：
- 超过阈值 → 追加提示要求 LLM 换说法重试一次
- 仍冗余 → 跳过该回复

#### 2.3.7 other_ai 折扣

当消息发送者为 `other_ai`（群里的其他 AI/Bot）时：
```
directed_score = min(score, score×0.5+0.1)
```
避免 AI 之间过度互聊。

#### 2.3.8 策略选择（ResponseStrategyEngine）

综合 `intent.relevance`、`urgency`、`threshold`、`assistant_emotion` 四个因素：

| 策略 | 触发条件 | 行为 |
|------|---------|------|
| **IMMEDIATE** | urgency ≥ 80 + relevance ≥ 0.7 | 立即生成回复 |
| **DELAYED** | urgency ≥ 50 | 加入延迟队列，等话题间隙 |
| **SILENT** | 低于阈值 | 不回复，仅后台观察 |
| **PROACTIVE** | 由后台触发器决定 | 不回复这条，标记为候选 |

#### 2.3.9 助手情感状态（AssistantEmotionState）

引擎自己也有情绪：
- 从 `persona.emotional_baseline` 初始化
- 受用户情绪影响（用户开心 → 助手轻微愉悦；用户愤怒 → 助手紧张）
- 随时间自然恢复（惯性 + 恢复机制）

---

### 2.4 执行层（Execution）—— 生成与输出

**职责**：按策略生成内容，调用 LLM，解析 SKILL。

#### 2.4.1 IMMEDIATE 流程

```
PromptFactory.assemble_chat()
    │
    ├── [角色剧本]     persona.build_system_prompt()
    ├── [身份识别]     PromptFactory.build_identity_verification()
    ├── [输出规范]     PromptFactory.build_output_spec()
    ├── [当下的感觉]   PromptFactory.build_emotion_context()（用户情绪 + 群体氛围 + 助手自身情绪）
    ├── [共情策略]     confirm_action / cognitive / action / share_joy / presence
    ├── [相关记忆]     PromptFactory.build_memory_context()（基础记忆最近窗口 + 日记检索 top-k）
    ├── [术语表]       glossary_section (GlossaryManager)
    ├── [群体风格]     PromptFactory.build_group_style()（群消息统计 + 互动率反馈 + 长度/温度限制）
    ├── [输出格式]     纯文本回复，可包含内联 [SKILL_CALL: ...]
    └── [消息] xxx
    │
    ▼
StyleAdapter.adapt()（输出 prompt 级指令，不再动态缩减 max_tokens）
    ├── temperature: persona 偏好
    └── length_instruction / tone_instruction（按对话节奏引导模型自主控制长度）
    │
    ▼
ModelRouter.resolve()
    ├── 认知分析 → gpt-4o-mini（便宜、快）
    ├── 回复生成 → gpt-4o（质量好）
    └── urgency > 80 → 升级更强模型，降低 temperature
    │
    ▼
_generate() → provider.generate_async()（全链路异步 httpx）
    ├── 估算 token 用量
    └── 记录 TokenUsageRecord
    │
    ▼
_process_skill_calls()
    ├── 解析 [SKILL_CALL: ...] 标记
    ├── 剥离标记，得到干净回复
    └── 执行 SKILL，追加结果
```

#### 2.4.2 DELAYED 流程

- 把消息元数据加入 `DelayedResponseQueue`
- 后台 ticker（每 3 秒）检查话题间隙
- 当 `turn_gap_readiness` 足够高时，触发延迟回复生成

#### 2.4.3 PROACTIVE 流程

- 后台 checker（每 60 秒）检查沉默过久的群聊
- `ProactiveTrigger` 判定条件满足时生成主动发言
- 主动话题从 `SemanticMemoryManager` 的 `interest_topics` 中选取
- **间隙感知**：`turn_gap_readiness < proactive_gap_threshold` 时不触发

---

## 第三章：后台任务与事件系统

### 3.1 后台任务

引擎启动后创建 6 个后台 `asyncio.Task`：

| 任务 | 间隔 | 职责 |
|------|------|------|
| **延迟队列 ticker** | 3 秒（由 bridge 驱动） | 扫描所有群聊的延迟队列，检测话题间隙 |
| **主动触发 checker** | 60 秒 | 检查沉默群聊，决定是否主动开口 |
| **日记生成 promoter** | 可配置 | 冷群检测 → 归档消息 → DiaryGenerator 生成日记 |
| **日记 consolidator** | 可配置 | 合并相似日记条目，减少冗余 |
| **开发者主动私聊 checker** | 可配置 | 检查开发者私聊的主动记忆对话触发 |
| **提醒检查器** | 10 秒 | 扫描到期提醒，生成人格化提醒消息 |

生命周期由引擎自己管理：`start_background_tasks()` / `stop_background_tasks()`

### 3.2 事件总线（Event Bus）

引擎在处理每条消息时发射 4 个认知事件：

```
PERCEPTION_COMPLETED → COGNITION_COMPLETED → DECISION_COMPLETED → EXECUTION_COMPLETED
```

外加 3 个后台事件：
```
DELAYED_RESPONSE_TRIGGERED → PROACTIVE_RESPONSE_TRIGGERED → REMINDER_TRIGGERED
```

外部可通过 `SessionEventBus.subscribe()` 拿到 `AsyncIterator` 实时监听。**有损广播**——队列满后事件丢弃，不阻塞引擎。

### 3.3 状态持久化

`save_state()` 持久化到 `{work_path}/memory/`：

| 文件 | 内容 |
|------|------|
| `basic_state.json` | 各群聊基础记忆窗口 |
| `diary_state.json` | 日记索引状态 |
| `assistant_emotion.json` | 助手自身情感状态 |
| `group_timestamps.json` | 群聊活跃度时间戳 |
| `token_usage_records.json` | token 用量统计 |
| `proactive_state.json` | 主动发言启用/禁用状态 |

---

## 第四章：使用方式

### 4.1 快速启动

```python
from sirius_chat import create_emotional_engine
from sirius_chat.core.persona_generator import PersonaGenerator

engine = create_emotional_engine(
    work_path="/path/to/workspace",
    provider=provider,
    persona=PersonaGenerator.from_template("sarcastic_techie"),
    config={
        "sensitivity": 0.6,
        "proactive_silence_minutes": 20,
        "basic_memory_hard_limit": 30,
        "basic_memory_context_window": 5,
    },
)
engine.start_background_tasks()

# 处理消息
result = await engine.process_message(
    message=Message(role="human", content="今天工作好累"),
    group_id="g123",
    participants=[...],
)
# result["reply"] 为 None 表示决定不回复
```

### 4.2 直接使用认知分析器

```python
from sirius_chat.core.cognition import CognitionAnalyzer

analyzer = CognitionAnalyzer()

# 直接调用（零成本规则引擎，复杂情况自动 LLM fallback）
emotion, intent, empathy = await analyzer.analyze(
    "今天工作好累，好想辞职", user_id="u1", group_id="g1"
)

print(emotion.valence)       # -0.6 (负面)
print(emotion.arousal)       # 0.7 (高唤醒)
print(intent.social_intent)  # emotional
print(empathy.strategy_type) # confirm_action
```

### 4.3 事件监听

```python
from sirius_chat.core.events import SessionEventType

async for event in engine.event_bus.subscribe():
    if event.type == SessionEventType.EXECUTION_COMPLETED:
        print(f"回复: {event.data.get('reply')}")
    elif event.type == SessionEventType.DECISION_COMPLETED:
        print(f"策略: {event.data.get('strategy')}")
```
