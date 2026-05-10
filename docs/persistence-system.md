# 持久化系统

> **记忆 + 会话 + Token 三层数据持久化** — 让 AI 记得住、存得下、算得清。

## 一句话定位

持久化系统负责三件事：**短期上下文保当下**（记忆系统）、**完整会话可恢复**（会话存储）、**每次调用有账可查**（Token 统计）。三者共享底层存储基础设施，但面向不同时间尺度和查询需求。

---

## 第一章：记忆系统（Memory System）

### 1.1 架构总览

记忆系统让引擎**记得住上下文、回忆得起往事、理解得了关系**。v1.0 采用极简四层模型：

```
┌─────────────────────────────────────────────────────────────┐
│  基础记忆 (BasicMemory)      ──  短期注意力窗口 + 热度计算      │
│  日记记忆 (DiaryMemory)      ──  LLM 生成摘要 + 索引检索        │
│  用户管理 (UserManager)      ──  极简 UserProfile + 群隔离      │
│  名词解释 (GlossaryManager)  ──  AI 自身知识库                  │
└─────────────────────────────────────────────────────────────┘
         ↑
   ContextAssembler ── 将基础记忆 + 日记记忆组装为 OpenAI messages
```

| 模块 | 定位 | 核心能力 |
|------|------|---------|
| **BasicMemoryManager** | 短期上下文 | 按群滑动窗口（硬限制 30，上下文窗口 5），热度计算，归档 |
| **DiaryManager** | 长期摘要 | LLM 生成群聊日记，关键词/嵌入索引，token 预算检索 |
| **UserManager** | 身份管理 | 极简 `UserProfile`，群隔离存储，跨平台身份追踪 |
| **GlossaryManager** | 知识库 | 名词解释，由 `learn_term` SKILL 写入 |
| **ContextAssembler** | 上下文组装 | 将基础记忆 + 日记检索组装为标准 OpenAI messages |
| **IdentityResolver** | 身份解析 | 解耦平台特定身份（QQ/discord 等），多级解析 |

### 1.2 基础记忆（Basic Memory）

**定位**：短期注意力窗口，纯粹内存中的热数据。

每个群聊有自己独立的窗口：
- **硬限制**：30 条（`HARD_LIMIT = 30`）
- **上下文窗口**：5 条（`CONTEXT_WINDOW = 5`），直接用于 prompt
- **热度计算**：`RhythmAnalyzer` 基于消息速率、独特发言者、最近度计算群体热度（0~1）

**热度计算**：
```
heat = message_rate_factor × unique_speakers_factor × recency_factor
```

**冷群检测**：`heat < 0.25` 且沉默 > 300 秒 → 归档为日记素材。

**持久化**：`memory/basic/<group_id>.jsonl`

### 1.3 日记记忆（Diary Memory）

**定位**：LLM 生成的群聊摘要，连接短期上下文与长期认知。

**生成流程**：
```
基础记忆归档（冷群消息）
    │
    ▼
DiaryGenerator.generate(group_id, candidates, persona, provider)
    │  （构建 prompt：persona + 消息列表）
    ▼
LLM 返回 JSON：content / keywords / summary / source_ids
    │
    ▼
DiaryManager.add_entry(entry) → DiaryIndexer.add(entry)
    │
    ▼
persistent to memory/diary/<group_id>.jsonl
index to memory/diary/index/<group_id>.json
```

**检索流程**：
```
query (当前消息内容)
    │
    ▼
DiaryIndexer.search(query, top_k=5)
    ├─ 关键词匹配（始终可用）
    └─ 嵌入余弦相似度（通过 EmbeddingClient 调用远程 Embedding 服务）
    │
    ▼
DiaryRetriever.retrieve(query, group_id, top_k=5, max_tokens_budget=800)
    │  （按相关性排序，然后按 token 预算截断）
    ▼
返回 DiaryEntry 列表 → 注入 system_prompt
```

> **v1.1 变更**：DiaryIndexer 和 StickerIndexer 不再依赖本地 SentenceTransformer 模型，改为通过 `EmbeddingClient` 调用远程 Embedding 微服务。Embedding 服务作为共享基础设施由 `PersonaManager` 在主进程启动，各人格子进程通过 HTTP 调用。服务不可用时引擎启动会直接失败（强依赖，无 fallback）。`StickerLearner` 同步适配远程 Embedding。

### 1.4 用户管理（UserManager）

**定位**：极简身份系统，群隔离存储。

```python
@dataclass(slots=True)
class UserProfile:
    user_id: str
    name: str
    persona: str = ""
    aliases: list[str] = field(default_factory=list)
    identities: dict[str, str] = field(default_factory=dict)  # {platform: uid}
    traits: list[str] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
```

**跨平台追踪**：用户 A 在 QQ 叫 "Alice"，在 Discord 叫 "alice#1234" → `identities = {"qq": "qq_456", "discord": "alice#1234"}` → 通过任意平台 UID 解析到同一 `UserProfile`。

**存储**：`user_memory/groups/<group_id>/<user_id>.json`

### 1.5 名词解释（GlossaryManager）

**定位**：AI 自身知识库。

- **写入**：`learn_term` SKILL 触发时调用 `GlossaryManager.add_term()`
- **读取**：`PromptFactory` 在组装 system_prompt 时注入 glossary 条目
- **持久化**：`memory/glossary/terms.json`

### 1.6 上下文组装（ContextAssembler）

**组装原则**：
- 日记内容放在 `system_prompt` 中，不进入 `messages` 历史
- 基础记忆只保留最近窗口，避免消息数组无限增长
- 日记检索按 token 预算截断（默认 800 tokens ≈ 1200 字符）

---

## 第二章：会话存储（Session Store）

### 2.1 为什么需要它

`EmotionalGroupChatEngine` 在内存中维护当前活跃的对话窗口，但进程重启后内存数据会丢失。会话存储提供：
- **持久化**：消息历史、用户档案、token 记录不因重启而丢失
- **Schema 演进**：新增字段时旧数据仍能加载（默认值回退）
- **并发安全**：SQLite WAL 模式支持多读者
- **后端选择**：JSON 适合便携和版本控制；SQLite 适合大容量和关系查询

### 2.2 Transcript（会话数据模型）

```python
@dataclass
class Transcript:
    messages: list[Message]              # 完整消息历史
    user_memory: UserManager             # 所有参与者的 UserProfile
    reply_runtime: ReplyRuntimeState     # 上次回复时间、冷却计数器等
    session_summary: str                 # 长会话压缩后的摘要
    orchestration_stats: dict            # 编排统计信息
    token_usage_records: list[TokenUsageRecord]  # LLM 调用记录
```

### 2.3 存储后端

**JsonSessionStore**：
- `save()` → `transcript.to_dict()` → `json.dumps` → 原子写入
- `load()` → 读取 JSON → `Transcript.from_dict()` → schema write-back
- 优点：人类可读、便于 git diff；缺点：大文件慢、无索引

**SqliteSessionStore**：
- 10 张表：`_meta`、`session_meta`、`session_messages`、`session_reply_runtime`、`session_user_profiles`、`session_user_runtime`、`session_user_memory_facts`、`session_token_usage_records`...
- WAL 模式 + 外键约束 + Schema 自动创建 + Bulk 事务写入

### 2.4 工厂与路径

```python
SessionStoreFactory.create(
    layout: WorkspaceLayout,
    session_id: str,
    backend: str = "sqlite",   # "json" 或 "sqlite"
) -> SessionStore
```

路径：`data_root/sessions/{session_id}/session_state.db`（或 `.json`）

---

## 第三章：Token 统计与持久化

### 3.1 为什么需要它

运行 AI 角色扮演 bot 需要持续调用 LLM，成本是实际运营问题。Token 系统提供：
- **精确计量**：每次 `provider.generate()` 后记录 prompt + completion token 数
- **多维分析**：按任务类型、模型、人格、群聊分组统计
- **持久化**：SQLite 存储，进程重启不丢失
- **预算感知**：生成前估算 prompt token，避免超出上下文窗口

### 3.2 TokenUsageRecord

```python
@dataclass(slots=True)
class TokenUsageRecord:
    timestamp: float
    actor_id: str              # 人格名称
    task_name: str             # response_generate / cognition_analyze / ...
    model: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    input_chars: int
    output_chars: int
    retry_count: int
    group_id: str | None
    provider_name: str | None
    persona_name: str | None
```

### 3.3 TokenUsageStore（SQLite）

**Schema**：单表 `token_usage` + 8 个索引（session/actor/task/model/timestamp/group/provider/persona）

**核心方法**：
- `add(record)` / `add_many(records)` — 单条/批量插入
- `get_summary()` — 总调用次数、总 token 数
- `get_breakdown_by(column)` — 按维度分组统计
- `get_recent_records(limit)` — 最近 N 条
- `fetch_records(...)` — 多条件过滤查询

**特性**：WAL 模式、NORMAL 同步、Schema 版本自动迁移、纯 SQL 无 ORM。

### 3.4 Token 分析

**内存聚合（usage.py）**：
```python
baseline = build_token_usage_baseline(transcript.token_usage_records)
summary = summarize_token_usage(transcript)
```
`TokenUsageBucket` 按 actor、task、model 三个维度累加。

**SQL 多维分析（analytics.py）**：
```python
baseline = compute_baseline(store, session_id="xxx", task_name="response_generate")
by_task = group_by_task(store, session_id="xxx")
by_model = group_by_model(store, actor_id="月白")
time_series = time_series(store, bucket_seconds=3600)
report = full_report(store)
```

### 3.5 Token 估算

**三层策略**：
```
estimate_tokens(text, model="generic", use_tiktoken=True)
    │
    ├── 优先 tiktoken（若安装且模型支持）→ 精确值
    └── 失败 → fallback 到 heuristic

estimate_tokens_heuristic(text, model="generic")
    ├── CJK 字符：1 token/字符
    ├── 英文单词：~4 字符/token
    └── 其他符号：~4 字符/token
```

---

## 第四章：数据流转全景

```
新消息进来
    │
    ▼
[IdentityResolver.resolve()] → 解析跨平台身份
    │
    ▼
[UserManager.register()] → 注册/更新用户（群隔离）
    │
    ▼
[BasicMemoryManager.add_entry()] → 加入窗口，计算热度
    │
    ▼
[ContextAssembler.build_messages()] → 基础记忆 + 日记检索 → OpenAI messages
    │
    ▼
[LLM 生成回复]
    │
    ▼
[BasicMemoryManager.add_entry()] → 记录 assistant 回复
    │
    ▼
[TokenUsageStore.add()] → 记录 token 消耗
    │
    ▼
后台：[_bg_diary_promoter] → 检查冷群 → DiaryGenerator 生成日记
```

---

## 第五章：存储路径汇总

| 数据 | 路径 | 模块 |
|------|------|------|
| 基础记忆归档 | `memory/basic/<group_id>.jsonl` | BasicMemoryFileStore |
| 日记条目 | `memory/diary/<group_id>.jsonl` | DiaryManager |
| 日记索引 | `memory/diary/index/<group_id>.json` | DiaryIndexer |
| 用户档案 | `user_memory/groups/<group_id>/<user_id>.json` | UserManager |
| 名词解释 | `memory/glossary/terms.json` | GlossaryManager |
| 引擎状态 | `memory/basic_state.json` / `diary_state.json` | save_state() |
| 会话状态 | `sessions/{session_id}/session_state.db` | SqliteSessionStore |
| Token 记录 | `token/token_usage.db` | TokenUsageStore |
