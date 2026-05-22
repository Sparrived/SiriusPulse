# Persona System（人格系统）

> 为 EmotionalGroupChatEngine 提供可配置、可持久化的角色人格。

## 设计原则

### 1. 不替用户决定角色的上位背景

系统**不会**在 prompt 里写"这是一个真实的人"，也**不会**写"你是一个 AI 助手"。角色的本质是什么——是真人、是虚拟角色、是 AI、是外星人、是猫——**完全由用户决定**。

`PersonaProfile` 只提供**角色信息**（名字、性格、说话方式、口头禅），不提供**元设定**（"你是什么"）。如果用户想让角色认为自己是猫，`full_system_prompt` 字段可以完全覆盖，系统不会干扰。

### 2. 非指令式的角色描述

传统的角色扮演 prompt 是这样写的：

```
你是小明，性格活泼开朗，说话幽默风趣。你的口头禅是"哈哈"。
```

这是**上位视角**——从外部给角色贴标签，模型进入的是"扮演"模式。效果通常生硬、脸谱化。

Persona 系统采用**中性描述**的方式。`build_system_prompt()` 不再用"你是 XXX"的指令式表述，而是直接呈现角色信息：

```
[小明]
一个嘴毒但靠谱的程序员，喜欢用技术梗调侃，关键时刻却总能一针见血地解决问题。
小明平时给人的印象是毒舌、机智、直率、逻辑强、外冷内热。
说话方式：简洁直接。
性子偏沉稳。
口头禅："这很简单"、"显然是..."、"懂我意思吧"。
个人原则：不聊废话；不解释基础概念超过两次。
在圈子里通常是个 observer。
```

这种方式不给模型下指令，而是提供**角色素材**。模型通过阅读理解来呈现这个角色，至于它"觉得自己是什么"——由用户的上位设定决定。

### 3. 行为重于标签

人格不是一堆形容词。`PersonaProfile` 的设计强调**可观察的行为模式**：

- `catchphrases` —— 这个人平时嘴里挂着什么词
- `speech_rhythm` —— 说话是快是慢，是短句还是长句
- `stress_response` —— 压力下会退缩、爆发、还是讲笑话
- `boundaries` —— 什么话题会让他不舒服

这些比"性格：开朗"更有信息量。

## 核心数据模型

`PersonaProfile` 包含以下维度：

| 维度 | 字段 | 影响范围 |
|------|------|----------|
| **Identity** | `name`, `aliases`, `persona_summary`, `backstory` | 系统提示词中的自我认知 |
| **Personality** | `personality_traits`, `core_values`, `flaws`, `motivations` | 角色深度 |
| **Expression** | `communication_style`, `speech_rhythm`, `catchphrases`, `emoji_preference`, `humor_style`, `typical_greetings`, `typical_signoffs` | 说话的外在特征 |
| **Emotional** | `emotional_baseline`, `emotional_range`, `empathy_style`, `stress_response` | 内在情绪模式、共情习惯 |
| **Behavior** | `boundaries`, `taboo_topics`, `preferred_topics`, `social_role` | 社交行为边界 |
| **Runtime** | `max_tokens_preference`, `temperature_preference`, `reply_frequency` | 生成参数、决策阈值 |

`backstory` 字段现在会被**完整注入**系统提示词的【背景故事】区块（v1.0.1+），不再只使用第一句。这让人格的完整背景叙事能被模型充分理解。

## 三条生成路径

| 路径 | 成本 | 丰富度 | 适用场景 |
|------|------|--------|----------|
| **Template（模板）** | 零 | 中等 | 快速启动、无 API key |
| **Keyword（关键词）** | 低 | 中-高 | 用几个词快速定制 |
| **Interview（访谈）** | 高 | 极高 | 深度角色设计 |

## 内置模板

```python
from sirius_pulse.core.persona_generator import PersonaGenerator

# 零成本创建
p = PersonaGenerator.from_template("sarcastic_techie")
```

| 模板 ID | 名称 | 画像 |
|---------|------|------|
| `warm_friend` | 小暖 | 温暖包容，群里气氛低沉时会出来缓和 |
| `sarcastic_techie` | 码叔 | 嘴毒但靠谱的程序员，关键时刻一针见血 |
| `gentle_caregiver` | 阿宁 | 温柔的大姐姐式存在，善于倾听 |
| `chaotic_jester` | 闹闹 | 精力过剩的开心果，群里冷场第一个跳出来 |
| `stoic_observer` | 静观 | 沉默寡言，但每次开口都言之有物 |
| `protective_elder` | 老周 | 阅历丰富的长辈，喜欢分享人生经验 |

## 关键词驱动生成

```python
p = PersonaGenerator.from_keywords(
    name="测试",
    trait_keywords=["毒舌", "程序员", "乐观"],
)
# 自动映射：毒舌→sarcastic humor，程序员→concise/observer，乐观→valence+0.6
```

可选传入 `provider_async` 进行 LLM 精炼，自动补全背景故事和口头禅。

## 访谈式生成

```python
answers = {
    "1": "毒舌、机智、外冷内热",
    "2": "看热闹，偶尔插一句精准吐槽",
    "3": "简短，带技术梗",
    "4": "不解释基础概念超过两次",
    "5": "丢一个解决方案过去，然后说'这很简单'",
    "6": "旁观者",
    "7": "这很简单",
    "8": "很少",
}

p = await PersonaGenerator.from_interview(
    name="码叔",
    answers=answers,
    provider_async=provider,
)
```

需要 LLM provider，输出最丰富的角色设定。

## Roleplay 预设桥接

已有的 `AgentPreset` 可以一键转换为 `PersonaProfile`：

```python
from sirius_pulse.core.persona_generator import PersonaGenerator

preset = load_your_preset()  # AgentPreset
persona = PersonaGenerator.from_roleplay_preset(preset)
```

`EngineRuntime` 会自动尝试从人格目录的 `persona.json` 加载 persona（如果未显式传入）。

## 与 EmotionalGroupChatEngine 集成

### 自动加载与持久化

引擎初始化时自动查找 `{work_path}/engine_state/persona.json`：
- 找到 → 加载
- 未找到 → 创建 `warm_friend` 默认人格并持久化

```python
from sirius_pulse import create_emotional_engine

# 使用默认（自动加载或创建 warm_friend）
engine = create_emotional_engine(work_path)

# 显式指定模板
from sirius_pulse.core.persona_generator import PersonaGenerator
engine = create_emotional_engine(
    work_path,
    persona=PersonaGenerator.from_template("chaotic_jester"),
)
```

### Prompt 中的呈现

有 persona 时，系统提示词直接呈现角色信息（含完整 backstory）：

```
[码叔]
一个嘴毒但靠谱的程序员，喜欢用技术梗调侃，关键时刻却总能一针见血地解决问题。

【背景故事】
码叔从小在程序员堆里长大，父亲是早期互联网工程师，母亲是数学老师。高中时因为帮同学写外挂被封了三个游戏账号，从此发誓只写"正经代码"。大学毕业后在三家 startup 待过，最后一家倒闭时他负责的核心模块被大厂收购，成了技术总监。但他说自己"本质上还是个写脚本的"。

【人格底色】
毒舌、机智、直率、逻辑强、外冷内热。
骨子里看重效率、真诚、技术洁癖。
缺点也明显：不耐烦、过度理性、社交懒。

【情绪反应】
平时情绪平稳，不会因为小事大起大落；遇到刺激反应很快，容易激动；压力大的时候会写长文吐槽。

【关系模式】
在群里像个observer；原则：不聊废话；不解释基础概念超过两次。

【说话方式】
说话concise；性子偏沉稳；口头禅："这很简单"、"显然是..."、"懂我意思吧"；幽默风格偏sarcastic。

【回应习惯】
看到感兴趣的话题才接话；聊到技术债、架构设计会特别来劲。

【场景行为】
你在一个多人聊天场景里，会收到其他人的消息。不需要每条都回，按自己的性格和当下的情绪决定是否开口。回应时用自己的说话方式和口头禅，不要刻意解释或总结。
```

没有 persona 时，fallback 提示词是：

```
[关于你]
你的身份和背景由角色设定决定。
```

没有任何关于"真实/虚拟/AI"的预设。用户可以通过 `full_system_prompt` 完全覆盖，定义角色的上位背景。

### 决策层影响

`reply_frequency` 直接调整 `_decision()` 的回复阈值：

| `reply_frequency` | 阈值倍率 | 效果 |
|-------------------|----------|------|
| `high` | ×0.8 | 话痨，更容易接话 |
| `moderate` | ×1.0 | 默认 |
| `low` | ×1.3 | 安静，更谨慎 |
| `selective` | ×1.6 | 只回感兴趣的话题 |

### 执行层影响

`StyleAdapter` 读取 persona 的以下字段调整输出：
- `max_tokens_preference` → 覆盖默认 max_tokens
- `temperature_preference` → 覆盖默认 temperature
- `communication_style` → 调整 length_instruction

## CLI 使用

```bash
# 启动 emotional 引擎并指定人格
python main.py --engine emotional --persona sarcastic_techie

# 其他内置模板
python main.py --engine emotional --persona chaotic_jester
python main.py --engine emotional --persona gentle_caregiver
```

## 持久化格式

`{work_path}/engine_state/persona.json`：

```json
{
  "name": "码叔",
  "personality_traits": ["毒舌", "机智", "逻辑强"],
  "communication_style": "concise",
  "emotional_baseline": {"valence": 0.1, "arousal": 0.4},
  "reply_frequency": "moderate",
  "source": "template",
  "version": "1.0",
  "created_at": "2026-04-17T19:49:10+08:00"
}
```

## 默认人格

未配置 persona 的 workspace 自动获得 `warm_friend` 默认人格。`PersonaProfile` 的默认名是"小星"。
