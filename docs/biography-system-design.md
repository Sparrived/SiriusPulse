# 人物传记系统（User Biography System）

> **让 AI 记住"谁是什么样的"** — 人物认知锚点，与日记系统的"发生了什么"互补。

## 一句话定位

现有系统通过**日记检索**让 AI 回忆过去，但日记检索不稳定，导致 AI 经常记错人的关键信息（如"yuki 是谁开发的"）。人物传记系统不靠检索，而是维护一份 **token 预算锁死的浓缩传记**，跟随日记周期自动更新，在每次对话时直接全文注入 prompt。

---

##  问题分析

### 现状痛点

1. **日记检索不可靠**：关于一个人的认知信息分散在数十条日记中，RAG 检索经常漏掉关键锚点
2. **无"锚定事实"机制**：现有 `UserSemanticProfile` 只记录 `engagement_rate` / `interest_graph`，不记录"这个人是谁"的固定认知
3. **别名/代称问题**：群聊中常有各种外号（"临雀"也叫"狗福"），模型无法自动关联
4. **意图分析负担过重**：现有意图分析已经承担了情绪、话题、定向判断，不应再加人物识别

### 核心设计原则

```
日记系统 = "发生了什么" → 按话题检索，适合回忆具体事件
传记系统 = "谁是什么样的" → 直接全文注入，提供人物认知锚点

两者互补，各司其职。
```

**传记是全局的，不分群**。一个人在不同群里的表现、身份、关系，全部收敛到同一张 UserPersonaCard 里。这意味着：

- 群 A 里知道"临雀是程序员"，群 B 里知道"临雀是群主" → 传记两边都能看到
- 日记仍是按群隔离的（那是聊了什么），传记是跨群的（这是谁）
- 消歧时用群上下文加权，但存储和浓缩不按群拆分

---

##  架构总览

```
消息流
  │
  ├── 实时路径（每次消息）
  │     ├── _perception → 别名归一化（字符串匹配，零 LLM）
  │     └── PromptFactory.assemble_chat → 注入「人物速查」section
  │
  └── 周期路径（跟随日记周期）
        │
        _bg_diary_promoter()
          ├── candidates（BasicMemoryEntry 原始数据，完整上下文，不做过滤）
          │
          ├──▶ diary.generate_from_candidates()    [现有]
          │       → DiaryEntry（自然语言日记）
          │
          └──▶ biography._feed_biography_from_candidates()  [新增]
                  ├── 构建全部消息（同日记输入，无预过滤）
                  ├── 每个用户拿到同一份完整群聊上下文
                  ├── feed_messages → 零 LLM 攒批
                  ├── maybe_distill → 层1：LLM 蒸馏关于用户的要点 → distilled_points
                  └── maybe_update_biography → 层2：足量要点 → LLM 重写传记卡
```

### 与日记系统的关系

| 维度 | 日记 | 传记 |
|------|------|------|
| 输入 | candidates（同一批原始数据） | candidates（同一批原始数据） |
| 触发 | 冷群 / 体量阈值 | 跟随日记周期（同一批） |
| 提取 | LLM 生成自然语言日记 | LLM 按人提取结构化信息 |
| 存储 | append 新条目（无限增长） | 覆盖重写（500 字锁死） |
| 检索 | embedding RAG | 直接全文注入 |
| 隔离 | **按群隔离** | **全局一张卡，跨群收敛** |
| 内容 | "谁说了什么" | "谁是什么样的人" |
```

### 两层凝练架构

传记系统采用**两层 LLM 凝练**架构，解决"原始消息多但稀 → 直接重写传记质量差"的问题：

```
群聊原始消息（全部 candidates，不做预过滤）
  │
  ├── feed_messages (零LLM) → pending_messages (截断 ~2000字)
  │
  ├── maybe_distill (触发: >=5条 或 >=8h)
  │     └── LLM: 从群聊中蒸馏"关于 {user} 的关键信息"
  │     └── 输出: {"points": [...], "discovered_aliases": [...]}
  │     └── 存入: card.distilled_points (累积)
  │
  └── maybe_update_biography (触发: >=3条要点 或 >=24h)
        └── LLM: 综合要点重写完整传记卡
        └── 输出: {"short_bio": ..., "identity_anchors": ..., "relationships": [...]}
        └── 清空: card.distilled_points = []
```

**触发参数**：

| 层 | 触发条件 | 输入 | 输出 |
|----|---------|------|------|
| 层1 蒸馏 | `pending_messages >= 5` 或 `>= 8h` | 原始群聊记录 | 最多 5 条要点 |
| 层2 传记更新 | `distilled_points >= 3` 或 `>= 24h` | 累积的蒸馏要点 | UserPersonaCard |

---

##  数据结构

### 1. UserPersonaCard（用户传记卡）

**定位**：每个用户一张卡，直接注入 prompt。不追加，只重写。

```python
@dataclass
class UserPersonaCard:
    user_id: str                              # 统一 user_id（如 "qq_123456"）
    name: str                                 # 主要显示名
    aliases: list[str]                        # 已知别名（合并自 UserProfile + LLM 发现）
    
    # ── 注入层 ──
    identity_anchors: list[str]               # 核心锚点，始终注入
    relationships: list[RelationshipAnchor]   # 与其他人物的关系
    short_bio: str                            # LLM 浓缩传记（500 字预算）
    
    # ── 层1：原始消息攒批（等待蒸馏）──
    pending_messages: list[str]              # 攒的原始消息文本（不超过 ~2000 字）
    pending_message_count: int               # 攒了多少条

    # ── 层2：蒸馏后的要点（等待传记更新）──
    distilled_points: list[str]              # LLM 蒸馏的关于此人的要点
    last_distill_at: str                     # ISO 8601，上次蒸馏时间

    # ── 内部追踪 ──
    last_updated_at: str                     # ISO 8601，上次传记更新
    bio_token_estimate: int                  # short_bio 的 token 估算
    bio_token_budget: int = 500              # 预算上限
```

### 2. RelationshipAnchor（关系锚点）

```python
@dataclass
class RelationshipAnchor:
    target_name: str                          # 对方显示名
    target_user_id: str                       # 对方 user_id（如有）
    relation: str                             # 关系描述，如 "朋友开发的机器人"
    fact_hint: str                            # 事实提示，如 "yuki 是临雀朋友的开发的QQ机器人"
    mentioned_count: int = 1                  # 被提及次数
    last_mentioned_at: str = ""
```

### 4. AliasEntry（别名条目 —— 支持同名消歧）

```python
@dataclass
class AliasEntry:
    user_id: str                              # 指向的用户
    user_name: str                            # 用户主要显示名
    weight: float = 1.0                       # 熟度权重（动态增减）
    groups: list[str] = field(default_factory=list)  # 该别名在哪些群出现过
    mentioned_count: int = 1                  # 被称呼次数
    first_seen_at: str = ""
    last_seen_at: str = ""
    source: str = "napcat"                    # "napcat" | "llm_discovery" | "manual"
```

**别名索引结构**（全局，一对多）：

```python
_alias_index: dict[str, list[AliasEntry]]
# 示例: {"狗福": [AliasEntry(user_id="qq_123456", groups=["群A"], weight=3.2),
#                AliasEntry(user_id="qq_789012", groups=["群A"], weight=1.0)]}
```

---

##  存储布局

```
{persona_dir}/memory/
└── biography/
    ├── qq_123456.json          # UserPersonaCard 完整序列化（全局一张卡）
    ├── qq_789012.json          # ...
    └── index.json              # 全局别名索引（一对多，含权重和群信息）
```

**序列化**：复用现有 `_atomic_write` 模式（临时文件 + replace）。

**传记文件是全局的**：同一个 `user_id` 只有一个 `qq_123456.json`，无论这个人在多少个群里出现过。所有群的观察都累积、浓缩到同一张卡中。

`index.json` 结构（全局，一对多）：

```json
{
  "狗福": [
    {"user_id": "qq_123456", "user_name": "临雀", "weight": 3.2, "groups": ["群A"]},
    {"user_id": "qq_789012", "user_name": "张三", "weight": 1.0, "groups": ["群A"]}
  ],
  "雀雀": [
    {"user_id": "qq_123456", "user_name": "临雀", "weight": 2.0, "groups": ["群A", "群B"]}
  ],
  "yuki": [
    {"user_id": "qq_789012", "user_name": "yuki", "weight": 4.5, "groups": ["群A"]}
  ]
}
```

这个 `index.json` 加载到内存作为别名速查表，消息级匹配零 LLM 开销。查询时传入 `group_id` 进行群上下文过滤。

---

##  核心模块

### 文件清单

| 文件 | 说明 |
|------|------|
| `sirius_chat/memory/biography/__init__.py` | 包入口 |
| `sirius_chat/memory/biography/models.py` | 数据结构定义（UserPersonaCard、RelationshipAnchor、AliasEntry） |
| `sirius_chat/memory/biography/manager.py` | `BiographyManager` 核心逻辑（攒消息 → 蒸馏 → 传记更新，两层凝练） |
| `sirius_chat/memory/biography/store.py` | `BiographyStore` 持久化 |

### BiographyManager

```python
class BiographyManager:
    """管理所有用户的全局传记卡（跨群收敛）。

    职责：
    - 加载/保存全局 UserPersonaCard（一个 user_id 一张卡）
    - 维护全局别名速查表（index.json，一对多）
    - 别名消歧（群上下文 + 权重 + 活跃度）
    - 两层凝练：攒原始消息 → 蒸馏 → 传记更新
    """

    def __init__(self, work_path: Path) -> None:
        self._store = BiographyStore(work_path)
        self._cards: dict[str, UserPersonaCard] = {}   # user_id → 全局卡片
        self._alias_index: dict[str, list[AliasEntry]] = {}  # alias → 候选列表

    # ── 别名速查（消歧版） ──

    def resolve_alias(
        self,
        alias: str,
        *,
        group_id: str = "",
        recent_speakers: list[str] | None = None,
        at_user_id: str | None = None,
    ) -> tuple[str | None, float, list[str]]:
        """别名消歧解析（三层：群过滤 → 上下文 → 兜底）。"""
        ...

    def bump_alias_weight(self, alias: str, user_id: str, group_id: str) -> None:
        """有人用此别名称呼了此人，提升权重，同别名其他候选衰减。"""
        ...

    # ── 两层凝练 ──

    def feed_messages(
        self,
        user_id: str,
        name: str,
        group_id: str,
        messages: list[str],                         # 完整群聊上下文（含所有说话人）
        discovered_aliases: list[str] = [],           # LLM 在此批次中发现的别名
    ) -> None:
        """把一批原始消息追加到 pending_messages 队列。

        - 不调 LLM，不做 embedding，纯文本追加
        - 与日记使用同一批 candidates，不做预过滤
        """
        ...

    # ── 层1：蒸馏 ──

    async def maybe_distill(
        self,
        user_id: str,
        *,
        persona_name: str,
        provider_async: Any,
        model_name: str,
    ) -> bool:
        """如果攒的原始消息足够（>= 5 条或距上次蒸馏 >= 8h），
        调用 LLM 从群聊中蒸馏关于此用户的要点。

        LLM 输出: {"points": [...], "discovered_aliases": [...]}
        存入 card.distilled_points，清空 pending_messages。

        Returns: True 表示蒸馏完成并产生了新要点。
        """
        ...

    # ── 层2：传记更新 ──

    async def maybe_update_biography(
        self,
        user_id: str,
        *,
        persona_name: str,
        provider_async: Any,
        model_name: str,
    ) -> bool:
        """如果蒸馏要点攒够了（>= 3 条或距上次更新 >= 24h），
        调用 LLM 综合要点重写传记卡。

        LLM 输出: {"short_bio": ..., "identity_anchors": ..., "relationships": [...]}
        存入传记卡，清空 distilled_points。

        Returns: True 表示传记被更新了。
        """
        ...

    # ── 查询 ──

    def get_card(self, user_id: str) -> UserPersonaCard | None
    def get_cards_for_users(self, user_ids: list[str]) -> list[UserPersonaCard]
    def load_index(self) -> None:
        """从 index.json 加载别名索引到内存。"""
        ...
```

### 两层凝练流程

**核心思路**：不使用"一步到位"把原始消息直接重写传记——原始消息太多且信息密度低，一次 LLM 调用的质量不可靠。采用两层凝练：先蒸馏为要点，再积累要点后重写传记。

```
candidates（全部对话，不做预过滤）
    │
    ├── feed_messages(user_id, all_messages)   # 每个用户同一份完整上下文，零 LLM
    │       → pending_messages: ["临雀: 我最近在学Rust", "Bob: 临雀代码真好", ...]
    │
    ├── maybe_distill(user_id)                 # 攒够 5 条或 8h 后触发
    │       │
    │       ▼
    │    LLM 蒸馏:
    │      输入: 群聊对话记录（"说话人: 内容"格式）
    │      输出: {"points": ["要点1", ...], "discovered_aliases": [...]}
    │      存入: card.distilled_points (追加)
    │      清空: card.pending_messages
    │
    └── maybe_update_biography(user_id)        # 攒够 3 条要点或 24h 后触发
            │
            ▼
         LLM 重写传记:
           输入: 旧传记 + 旧锚点 + 新蒸馏要点
           输出: {"short_bio": ..., "identity_anchors": ..., "relationships": [...]}
           清空: card.distilled_points
```

### LLM 蒸馏 prompt（层1）

```
你是一个信息提炼助手。以下是一段群聊对话记录，请从中提取关于 {user_name} 的关键信息。

人格名称：{persona_name}

=== 群聊对话记录 ===
{messages}

对话中每条消息都标注了说话人（"说话人: 内容"格式）。请提炼 {user_name} 相关的信息，
每条要点简洁（不超过 40 字），按重要性排列，最多 5 条。

提取角度：
1. {user_name} 自己说的话中透露的自身信息
2. 其他人谈论 {user_name} 时透露的信息（含代称/外号指代）
3. {user_name} 与他人的互动中体现的关系信息

注意：只提取与 {user_name} 相关的内容，忽略不相关的闲聊。

严格输出 JSON：
{"points": ["要点1", "要点2", ...], "discovered_aliases": ["别名1", ...]}
```

### LLM 传记更新 prompt（层2）

```
你是人物传记维护助手。以下是从多段群聊中浓缩的关于 {user_name} 的要点，
请据此更新你对该用户的认知档案。

人格名称：{persona_name}

=== 现有的《{user_name}》档案 ===
短期传记：
{old_short_bio or "（尚无传记）"}

已知锚点：
{old_anchors}

已知关系：
{old_relationships}

=== 近期的认知要点（从群聊蒸馏而来） ===
{points}

请综合旧档案和新要点，输出 {user_name} 的更新后完整档案。注意：
- 如果旧信息与新要点冲突，以新要点为准
- 如果新要点没有涉及旧档案中的某条信息，保留旧信息（除非明显过时）
- 传记不超过 500 字
- 锚点每条不超过 20 字，最多 5 条

严格输出 JSON：
{
  "short_bio": "浓缩传记全文（不超过500字）",
  "identity_anchors": ["锚点1", "锚点2", ...],
  "relationships": [{"target": "对方名", "fact_hint": "事实描述"}, ...]
}
```

### 两层凝练 vs 一步到位

| 维度 | 一步到位方案（旧） | 两层凝练方案（新） |
|------|-------------------|-------------------|
| LLM 调用 | 1 次（原始消息 → 传记） | 2 次（消息 → 要点 → 传记），但每次更轻量 |
| 单次输入量 | N 条原始消息（噪音多） | 蒸馏层 5 条以内要点（高信息密度） |
| 信息遗漏 | LLM 可能被大量闲聊淹没 | 蒸馏层强制提炼关键信息 |
| 触发节奏 | >=8 条消息 或 >=24h | 蒸馏 >=5 条 / 8h，传记 >=3 点 / 24h |
| 成本 | 同等 | 蒸馏 prompt 更短，传记 prompt 信息密度更高 |
| 别名发现 | 传记更新 LLM 顺带 | 蒸馏 LLM 顺带（更早发现新别名） |

**核心洞察**：原始对话中 80% 是无用闲聊，直接让 LLM 重写传记时那些闲聊会挤占注意力。先蒸馏一次筛掉噪音，传记更新的输入是精炼要点的集合，质量更高。

### feed_messages 实现

```python
def feed_messages(
    self,
    user_id: str,
    name: str,
    group_id: str,
    messages: list[str],
    discovered_aliases: list[str] = [],
) -> None:
    """攒消息，零 LLM 零 embedding。"""
    card = self._ensure_card(user_id, name)
    card.name = card.name or name

    # 追加消息（截断到最近 ~2000 字）
    card.pending_messages.extend(messages)
    total_chars = sum(len(m) for m in card.pending_messages)
    while total_chars > 2000 and len(card.pending_messages) > 1:
        card.pending_messages.pop(0)  # 丢弃最老的
        total_chars = sum(len(m) for m in card.pending_messages)
    
    card.pending_message_count += len(messages)
    
    # 注册别名
    for alias in discovered_aliases:
        self._register_alias(alias, user_id, name, group_id, source="llm_discovery")
    
    self._store.save_card(card)
```

### maybe_distill 实现（层1）

```python
async def maybe_distill(
    self,
    user_id: str,
    *,
    persona_name: str,
    provider_async: Any,
    model_name: str,
) -> bool:
    """蒸馏：从原始群聊消息提炼关于此用户的要点。"""
    card = self._ensure_card(user_id)
    if not card.pending_messages:
        return False

    # 触发条件
    should_distill = (
        len(card.pending_messages) >= 5
        or (
            card.last_distill_at
            and self._hours_since(card.last_distill_at) >= 8
        )
    )
    if not should_distill:
        return False

    prompt = _build_distill_prompt(
        user_name=card.name,
        persona_name=persona_name,
        messages=card.pending_messages,
    )
    request = GenerationRequest(
        model=model_name,
        system_prompt="你是信息提炼助手。严格输出 JSON。",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.3,
        max_tokens=1024,
        purpose="biography_distill",
    )
    raw = await provider_async.generate_async(request)
    result = json.loads(raw)

    # 蒸馏要点追加到 distilled_points
    new_points = [str(p).strip() for p in result.get("points", []) if p]
    card.distilled_points.extend(new_points)
    card.pending_messages = []
    card.last_distill_at = now_iso()

    # 注册蒸馏发现的别名
    for alias in result.get("discovered_aliases", []):
        self._register_alias(alias, user_id, card.name, source="llm_discovery")

    self._store.save_card(card)
    return bool(new_points)
```

### maybe_update_biography 实现（层2）

```python
async def maybe_update_biography(
    self,
    user_id: str,
    *,
    persona_name: str,
    provider_async: Any,
    model_name: str,
) -> bool:
    """综合蒸馏要点重写传记卡。"""
    card = self._ensure_card(user_id)
    if not card.distilled_points:
        return False
    
    # 触发条件
    should_update = (
        len(card.distilled_points) >= 3
        or (
            card.last_updated_at
            and self._hours_since(card.last_updated_at) >= 24
        )
    )
    if not should_update:
        return False
    
    prompt = _build_update_prompt(
        user_name=card.name,
        persona_name=persona_name,
        old_bio=card.short_bio,
        old_anchors=card.identity_anchors,
        old_relationships=card.relationships,
        points=card.distilled_points,
    )
    request = GenerationRequest(
        model=model_name,
        system_prompt="你是人物传记维护助手。严格输出 JSON。",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.4,
        max_tokens=1024,
        purpose="biography_update",
    )
    raw = await provider_async.generate_async(request)
    result = json.loads(raw)
    
    # 更新传记卡
    card.short_bio = result.get("short_bio", card.short_bio)[:card.bio_token_budget * 4]
    card.identity_anchors = result.get("identity_anchors", [])[:5]
    card.relationships = [
        RelationshipAnchor(
            target_name=r.get("target", ""),
            fact_hint=r.get("fact_hint", ""),
        )
        for r in result.get("relationships", [])[:5]
    ]
    card.distilled_points = []
    card.last_updated_at = now_iso()
    
    self._store.save_card(card)
    return True
```

**使用 `memory_extract` 模型（gpt-4o-mini），温度：蒸馏 0.3 / 传记重写 0.4**。蒸馏每次只提炼 5 条以内要点，成本很低。传记重写的输入是高密度要点，质量远高于直接从原始消息重写。

---

##  别名消歧与归一化系统

### 问题场景

```
群聊中：
  临雀 (qq_123456) → 别名: [狗福, 雀雀]
  张三 (qq_789012)  → 别名: [狗福, 三哥]    ← 冲突！

旧: _alias_index = {"狗福": qq_789012}  ← 后注册覆盖，临雀的"狗福"丢失
新: _alias_index = {"狗福": [AliasEntry(临雀), AliasEntry(张三)]}  ← 一对多，保留冲突
```

### 别名来源（多层收集）

| 来源 | 方式 | 更新频率 |
|------|------|---------|
| QQ 群名片（card） | NapCatAdapter 解析时提取 | 每次消息 |
| QQ 昵称（nickname） | NapCatAdapter 解析时提取 | 每次消息 |
| UserProfile.aliases | IdentityResolver 合并 | 每次消息 |
| LLM 发现 | BiographyGenerator 提取（标 `source_group_id`） | 跟随日记周期 |
| 手动标注 | WebUI 管理界面 | 按需 |

### 三层消歧策略

```
消息: "狗福你代码又写崩了"（当前群=群A）
    │
    ▼
[L1] 群内精确匹配
  _alias_index["狗福"] → [AliasEntry(qq_123456, groups=["群A"], weight=3.2),
                          AliasEntry(qq_789012, groups=["群A"], weight=1.0)]
  按 group_id 过滤 → 两个候选都在群A
    │
    ├── 只有一人 → 直接命中，confidence=0.95 ✓
    └── 多人冲突 → 进入 L2
          │
          ▼
[L2] 上下文消歧（优先度递减）
  # 信号1: @ 锚定（最强）
    if at_user_id in [c.user_id for c in candidates]:
        return at_user_id, confidence=0.98

  # 信号2: 最近活跃者（5分钟内刚发过言）
    if speaker in candidates:
        return speaker, confidence=0.75

  # 信号3: 权重差距
    if best_weight > second_best_weight * 1.5:
        return best.user_id, confidence=0.6
          │
    ├── 命中 → 返回，confidence=0.6~0.98 ✓
    └── 仍无法确定 → 进入 L3
          │
          ▼
[L3] 安全兜底
  ─ 不强行绑定，返回 None + 所有候选 user_id
  ─ PromptFactory 注入所有人的 identity_anchors
  ─ 标注消歧提示让模型自己根据上下文判断
```

### 别名权重更新

权重不是固定的——用得多就涨，不用就衰减：

```python
def bump_alias_weight(self, alias: str, user_id: str, group_id: str) -> None:
    """有人用这个别名称呼了此人。"""
    entries = self._alias_index.get(alias.lower(), [])
    
    for entry in entries:
        if entry.user_id == user_id:
            entry.mentioned_count += 1
            entry.weight = min(10.0, entry.weight + 0.3)  # 指数增长
            entry.last_seen_at = now_iso()
            if group_id not in entry.groups:
                entry.groups.append(group_id)
    
    # 同别名的其他候选权重微衰减
    for entry in entries:
        if entry.user_id != user_id:
            entry.weight = max(0.5, entry.weight * 0.98)
```

### Prompt 消歧注入

当消歧结果 confidence < 0.7 时，在「人物速查」中注入消歧提示：

```
【人物速查】
关于临雀（别称：狗福、雀雀）：
  临雀是群主，26岁程序员。

关于张三（别称：狗福、三哥）：
  张三是新来的实习生，喜欢打游戏。

【注意】消息中的"狗福"可能指临雀或张三，请根据上下文判断。
```

模型同时看到两个人的信息，结合上下文（"代码又写崩了"→ 程序员 → 临雀），可以自行消歧。

### 在 pipeline 中的使用

```python
# 在 pipeline.py:_perception 末尾
def _collect_mentioned_users(self, message_content: str, group_id: str,
                              at_segments: list[str] | None = None,
                              recent_speakers: list[str] | None = None) -> dict[str, float]:
    """收集消息中提到的用户及置信度。

    Returns:
        {user_id: confidence}  — 高置信的直接注入，低置信的带消歧提示
    """
    mentioned: dict[str, float] = {}
    text = message_content or ""
    
    # @ 提及
    if at_segments:
        for at_text in at_segments:
            uid, conf, _ = self.biography_manager.resolve_alias(
                at_text, group_id=group_id
            )
            if uid:
                mentioned[uid] = max(mentioned.get(uid, 0), conf)

    # 文本别名精确匹配
    for alias, entries in self.biography_manager._alias_index.items():
        if len(alias) >= 2 and alias in text:
            uid, conf, _ = self.biography_manager.resolve_alias(
                alias, group_id=group_id,
                recent_speakers=recent_speakers,
                at_user_id=list(mentioned.keys())[0] if mentioned else None,
            )
            if uid:
                mentioned[uid] = max(mentioned.get(uid, 0), conf)
            elif conf == 0:  # L3 兜底：无法确定，保留所有候选
                for entry in entries:
                    if group_id in entry.groups:
                        mentioned[entry.user_id] = 0.0  # 零置信 = 消歧提示
    
    return mentioned
```

---

##  Prompt 注入

### 注入位置

在 [PromptFactory.assemble_chat()](file:///d:/Code/sirius_chat/sirius_chat/core/prompt_factory.py#L788) 的 sections 序列中，在 `persona_prompt` 之后、`identity_verification` 之前插入：

```python
# 新增：人物速查
biography_section = PromptFactory.build_biography_section(
    speaker_card=speaker_card,             # 当前发言者传记
    mentioned_cards=mentioned_cards,       # 被提及者的传记
    all_aliases=all_aliases,               # 别名速查表
)
if biography_section:
    _add(biography_section, "identity")
```

### 注入策略

| 场景 | 注入谁 | 注什么 | Token 预算 |
|------|--------|--------|-----------|
| 即时回复 | 当前发言者 | identity_anchors + short_bio | ~600 |
| 即时回复 | 被 @ 的人 | identity_anchors | ~100/人 |
| 即时回复 | 文本别名命中的 | identity_anchors | ~100/人 |
| 即时回复 | 别名消歧 L3 兜底 | 所有候选的 identity_anchors + 消歧提示 | ~200 |
| 延迟回复 | 批次中所有发言人 | identity_anchors | ~100/人 |
| 主动发言 | 全群 | 别名速查表（仅 alias→name） | ~200 |

> 注：传记是全局的，不存在"跨群感知"和"当前群感知"的二分。同一张卡在任意群注入的内容都一样。

### 注入格式

```
【人物速查】
关于临雀（别称：狗福、雀雀）：
  临雀是群主，26岁程序员。最近在学Rust。
  yuki是临雀的朋友小明开发的QQ机器人，不是临雀开发的。
  临雀不喜欢加班，经常吐槽老板。

关于yuki（别称：小y）：
  yuki是QQ机器人，临雀的朋友小明开发。
  yuki擅长天气预报和定时提醒。
```

### 新增 PromptFactory 静态方法

```python
class PromptFactory:
    TAG_BIOGRAPHY = "【人物速查】"

    @staticmethod
    def build_biography_section(
        *,
        speaker_card: UserPersonaCard | None = None,
        mentioned_cards: list[UserPersonaCard] | None = None,
        all_aliases: dict[str, str] | None = None,          # alias → user_name
        confidence: dict[str, float] | None = None,          # user_id → confidence
    ) -> str | None:
        """构建人物传记 section。
        
        confidence 中值为 0.0 的表示消歧无法确定，需要加消歧提示。
        """
        lines: list[str] = [PromptFactory.TAG_BIOGRAPHY]
        written: set[str] = set()
        low_confidence_users: list[str] = []        # 需要消歧提示的用户

        def _write_card(card: UserPersonaCard, conf: float = 1.0) -> None:
            if card.user_id in written:
                return
            written.add(card.user_id)
            
            if conf <= 0.0:
                low_confidence_users.append(card.name)
            
            alias_hint = ""
            if card.aliases:
                alias_hint = f"（别称：{'、'.join(card.aliases[:4])}）"
            lines.append(f"关于{card.name}{alias_hint}：")
            
            for anchor in card.identity_anchors[:5]:
                lines.append(f"  {anchor}")
            
            for rel in card.relationships[:3]:
                lines.append(f"  {rel.fact_hint}")

        if speaker_card:
            _write_card(speaker_card, confidence.get(speaker_card.user_id, 1.0) if confidence else 1.0)

        if mentioned_cards:
            for card in mentioned_cards:
                _write_card(card, confidence.get(card.user_id, 1.0) if confidence else 1.0)
        
        # 消歧提示
        if low_confidence_users:
            names = "、".join(low_confidence_users)
            lines.append(f"【注意】消息中提到的别名可能指{names}中的一位，请根据上下文判断。")

        if len(lines) == 1:
            return None
        return "\n".join(lines)
```

---

##  bg_tasks 集成

### _bg_diary_promoter 修改

在现有 [bg_tasks.py:_bg_diary_promoter](file:///d:/Code/sirius_chat/sirius_chat/core/bg_tasks.py#L157-L234) 的日记生成成功后，新增传记提取：

```python
# 在现有 _bg_diary_promoter 中，result 成功后插入：

if result:
    promoted_total += 1
    # ... 现有 semantic_memory 更新 ...

    # ── 新增：传记攒消息 + 两层凝练 ──
    if self.biography_manager is not None:
        try:
            await self._feed_biography_from_candidates(
                group_id, candidates, cfg.model_name
            )
        except Exception as exc:
            logger.warning("传记提取失败: %s", exc)
```

### 新增辅助方法

```python
async def _feed_biography_from_candidates(
    self,
    group_id: str,
    candidates: list[BasicMemoryEntry],
    model_name: str,
) -> None:
    """从日记候选消息中攒消息到各自传记。

    与日记一致：使用全部 candidates，不做预过滤。每个用户都拿到
    完整对话上下文，LLM 在蒸馏/更新传记时自行提取相关信息。
    """

    # 1. 构建全部消息（与日记输入相同，不做预过滤）
    all_messages: list[str] = []
    user_ids: set[str] = set()
    user_name_map: dict[str, str] = {}
    for entry in candidates:
        uid = entry.user_id
        if uid in ("assistant", "system", ""):
            continue
        speaker = entry.speaker_name or uid
        all_messages.append(f"{speaker}: {entry.content}")
        user_ids.add(uid)
        if uid not in user_name_map:
            user_name_map[uid] = speaker

    if not user_ids or self.biography_manager is None:
        return

    # 2. 每个用户拿到同一份完整上下文，零 LLM 攒批
    for user_id in user_ids:
        user_name = user_name_map.get(user_id, user_id)
        try:
            self.biography_manager.feed_messages(
                user_id=user_id,
                name=user_name,
                group_id=group_id,
                messages=all_messages,
            )
        except Exception as exc:
            logger.warning("传记攒消息失败 user=%s: %s", user_id, exc)

    # 3. 层1：蒸馏 → 从原始消息提炼关于各用户的要点
    for user_id in user_ids:
        try:
            distilled = await self.biography_manager.maybe_distill(
                user_id=user_id,
                persona_name=self.persona.name,
                provider_async=self.provider_async,
                model_name=model_name,
            )
            if distilled:
                self._record_subtask_tokens(
                    task_name="biography_distill",
                    model_name=model_name,
                    group_id=group_id,
                )
        except Exception as exc:
            logger.warning("传记蒸馏失败 user=%s: %s", user_id, exc)

    # 4. 层2：传记更新 → 从蒸馏要点构建传记卡
    for user_id in user_ids:
        try:
            updated = await self.biography_manager.maybe_update_biography(
                user_id=user_id,
                persona_name=self.persona.name,
                provider_async=self.provider_async,
                model_name=model_name,
            )
            if updated:
                logger.info("传记已更新: user=%s", user_id)
                self._record_subtask_tokens(
                    task_name="biography_update",
                    model_name=model_name,
                    group_id=group_id,
                )
        except Exception as exc:
            logger.warning("传记更新失败 user=%s: %s", user_id, exc)
```

---

##  engine_core 集成

### 构造函数新增成员

在 [engine_core.py:__init__](file:///d:/Code/sirius_chat/sirius_chat/core/engine_core.py#L199-L205) 的 memory foundation 区域新增：

```python
# 在 diary_manager = DiaryManager(...) 之后新增：
from sirius_chat.memory.biography.manager import BiographyManager

self.biography_manager = BiographyManager(work_path)
```

### pipeline.py 新增方法

在 [pipeline.py](file:///d:/Code/sirius_chat/sirius_chat/core/pipeline.py) 中新增：

```python
def _collect_biography_section(
    self,
    group_id: str,
    user_id: str,
    message_content: str,
    at_segments: list[str] | None = None,
    recent_speakers: list[str] | None = None,
) -> tuple[UserPersonaCard | None, list[UserPersonaCard], dict[str, float], dict[str, str]]:
    """收集人物传记信息（全局卡 + 群上下文消歧）。

    Returns:
        speaker_card: 当前发言者全局传记
        mentioned_cards: 被提及者的全局传记列表（含消歧结果）
        mentioned_confidence: {user_id: confidence}（用于 PromptFactory 判断是否加消歧提示）
        all_aliases: 当前群相关的别名速查表（alias → name，仅当前群有记录的别名）
    """
    if self.biography_manager is None:
        return None, [], {}, {}
    
    # 1. 当前发言者全局传记
    speaker_card = self.biography_manager.get_card(user_id) if user_id else None
    
    # 2. 被提及者：走消歧流程
    mentioned = self._collect_mentioned_users(
        message_content, group_id,
        at_segments=at_segments,
        recent_speakers=recent_speakers,
    )
    
    # 3. 加载传记卡
    mentioned_cards = self.biography_manager.get_cards_for_users(list(mentioned.keys()))
    
    # 4. 构建群相关别名速查表（仅当前群有记录的别名）
    all_aliases = self.biography_manager.get_aliases_for_group(group_id)
    
    return speaker_card, mentioned_cards, mentioned, all_aliases
```

### 决策阶段传入 biography 参数

在 [pipeline.py:_decision_and_schedule](file:///d:/Code/sirius_chat/sirius_chat/core/pipeline.py#L350) 的区域，在现有 profiles 收集后新增：

```python
# 现有代码
group_profile = self.semantic_memory.get_group_profile(group_id)
user_profile = self.semantic_memory.get_user_profile(group_id, user_id) if user_id else None

# 新增
speaker_card, mentioned_cards, all_aliases = self._collect_biography_section(
    group_id, user_id, message.content, at_segments=at_segments
)
```

然后在 delayed queue enqueue 和即时回复 prompt 构建时传入这些参数。

---

##  PromptFactory 完整调用链变更

### assemble_chat 签名新增参数

```python
@staticmethod
def assemble_chat(
    *,
    # ... 现有参数 ...
    
    # 新增
    biography_speaker: Any | None = None,              # UserPersonaCard | None
    biography_mentioned: list[Any] | None = None,       # list[UserPersonaCard]
    biography_aliases: dict[str, str] | None = None,    # alias → user_name
    biography_confidence: dict[str, float] | None = None,  # user_id → confidence（供消歧提示用）
) -> PromptBundle:
```

在 sections 构建中插入：

```python
# 在 persona_prompt 之后、identity_verification 之前
bio = PromptFactory.build_biography_section(
    speaker_card=biography_speaker,
    mentioned_cards=biography_mentioned,
    all_aliases=biography_aliases,
)
if bio:
    _add(bio, "identity")
```

---

##  传记重写逻辑

### 触发条件（在 BiographyManager.accumulate 内）

```
pending_observations >= 5
    OR
(距上次重写 >= 24h AND pending_observations > 0)
```

### 重写 prompt

```
你是人物简介撰写助手。请将"旧传记"和"新观察"合并，在预算内重写一份浓缩传记。

旧传记：
{old_short_bio or "（无旧传记）"}

旧锚点：
{old_anchors}

新观察：
{new_observations}

要求：
1. 字数不超过 {token_budget} 字上限
2. 重要事实优先保留，次要信息可自然淘汰
3. 如果新旧信息冲突，以新观察为准（置信度高时）
4. 格式：自然语言段落，不需要结构化

输出格式（JSON）：
{
  "identity_anchors": ["锚定事实1", "锚定事实2", ...],
  "relationships": [{"target": "对方名", "target_id": "对方ID", "fact_hint": "事实描述"}, ...],
  "short_bio": "浓缩传记全文"
}
```

**重写使用 analysis_model（gpt-4o-mini），成本极低。**

---

##  实施顺序

| 阶段 | 文件 | 内容 |
|------|------|------|
| **Phase 1** | `memory/biography/models.py` | 数据结构定义（UserPersonaCard + distilled_points / last_distill_at、RelationshipAnchor、AliasEntry） |
| **Phase 1** | `memory/biography/store.py` | 持久化层（BiographyStore，复用 _atomic_write） |
| **Phase 1** | `memory/biography/__init__.py` | 包入口 |
| **Phase 2** | `memory/biography/manager.py` | 核心管理器（BiographyManager：feed_messages + maybe_distill + maybe_update_biography + 别名消歧） |
| **Phase 3** | `core/engine_core.py` | 构造函数集成（初始化 biography_manager） |
| **Phase 3** | `core/bg_tasks.py` | _bg_diary_promoter 集成（新增 _feed_biography_from_candidates，使用全部 candidates） |
| **Phase 4** | `core/pipeline.py` | 新增 _collect_biography_section，传入决策/执行参数 |
| **Phase 4** | `core/prompt_factory.py` | 新增 build_biography_section + TAG_BIOGRAPHY |
| **Phase 5** | `core/bg_tasks.py` | _build_delayed_prompt 中传入 biography 参数 |
| **Phase 5** | 各 assemble_* 调用点 | 透传 biography 参数 |
| **Phase 6** | 测试 | test_biography.py（models roundtrip、store save/load、manager 两层凝练 + 别名消歧、prompt builder） |
| **Phase 7** | WebUI | API 端点 + 用户传记查看/编辑界面 |

---

##  影响范围

| 模块 | 变更类型 | 说明 |
|------|---------|------|
| `memory/biography/*` | **新增** | 3 个新文件（models, store, manager） |
| `core/engine_core.py` | 修改 | 构造函数 +1 行初始化 |
| `core/bg_tasks.py` | 修改 | _bg_diary_promoter +4 行调用，+1 个新方法 |
| `core/pipeline.py` | 修改 | +1 个新方法，decision 阶段 +3 行 |
| `core/prompt_factory.py` | 修改 | +1 个新静态方法，assemble_chat 签名 +4 个可选参数 |
| 测试 | **新增** | test_biography_manager.py |

**所有现有功能零影响**：新增参数全为可选，默认 None 时全部走现有逻辑。

---

##  关键设计决策记录

1. **传记是全局的（跨群一张卡）** → 群 A 和群 B 的观察收敛到同一个 UserPersonaCard，跨群信息自然综合
2. **传记跟随日记周期** → 不新增触发频率，日记触发时才攒消息 + 条件更新
3. **输入用 candidates 而非日记 output** → candidates 的 user_id↔发言 是系统级精确映射
4. **传记通过 LLM 重写而非追加** → 500 字预算锁死，LLM 语义判断淘汰旧信息
5. **两层凝练而非一步到位** → 原始消息先蒸馏为要点（层1），再积累要点重写传记（层2），信息密度更高
6. **输入使用全部 candidates，不做预过滤** → 与日记输入一致，LLM 自行蒸馏目标用户相关信息（含第三方视角）
7. **别名一对多消歧** → `_alias_index: dict[str, list[AliasEntry]]`，三层消歧（群过滤 → 上下文 → 兜底）
8. **别名权重动态变化** → 经常被用的别名 weight 增长，不用则衰减，支持同名自然分化
9. **别名匹配零 LLM** → 内存 `_alias_index` 精确匹配，消息级 O(1)
10. **与意图分析完全解耦** → 传记更新是后台任务，消息级只做字符串匹配
11. **SemanticMemory 用户画像可大幅简化** → `interest_graph` 不再注入 prompt（由传记替代），`engagement_rate` 仅保留极端档位

---

##  替换分析：传记系统可以取代的现有系统

下表按当前 `assemble_chat()` 的 sections 注入顺序，逐条分析。

### assemble_chat 完整 sections 链

```
当前注入顺序：
  ① persona_prompt           ← AI 人格
  ② identity_verification     ← 身份识别（QQ号 vs 群名片）
  ③ other_ai_instruction      ← 其他 AI peers
  ④ output_spec               ← 输出规范
  ⑤ emotion / scene            ← 情绪 / 场景
  ⑥ first_interaction_hint    ← 首次互动
  ⑦ relationship_contexts     ← 互动指导（engagement_rate）
  ⑧ memory_context            ← 相关记忆（日记 RAG）
  ⑨ group_style               ← 群体风格
  ⑩ cross_group_context       ← 跨群认知
  ⑪ skills                    ← 技能描述
  ⑫ glossary                  ← 名词解释
  ─────────────────────────────────────
  [新增] biography_section     ← 人物速查（插在 ① 和 ② 之间）
```

### 逐条分析

#### ① persona_prompt — 保持

AI 自身人格定义。与传记无关。

#### ② `build_identity_verification()` — 可精简

**当前内容**：
```
【身份识别】
每条消息都标注了发送者的「群名片」和「QQ号」。
注意：群名片可以被用户随意修改，QQ号是固定不变的唯一标识。
如果有人改了群名片冒充别人，请以QQ号为准。
```

**传记覆盖了什么**：传记 section 直接用名字+别名展示当前对话参与者的身份锚点，模型不再需要靠"QQ号匹配"来识别。

**建议**：删除此 section。传记 section 中的别名速查 + 人物锚点已经比"以QQ号为准"更直观。保留它反而会让 prompt 显得割裂——上面刚说了"临雀是群主"，下面又说"请注意QQ号"。

#### ③ `other_ai_instruction` — 保持

告诉模型群内还有其他 AI 角色，避免互相混淆。与传记无关。

#### ④ `output_spec` — 保持

输出格式约束。与传记无关。

#### ⑤ emotion / scene — 保持

情绪上下文。与传记无关。

#### ⑥ `build_first_interaction_hint()` — 可删除

**当前内容**：
```
【首次互动】这是你和 {speaker} 的第一次交流，请保持友好和礼貌。
```

**传记覆盖了什么**：如果传记 section 中某人的卡是空的（无 `short_bio`），模型自然知道此人之前没有认知。如果传记有内容，模型可以看到跨群的累积认知，比单纯的"第一次交流"提示信息量大得多。

**建议**：删除。由传记 section 的 presence/absence 自然指示。
- 调用方（pipeline.py L472）的 `is_first_interaction` 判断 → 删除
- `SemanticMemoryManager.get_user_profile()` 中仅用于这种判断的查询 → 可移除
- `assemble_chat` 的 `is_first_interaction` 参数 → 删除
- `build_first_interaction_hint()` 整个方法 → 删除

#### ⑦ `build_relationship_contexts()` — 可精简但不删除

**当前内容**（基于 engagement_rate）：
```
【互动指导】Alice经常回应你的消息，你们互动很好，可以自然放松。
【互动指导】Bob很少回应你的消息，尽量简洁，不要强行搭话。
【互动指导】你和Charlie是第一次交流，请保持友好和礼貌。
```

**传记覆盖了什么**：传记中的 `short_bio` 已经通过 LLM 提炼了互动层面（如"临雀经常在群里聊天"）。但 `engagement_rate` 是一个**量化信号**（0~1 的精确数字），LLM 的自由文本无法精确表达。

**建议**：保留但精简。
- "首次互动"逻辑移除（已在⑥中删除）
- 开发者的特殊关系保留（"is_developer → 畅所欲言"）
- 极端情况保留（rate < 0.15 或 rate > 0.6），中间档位（0.3~0.6）去掉

精简后的方法：
```
if is_developer:   →  "畅所欲言"
if rate < 0.15:    →  "很少回应，尽量简洁"
if rate > 0.6:     →  "互动很好，自然放松"
else:              →  不注入（传记的 short_bio 已覆盖常规认知）
```

#### ⑧ `build_memory_context()` — 保持

日记 RAG 检索的历史话题记忆。不是人物信息。

#### ⑨ `build_group_style()` — 保持

群消息统计 + 长度/温度限制。不是人物信息。

#### ⑩ `cross_group_context`（pipeline L382-403） — 完全删除

**当前构建逻辑**（在 [pipeline.py](file:///d:/Code/sirius_chat/sirius_chat/core/pipeline.py#L382-L403)）：
```python
cross_group_context = ""   # 约 25 行代码
if user_id:
    global_user = self.user_manager.get_global_user(user_id)
    global_semantic = self.semantic_memory.get_global_user_profile(user_id)
    group_count = sum(...)
    if group_count > 0 or ...:
        parts.append(f"你在 {group_count} 个其他群中也认识...")
        parts.append(f"TA 的别名/昵称有：{aliases}")
        parts.append(f"兴趣话题：{topics}")
```

**传记覆盖了什么**：全部。
- "跨群认识" → 传记是全局的，同一张卡
- "别名/昵称" → 传记 section 自带别名速查
- "兴趣话题" → 传记 `short_bio` 中 LLM 已综合

**建议**：完全删除此代码块及以下相关代码：
- `pipeline.py` 中 `cross_group_context` 构建的 25 行 → 删除
- `assemble_chat` 的 `cross_group_context` 参数 → 删除
- `PromptFactory.build_cross_group_section()` → 删除
- `TAG_CROSS_GROUP` 常量 → 可保留（无副作用）

相应的 `user_manager.get_global_user()` 和 `semantic_memory.get_global_user_profile()` 调用 — 若仅服务于 cross_group_context，则一并删除。

#### ⑪ skills — 保持

技能描述。与传记无关。

#### ⑫ glossary — 保持

名词解释。与传记无关。

---

### 汇总：可删除 / 可精简清单

| 项目 | 操作 | 涉及文件 | 删除行约 |
|------|------|---------|---------|
| `build_identity_verification()` | **删除** | `prompt_factory.py` | ~7 |
| `build_first_interaction_hint()` | **删除** | `prompt_factory.py` | ~8 |
| `is_first_interaction` 判断逻辑 | **删除** | `pipeline.py` L465-470 | ~8 |
| `cross_group_context` 构建 | **删除** | `pipeline.py` L382-403 | ~25 |
| `cross_group_context` 参数 | **删除** | `assemble_chat` 签名 | 1 参数 |
| `TAG_CROSS_GROUP` section 注入 | **删除** | `assemble_chat` sections 构建 | ~3 |
| `build_cross_group_section()` | **删除** | `prompt_factory.py` | ~5 |
| `build_relationship_context()` 中间档位 | **精简** | `prompt_factory.py` L397-425 | ~15 |
| `assemble_chat` `is_first_interaction` 参数 | **删除** | 签名 + 调用点 | 若干 |

**总计可减少 ~70 行代码 + ~50 tokens/prompt**。

### SemanticMemory 的受影响部分

| 当前功能 | 影响 | 说明 |
|---------|------|------|
| `get_user_profile()` → `first_interaction_at` 判断 | 删除 | ⑥已删除首次互动判断 |
| `get_user_profile()` → `engagement_rate` | 保留 | ⑦精简后仍用于极端互动指导 |
| `get_user_profile()` → `interest_graph` | 删除LLM端 | 不再通过 cross_group 注入，但日记 promotion 时仍会更新 topics |
| `get_global_user_profile()` | 删除 | 仅 cross_group 使用，传记已替代 |
| `user_manager.get_global_user()` | 删除 | 同上 |
| `record_user_interaction()` | 保留 | engagement_rate 计算需要 |
| `resolve_pending_feedback()` | 保留 | engagement_rate 计算需要 |

### 不删除的内容及原因

| 保留项 | 原因 |
|--------|------|
| `persona_prompt` | AI 自身人格，完全不同的概念 |
| `other_ai_instruction` | 其他 AI 角色标记，传记不覆盖 |
| `emotion_context` | 发言者当下情绪，传记是长期认知 |
| `memory_context`（日记 RAG） | "发生过什么"，与传记"是什么样的人"互补 |
| `group_style` | 群级别统计，非人物级别 |
| `SemanticMemory.engagement_rate` 计算 | ⑦保留极端档位的行为信号 |
| `SemanticMemory.interest_topics` 生成 | 日记 promoter 仍需要提取群话题 |

---

### 架构变更图

```
变更前：
  Message → perception → cognition → decision → execution
              │              │
              ├── cross_group_context (UserManager + SemanticMemory)
              │       └── 别名、跨群认知、兴趣话题
              │
              └── prompt injection
                      ├── identity_verification (QQ号 vs 群名片)
                      ├── first_interaction_hint
                      ├── relationship_contexts (engagement_rate 全档位)
                      └── cross_group section
                          
变更后：
  Message → perception → decision → execution
              │
              ├── biography._collect_mentioned_users (纯别名匹配)
              │
              └── prompt injection
                      ├── biography_section [新增] ← 替代全部以上四项
                      ├── relationship_contexts [精简]（仅极端档位）
                      │
（以下全部删除）
  ✗ identity_verification
  ✗ first_interaction_hint
  ✗ cross_group_context 构建
  ✗ cross_group section 注入
