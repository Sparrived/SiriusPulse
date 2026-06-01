---
name: framework-quickstart
description: "在不通读全部代码的情况下快速理解 Sirius Pulse 架构时使用，包括模块边界、执行流与扩展点。关键词：架构总览、框架地图、修改位置、provider 集成、多人格。"
---

# 框架快速上手

## 目标

在开始修改前，快速建立对 Sirius Pulse 当前代码结构的准确认知，优先搞清楚：

- 推荐入口是什么
- 真正的 engine 实现位于哪里
- 多人格进程模型如何工作
- 四层记忆架构如何运作
- 各模块的边界如何划分
- 哪些文件是当前架构的事实来源

## 语言规范

- 本仓库所有 SKILL 文件必须使用中文编写。
- 后续新增或修改任意 SKILL 时，frontmatter 的 `description` 与正文均需使用中文。
- 若发现历史 SKILL 出现英文内容，需在当前任务中一并改为中文。

## 阅读顺序（先做这个）

### 文档层

1. `docs/guide/architecture-overview.md`（v1.2 多人格架构全景，10 章详细文档）
2. `docs/guide/engine-architecture.md`（引擎架构详解）
3. `docs/guide/memory-system.md`（记忆系统总览）
4. `docs/guide/memory-diary.md`（日记记忆子系统）
5. `docs/guide/memory-evolution.md`（演化链记忆子系统）
6. `docs/guide/persona-system.md`（人格系统）
7. `docs/guide/skill-system.md`（技能系统）
8. `docs/guide/configuration.md`（配置系统）
9. `docs/guide/platform-napcat.md`（NapCat 平台适配）
10. `README.md`

### 入口与人格生命周期

11. `main.py`（统一 CLI 入口）
12. `sirius_pulse/__init__.py`（顶层公开 API 清单，130+ 符号）
13. `sirius_pulse/persona_manager.py`（多人格生命周期管理）
14. `sirius_pulse/persona_worker.py`（子进程入口）
15. `sirius_pulse/persona_config.py`（人格级配置模型）

### 引擎核心（组合模式拆分）

16. `sirius_pulse/core/emotional_engine.py`（组合模式最终类，薄 shim）
17. `sirius_pulse/core/engine_core.py`（引擎基类：__init__、公开 API、委托方法）
18. `sirius_pulse/core/pipeline.py`（Pipeline 组件：4 阶段管线）
19. `sirius_pulse/core/brain.py`（Brain：LLM 调用中枢，chat() 串行 + raw_call() 并行）
20. `sirius_pulse/core/prompt_factory.py`（PromptFactory：无状态 prompt 构建工具类）
21. `sirius_pulse/core/bg_tasks.py`（BackgroundTasks 组件：后台任务管理）
22. `sirius_pulse/core/bg_tasks_delayed.py`（DelayedQueueTasks 组件：延迟队列任务）
23. `sirius_pulse/core/bg_tasks_proactive.py`（ProactiveTasks 组件：主动消息任务）
24. `sirius_pulse/core/helpers.py`（Helpers 组件：技能集成、被动 SKILL 注册与触发分发）
25. `sirius_pulse/core/engine_persistence.py`（EnginePersistence 组件 + EngineStateStore）
26. `sirius_pulse/core/engine_sticker.py`（EngineSticker 组件：表情包系统）

### 引擎子系统

27. `sirius_pulse/core/cognition.py`（认知分析）
28. `sirius_pulse/core/response_strategy.py`（响应策略引擎）
29. `sirius_pulse/core/model_router.py`（任务感知模型选择）
30. `sirius_pulse/core/threshold_engine.py`（阈值引擎）
31. `sirius_pulse/core/rhythm.py`（节奏分析）
32. `sirius_pulse/core/events.py`（事件总线）
33. `sirius_pulse/core/identity_resolver.py`（四层身份解析链）
34. `sirius_pulse/core/proactive_trigger.py`（主动触发器）
35. `sirius_pulse/core/delayed_response_queue.py`（延迟响应防抖队列）
36. `sirius_pulse/core/pinned_message.py`（消息钉住管理）
37. `sirius_pulse/core/orchestration_store.py`（编排配置持久化）
38. `sirius_pulse/core/persona_db.py`（统一 SQLite 连接管理）
39. `sirius_pulse/core/persona_store.py`（PersonaProfile 持久化）
40. `sirius_pulse/core/persona_generator.py`（人格创建器：模板 + 访谈）
41. `sirius_pulse/core/plugin_intent_matcher.py`（插件意图嵌入匹配）
42. `sirius_pulse/core/plugin_intent_verifier.py`（插件意图 LLM 验证）
43. `sirius_pulse/core/user_lookup.py`（用户查找服务）
44. `sirius_pulse/core/skill_engine_context.py`（SkillEngineContextImpl）
45. `sirius_pulse/core/constants.py`（核心引擎常量定义）
46. `sirius_pulse/core/utils.py`（核心引擎工具函数）

### 四层记忆架构

47. `sirius_pulse/memory/basic/manager.py`（Layer 1：基础消息记忆）
48. `sirius_pulse/memory/situation/extractor.py`（Layer 2：情景压缩提取器）
49. `sirius_pulse/memory/situation/store.py`（Layer 2：情景 SQLite 存储）
50. `sirius_pulse/memory/evolution/chain.py`（Layer 3：演化链验证中枢）
51. `sirius_pulse/memory/evolution/store.py`（Layer 3：演化链 SQLite 存储）
52. `sirius_pulse/memory/biography/view.py`（Layer 4：传记视图引擎）
53. `sirius_pulse/memory/schema.py`（Layer 4：行为模式归纳）
54. `sirius_pulse/memory/gap_detector.py`（Layer 4：知识缺口检测）
55. `sirius_pulse/memory/cold_detector.py`（冷检测器：驱动 Layer 2/3 触发）
56. `sirius_pulse/memory/diary/manager.py`（日记记忆管理器）
57. `sirius_pulse/memory/semantic/manager.py`（语义记忆管理器）
58. `sirius_pulse/memory/user/unified_manager.py`（统一用户管理器）
59. `sirius_pulse/memory/glossary/manager.py`（名词解释管理器）
60. `sirius_pulse/memory/context_assembler.py`（上下文组装器）
61. `sirius_pulse/memory/cognition_store.py`（认知事件存储）
62. `sirius_pulse/memory/storage.py`（统一 SQLite 用户存储层）

### 平台适配

63. `sirius_pulse/adapters/base.py`（BaseAdapter 抽象基类）
64. `sirius_pulse/adapters/models.py`（跨平台消息片段模型）
65. `sirius_pulse/platforms/runtime.py`（EngineRuntime 封装）
66. `sirius_pulse/platforms/onebot_v11/napcat/manager.py`（NapCat 多实例管理）
67. `sirius_pulse/platforms/onebot_v11/napcat/adapter.py`（NapCat 适配器）
68. `sirius_pulse/platforms/onebot_v11/protocol.py`（OneBot v11 协议解析）

### 插件系统

69. `sirius_pulse/plugins/api.py`（插件开发统一 API 入口）
70. `sirius_pulse/plugins/base.py`（PluginBase 基类）
71. `sirius_pulse/plugins/loader.py`（插件加载器）
72. `sirius_pulse/plugins/registry.py`（插件注册表）
73. `sirius_pulse/plugins/executor.py`（插件执行器）
74. `sirius_pulse/plugins/lexer.py`（词法分析器 + AST 解析）
75. `sirius_pulse/plugins/dispatcher.py`（输出调度器）
76. `sirius_pulse/plugins/scheduler.py`（定时调度器）
77. `sirius_pulse/plugins/context.py`（EngineProxy + PluginContext）
78. `sirius_pulse/plugins/models.py`（插件数据模型）
79. `sirius_pulse/plugins/decorators.py`（@command 装饰器）
80. `sirius_pulse/plugins/config.py`（插件配置管理）
81. `sirius_pulse/plugins/events.py`（插件事件定义）

### SKILL 系统

82. `sirius_pulse/skills/api.py`（SKILL 开发统一 API 入口）
83. `sirius_pulse/skills/registry.py`（SKILL 注册表）
84. `sirius_pulse/skills/executor.py`（SKILL 执行器）
85. `sirius_pulse/skills/security.py`（安全校验）
86. `sirius_pulse/skills/models.py`（SKILL 数据模型）
87. `sirius_pulse/skills/data_store.py`（SKILL 数据持久化 KV）
88. `sirius_pulse/skills/dependency_resolver.py`（依赖自动安装）
89. `sirius_pulse/skills/telemetry.py`（执行遥测）

### Provider 体系

90. `sirius_pulse/providers/base.py`（Provider 抽象基类 + 数据模型）
91. `sirius_pulse/providers/routing.py`（注册表 + 路由逻辑）
92. `sirius_pulse/providers/openai_compatible.py`（OpenAI 兼容基类）
93. `sirius_pulse/providers/models_dev.py`（models.dev 自动填充）

### 配置与模型

94. `sirius_pulse/config/manager.py`（ConfigManager）
95. `sirius_pulse/config/models.py`（配置数据模型）
96. `sirius_pulse/config/config_builder.py`（声明式配置构建器）
97. `sirius_pulse/config/config_helpers.py`（类型强制转换 + 环境变量替换）
98. `sirius_pulse/config/file_io.py`（原子 JSON 保存）
99. `sirius_pulse/config/jsonc.py`（JSONC 注释解析）
100. `sirius_pulse/models/models.py`（Message、Transcript 等核心数据契约）
101. `sirius_pulse/models/persona.py`（PersonaProfile 等人格模型）
102. `sirius_pulse/models/emotion.py`（EmotionState 情感模型）
103. `sirius_pulse/models/intent_v3.py`（IntentAnalysisV3 意图分析）
104. `sirius_pulse/models/response_strategy.py`（响应策略模型）

### 存储与工具

105. `sirius_pulse/session/store.py`（SessionStore）
106. `sirius_pulse/token/token_store.py`（Token 使用 SQLite 存储）
107. `sirius_pulse/token/analytics.py`（多维 Token 分析）
108. `sirius_pulse/token/usage.py`（Token 使用聚合模型）
109. `sirius_pulse/utils/sqlite_base.py`（BaseSqliteStore 基类）
110. `sirius_pulse/utils/query_builder.py`（动态 SQL 构建器）
111. `sirius_pulse/utils/layout.py`（WorkspaceLayout 路径布局）
112. `sirius_pulse/utils/json_io.py`（公共 JSON I/O 工具）
113. `sirius_pulse/utils/retry.py`（通用异步重试工具）

### WebUI

114. `sirius_pulse/webui/server.py`（WebUI 统一再导出 shim）
115. `sirius_pulse/webui/server_core.py`（WebUIServer 类定义、路由注册）
116. `sirius_pulse/webui/persona_api.py`（人格管理 API）
117. `sirius_pulse/webui/biography_api.py`（传记系统 API）
118. `sirius_pulse/webui/evolution_api.py`（统一记忆系统 API）
119. `sirius_pulse/webui/memory_api.py`（Token/认知/日记 API）
120. `sirius_pulse/webui/server_plugin_api.py`（全局 Plugin 管理 API）
121. `sirius_pulse/webui/server_skill_api.py`（每人格 Skill 配置 API）
122. `sirius_pulse/webui/auth.py`（JWT 认证管理器）
123. `sirius_pulse/webui/middleware.py`（认证中间件）
124. `sirius_pulse/webui/monitoring_api.py`（监控 API）
125. `sirius_pulse/webui/ws_server.py`（WebSocket 事件推送服务）

### 外部集成与基础设施

126. `sirius_pulse/github/client.py`（GitHub REST API 异步客户端）
127. `sirius_pulse/github/event_bridge.py`（GitHub 事件桥接）
128. `sirius_pulse/github/webhook.py`（Webhook 签名验证 + HTTP 服务器）
129. `sirius_pulse/embedding/server.py`（Embedding 微服务端）
130. `sirius_pulse/embedding/client.py`（Embedding 同步客户端）
131. `sirius_pulse/exceptions.py`（结构化异常体系）
132. `sirius_pulse/logging_config.py`（结构化日志配置）
133. `sirius_pulse/mixins.py`（JsonSerializable mixin）

## 心智模型

### 多人格进程架构

```
主进程（python main.py run）
    │
    ├── PersonaManager          # 扫描人格目录、端口分配、启停调度
    ├── WebUIServer             # aiohttp REST API + WebSocket + 认证中间件
    └── NapCatManager           # NapCat 全局安装/多实例管理
            │
            ▼
    子进程（独立控制台窗口）
    ├── PersonaWorker ── EngineRuntime ── EmotionalGroupChatEngine（组合模式）
    │       │
    │       ├── Brain（LLM 调用中枢：chat 串行 + raw_call 并行）
    │       ├── NapCatAdapter ── NapCat OneBot v11 WS
    │       ├── 四层记忆架构：
    │       │   ├── L1 BasicMemory（基础消息）
    │       │   ├── L2 SituationExtractor（情景压缩，暂冷 5 分钟触发）
    │       │   ├── L3 EvolutionChain（演化链验证中枢，矛盾检测+置信度+溯源）
    │       │   └── L4 BiographyView + SchemaInductor + GapDetector（传记/模式/缺口）
    │       ├── ColdDetector（两阶段冷检测：WARM→COLD）
    │       ├── IdentityResolver（四层身份解析链）
    │       ├── ModelRouter（任务感知模型选择）
    │       ├── PluginExecutor + PluginIntentMatcher + PluginIntentVerifier
    │       ├── SkillRegistry + SkillExecutor
    │       ├── ProactiveTrigger（无刺激主动触发）
    │       ├── DelayedResponseQueue（延迟防抖合并）
    │       └── PinnedMessageManager（消息钉住）
    ├── EmbeddingClient ── Embedding 微服务（共享，主进程启动）
    └── ...（多个人格并行）
```

### 关键事实

- **推荐生产入口** 是 `PersonaManager`（多人格生命周期管理）；单个人格可直接创建 `EngineRuntime` 或 `EmotionalGroupChatEngine`。
- `EmotionalGroupChatEngine` 是唯一引擎，采用**组合模式**：`emotional_engine.py`（薄 shim）继承 `_EmotionalGroupChatEngineBase`（基类在 `engine_core.py`），通过以下组件实现功能：
  - `engine._pipeline: Pipeline` — 4 阶段管线（感知→认知→决策→执行）
  - `engine._brain: Brain` — LLM 调用中枢（`chat()` 串行通道 + `raw_call()` 并行通道，支持 pre/post hook）
  - `engine._bg_tasks_mgr: BackgroundTasks` — 后台任务管理（委托给 `ProactiveTasks` 和 `DelayedQueueTasks`）
  - `engine._helpers: Helpers` — 技能集成、被动 SKILL 注册与触发分发
  - `engine._persistence: EnginePersistence` — 状态持久化（`EngineStateStore` 负责序列化）
  - `engine._sticker: EngineSticker` — 表情包系统
  - `engine._prompt_factory: PromptFactory` — 无状态 prompt 构建
  - `engine._pinned_messages: PinnedMessageManager` — 消息钉住管理
  - `engine._delayed_queue: DelayedResponseQueue` — 延迟响应防抖
  - `engine._identity_resolver: IdentityResolver` — 四层身份解析链（L1 平台 ID→L1.5 Bot 检测→L2 别名→L3 模糊→L4 上下文推断）
  - `engine._proactive_trigger: ProactiveTrigger` — 主动触发（时间/记忆/情绪三种触发类型）
- `engine_core.py` 的 `__init__` 通过一系列 `_init_*` 方法初始化所有子系统，保持向后兼容 API。
- **四层记忆架构**：
  - Layer 1（基础消息）→ Layer 2（情景压缩，暂冷 5 分钟触发）→ Layer 3（演化链验证，矛盾检测+置信度评估）→ Layer 4（传记视图+行为模式+知识缺口）
  - `ColdDetector` 驱动 Layer 2/3 的触发时机
  - `EvolutionChain` 是记忆系统的"真理之源"，所有信息必须经过验证
- **人格系统**：`PersonaDatabase` 提供统一 SQLite 连接管理（`persona.db`），`PersonaStore` 负责 JSON 持久化，`PersonaGenerator` 支持模板和访谈两种创建路径。
- **插件意图快速识别**：两阶段管线 — `PluginIntentMatcher`（嵌入向量余弦相似度，阈值 0.65）→ `PluginIntentVerifier`（轻量 LLM 确认+参数提取）。
- **平台适配层**：`adapters/` 包提供跨平台抽象（`BaseAdapter`、`MessageSegment` 联合类型、`ParsedEvent`），各平台（NapCat/Discord 等）继承实现。
- 一个 `SessionConfig` 只对应一个主 AI，主 AI 由 `preset=AgentPreset(...)` 描述。
- `User` 是 `Participant` 的公开别名，不存在第二套独立的人类参与者模型。
- 配置资产与运行态数据支持双根分离：config root 负责配置与角色资产，data root 负责 session、memory、token 与 skill_data。
- `sirius_pulse/__init__.py` 是顶层公开 API 统一重导出（严格 `__all__`，130+ 符号），所有对外接口从这里导入。
- WebUI 支持 JWT 认证（admin/viewer 角色）、WebSocket 事件推送、监控 API、传记管理、演化链仪表盘。

### 模块职责

| 模块 | 主要职责 | 不应承担的职责 |
|------|---------|-------------|
| `sirius_pulse/__init__.py` | 顶层公开 API 统一重导出（130+ 符号） | 不直接实现底层编排或路径布局 |
| `sirius_pulse/persona_manager.py` | **推荐生产入口**：多人格生命周期管理 | 不实现底层对话生成 |
| `sirius_pulse/persona_worker.py` | 单个人格子进程入口 | 不管理其他人格 |
| `sirius_pulse/persona_config.py` | 人格级配置模型 | 不处理全局配置 |
| `sirius_pulse/adapters/` | 跨平台适配器抽象层：`BaseAdapter` 基类、`MessageSegment` 消息片段模型、`ParsedEvent` 统一事件结构 | 不介入引擎编排 |
| `sirius_pulse/platforms/` | 平台具体实现：`platforms/onebot_v11/napcat/`（NapCat 适配器、管理器、协议解析）、`runtime.py`（EngineRuntime 封装） | 不介入高层人格调度 |
| `sirius_pulse/webui/` | WebUI REST API + 静态页面 + JWT 认证 + WebSocket 事件推送 + 监控 API + 传记管理 + 演化链仪表盘 | 不直接操作 NapCat 进程 |
| `sirius_pulse/github/` | GitHub API 集成：REST 客户端、事件桥接、Webhook 服务器 | 不介入核心对话逻辑 |
| `sirius_pulse/plugins/` | 插件系统：PluginBase、词法分析/AST、输出调度、定时调度、EngineProxy、@command 装饰器、PluginContext | 不负责 SKILL 执行 |
| `sirius_pulse/core/` | 编排核心：EmotionalGroupChatEngine（组合模式）、Brain（LLM 中枢）、Pipeline、认知/决策/响应策略、四层身份解析、主动触发、延迟防抖、消息钉住、编排持久化、人格数据库、事件总线、节奏/阈值分析 | 不负责 SKILL 注册表 |
| `sirius_pulse/memory/` | 四层记忆架构（基础消息→情景压缩→演化链验证→传记/模式/缺口）、日记、语义、统一用户管理、名词解释、上下文组装、冷检测 | 不直接决定 provider 路由 |
| `sirius_pulse/providers/` | provider 协议、9 个具体上游实现（OpenAI 兼容/智谱/DeepSeek/硅基流动/火山引擎/阿里云百炼/MIMO/YTea/Mock）、注册表、自动路由、models.dev 自动填充 | 不介入高层人格生命周期 |
| `sirius_pulse/skills/` | SKILL 注册、依赖解析、执行、安全校验、遥测、数据存储；被动 SKILL 支持；13 个内置技能（reminder/github_monitor/bing_search/url_content_reader/file_*/send_*/desktop_screenshot/learn_term/system_info） | 不负责 provider 注册表 |
| `sirius_pulse/config/` | SessionConfig、WorkspaceConfig、ConfigManager、ConfigBuilder 声明式构建、JSONC 解析、原子文件 I/O、类型强制转换 | 不改变核心对话契约 |
| `sirius_pulse/models/` | 数据契约：Message、Transcript、EmotionState、IntentAnalysisV3、PersonaProfile、ResponseStrategy 等 | 不处理持久化 |
| `sirius_pulse/session/` | SessionStore（Json/Sqlite）、持久化后端 | 不介入对话逻辑 |
| `sirius_pulse/token/` | Token 记录（SQLite 持久化+批量缓冲）、多维分析（按 session/actor/task/model/时间切片聚合）、tiktoken 估算 | 不介入对话逻辑 |
| `sirius_pulse/embedding/` | Embedding 微服务端（aiohttp + asyncio.Queue 批量合并推理）+ 同步客户端 | 不介入对话逻辑 |
| `sirius_pulse/utils/` | 工具函数、WorkspaceLayout 路径布局、JSON I/O、异步重试、BaseSqliteStore 基类、QueryBuilder 动态 SQL | 不改变核心对话契约 |
| `sirius_pulse/exceptions.py` | 结构化异常体系（SiriusException→ProviderError/TokenError/ParseError 等 16 个异常） | 不改变核心对话契约 |
| `sirius_pulse/logging_config.py` | 结构化日志配置（console/json 双格式、文件循环、异步处理） | 不改变核心对话契约 |
| `sirius_pulse/mixins.py` | JsonSerializable dataclass 序列化 mixin | 不改变核心对话契约 |

## 常用文件路径

| 路径 | 说明 |
| ---- | ---- |
| `main.py` | 统一 CLI 入口（默认启动 WebUI；`run` 启动全部人格；`persona` 子命令管理人格） |
| `sirius_pulse/__init__.py` | 顶层公开 API 导出清单（严格 `__all__`，130+ 符号） |
| `sirius_pulse/persona_manager.py` | 多人格生命周期管理 |
| `sirius_pulse/persona_worker.py` | 子进程入口 |
| `sirius_pulse/persona_config.py` | 人格级配置模型 |
| `sirius_pulse/core/emotional_engine.py` | 核心情感群聊引擎（薄 shim，组合模式最终类） |
| `sirius_pulse/core/engine_core.py` | 引擎基类（__init__、公开 API、委托方法） |
| `sirius_pulse/core/pipeline.py` | Pipeline 组件（4 阶段管线：感知→认知→决策→执行） |
| `sirius_pulse/core/brain.py` | Brain：LLM 调用中枢（chat 串行 + raw_call 并行，支持 pre/post hook） |
| `sirius_pulse/core/prompt_factory.py` | PromptFactory：无状态 prompt 构建工具类 |
| `sirius_pulse/core/bg_tasks.py` | BackgroundTasks 组件（后台任务管理） |
| `sirius_pulse/core/bg_tasks_delayed.py` | DelayedQueueTasks 组件（延迟队列任务） |
| `sirius_pulse/core/bg_tasks_proactive.py` | ProactiveTasks 组件（主动消息任务） |
| `sirius_pulse/core/helpers.py` | Helpers 组件（技能集成、被动 SKILL 注册与触发分发） |
| `sirius_pulse/core/engine_persistence.py` | EnginePersistence 组件 + EngineStateStore（状态持久化） |
| `sirius_pulse/core/engine_sticker.py` | EngineSticker 组件（表情包系统） |
| `sirius_pulse/core/identity_resolver.py` | 四层身份解析链（L1 平台 ID→L2 别名→L3 模糊→L4 上下文推断） |
| `sirius_pulse/core/proactive_trigger.py` | 主动触发器（时间/记忆/情绪三种触发类型） |
| `sirius_pulse/core/delayed_response_queue.py` | 延迟响应防抖队列（IMMEDIATE 5 秒防抖，按热度动态调整） |
| `sirius_pulse/core/pinned_message.py` | 消息钉住管理器（多条钉住+carry count 自动取消） |
| `sirius_pulse/core/orchestration_store.py` | 编排配置持久化（orchestration.json） |
| `sirius_pulse/core/persona_db.py` | 统一 SQLite 连接管理（persona.db，WAL 模式） |
| `sirius_pulse/core/persona_store.py` | PersonaProfile JSON 持久化 |
| `sirius_pulse/core/persona_generator.py` | 人格创建器（模板 + 访谈双路径） |
| `sirius_pulse/core/plugin_intent_matcher.py` | 插件意图嵌入匹配（余弦相似度，阈值 0.65） |
| `sirius_pulse/core/plugin_intent_verifier.py` | 插件意图 LLM 验证（轻量确认+参数提取） |
| `sirius_pulse/core/cognition.py` | 认知分析 |
| `sirius_pulse/core/response_strategy.py` | 响应策略引擎 |
| `sirius_pulse/core/model_router.py` | 任务感知模型选择 |
| `sirius_pulse/core/threshold_engine.py` | 阈值引擎 |
| `sirius_pulse/core/rhythm.py` | 节奏分析 |
| `sirius_pulse/core/events.py` | 事件总线（8 种事件类型，有损广播） |
| `sirius_pulse/core/skill_engine_context.py` | SkillEngineContextImpl（被动 SKILL 与引擎交互适配器） |
| `sirius_pulse/core/constants.py` | 核心引擎常量定义（时间、Token、记忆等魔法数字） |
| `sirius_pulse/core/utils.py` | 核心引擎工具函数（时间戳、XML 清理、表情包标签解析） |
| `sirius_pulse/memory/cold_detector.py` | 两阶段冷检测器（HOT→WARM 5 分钟→COLD 30 分钟） |
| `sirius_pulse/memory/evolution/chain.py` | 演化链验证中枢（矛盾检测+置信度+级联纠正） |
| `sirius_pulse/memory/evolution/store.py` | 演化链 SQLite 存储层 |
| `sirius_pulse/memory/situation/extractor.py` | 情景压缩提取器（LLM 提取三元组） |
| `sirius_pulse/memory/situation/store.py` | 情景 SQLite 存储层 |
| `sirius_pulse/memory/biography/view.py` | 传记视图引擎（从演化链 active 三元组实时计算） |
| `sirius_pulse/memory/schema.py` | 行为模式归纳（LLM 从三元组归纳抽象模式） |
| `sirius_pulse/memory/gap_detector.py` | 知识缺口检测（纯规则，不使用 LLM） |
| `sirius_pulse/memory/storage.py` | 统一 SQLite 用户存储层（users + user_identities 表） |
| `sirius_pulse/memory/diary/manager.py` | 日记记忆管理器 |
| `sirius_pulse/memory/diary/slicer.py` | 日记切片器 |
| `sirius_pulse/memory/diary/slice_store.py` | 日记切片 SQLite 存储 |
| `sirius_pulse/memory/diary/slice_vector_store.py` | 日记切片向量存储 |
| `sirius_pulse/memory/semantic/manager.py` | 语义记忆管理器 |
| `sirius_pulse/memory/user/unified_manager.py` | 统一用户管理器（注册/解析/群隔离/别名管理） |
| `sirius_pulse/memory/glossary/manager.py` | 名词解释管理器 |
| `sirius_pulse/memory/context_assembler.py` | 上下文组装器 |
| `sirius_pulse/memory/cognition_store.py` | 认知事件存储 |
| `sirius_pulse/adapters/base.py` | BaseAdapter 抽象基类（跨平台适配器接口） |
| `sirius_pulse/adapters/models.py` | 跨平台消息片段模型（MessageSegment/MessageGroup/ParsedEvent） |
| `sirius_pulse/platforms/runtime.py` | EngineRuntime 封装 |
| `sirius_pulse/platforms/onebot_v11/napcat/manager.py` | NapCat 多实例管理 |
| `sirius_pulse/plugins/api.py` | 插件开发统一 API 入口 |
| `sirius_pulse/plugins/base.py` | PluginBase 基类 |
| `sirius_pulse/plugins/lexer.py` | 词法分析器 + AST 解析（Unix 风格指令） |
| `sirius_pulse/plugins/dispatcher.py` | 输出调度器（direct/llm/silent 三模式） |
| `sirius_pulse/plugins/scheduler.py` | 定时调度器（cron + interval） |
| `sirius_pulse/skills/api.py` | SKILL 开发统一 API 入口 |
| `sirius_pulse/skills/data_store.py` | SKILL 数据持久化 KV（JSON 文件，线程安全） |
| `sirius_pulse/skills/dependency_resolver.py` | 依赖自动安装（uv pip install） |
| `sirius_pulse/skills/telemetry.py` | 执行遥测（JSONL 追加式存储） |
| `sirius_pulse/providers/base.py` | Provider 抽象基类 + 数据模型 |
| `sirius_pulse/providers/routing.py` | Provider 注册表 + 路由逻辑 |
| `sirius_pulse/providers/openai_compatible.py` | OpenAI 兼容基类（所有 Provider 的基础） |
| `sirius_pulse/providers/models_dev.py` | models.dev 社区数据库自动填充 |
| `sirius_pulse/config/manager.py` | ConfigManager |
| `sirius_pulse/config/config_builder.py` | 声明式配置构建器（链式分组 API） |
| `sirius_pulse/config/jsonc.py` | JSONC 注释解析 |
| `sirius_pulse/config/file_io.py` | 原子 JSON 保存 |
| `sirius_pulse/github/client.py` | GitHub REST API 异步客户端 |
| `sirius_pulse/github/event_bridge.py` | GitHub 事件桥接（Plugin/SKILL 消费） |
| `sirius_pulse/github/webhook.py` | Webhook 签名验证 + HTTP 服务器 |
| `sirius_pulse/embedding/server.py` | Embedding 微服务端（aiohttp + asyncio.Queue 批量合并推理） |
| `sirius_pulse/embedding/client.py` | Embedding 同步客户端（urllib） |
| `sirius_pulse/token/token_store.py` | Token 使用 SQLite 存储（批量缓冲写入） |
| `sirius_pulse/token/analytics.py` | 多维 Token 分析（跨 session 聚合） |
| `sirius_pulse/utils/sqlite_base.py` | BaseSqliteStore 基类（统一连接管理+WAL） |
| `sirius_pulse/utils/query_builder.py` | 动态 SQL 查询构建器 |
| `sirius_pulse/utils/json_io.py` | 公共 JSON I/O 工具（原子写入 + 安全读取） |
| `sirius_pulse/utils/retry.py` | 通用异步重试工具 |
| `sirius_pulse/utils/layout.py` | WorkspaceLayout 路径布局 |
| `sirius_pulse/exceptions.py` | 结构化异常体系（16 个异常类型） |
| `sirius_pulse/logging_config.py` | 结构化日志配置（console/json 双格式） |
| `sirius_pulse/mixins.py` | JsonSerializable dataclass 序列化 mixin |
| `sirius_pulse/webui/server.py` | WebUI 统一再导出 shim |
| `sirius_pulse/webui/server_core.py` | WebUIServer 类定义、路由注册 |
| `sirius_pulse/webui/persona_api.py` | 人格管理 API |
| `sirius_pulse/webui/biography_api.py` | 传记系统 API |
| `sirius_pulse/webui/evolution_api.py` | 统一记忆系统 API（演化链/情景/切片/传记/缺口） |
| `sirius_pulse/webui/memory_api.py` | Token/认知/日记/向量存储 API |
| `sirius_pulse/webui/auth.py` | JWT 认证管理器（纯标准库实现，HMAC-SHA256） |
| `sirius_pulse/webui/middleware.py` | 认证中间件（白名单放行 + RBAC 权限控制） |
| `sirius_pulse/webui/monitoring_api.py` | 监控 API（全局概览、单人格指标、健康检查） |
| `sirius_pulse/webui/ws_server.py` | WebSocket 事件推送服务（按人格/全局订阅） |
| `tests/conftest.py` | 测试最小 fixture |
| `scripts/ci_check.py` | 统一 CI 检查脚本 |
| `scripts/migrate_to_unified_db.py` | 数据迁移：独立数据库→统一 persona.db |
| `scripts/migrate_to_evolution.py` | 数据迁移：旧记忆→演化链 |
| `scripts/docs_sync_agent.py` | 文档自动同步 Agent |

## 多人格数据隔离

```
data/
├── global_config.json              # 全局配置
├── providers/provider_keys.json    # 全局 Provider 凭证（所有人格共用）
├── adapter_port_registry.json      # 端口分配表
└── personas/{name}/                # 人格隔离目录
    ├── persona.json                # 人格定义（PersonaProfile）
    ├── orchestration.json          # 模型编排配置
    ├── adapters.json               # 平台适配器配置
    ├── experience.json             # 体验参数
    ├── persona.db                  # 统一 SQLite 数据库（WAL 模式）
    │   ├── users / user_identities # 用户数据 + 别名索引
    │   ├── evolution_records       # 演化链记录（三元组+生命周期）
    │   ├── situations              # 情景压缩数据
    │   ├── schema_patterns         # 行为模式
    │   ├── token_usage             # Token 使用记录
    │   └── cognition_events        # 认知事件
    ├── engine_state/               # 运行状态（含 orchestration.json、pinned_messages.json）
    ├── memory/                     # 语义记忆（向量存储）
    ├── diary/                      # 日记记忆（切片+向量）
    ├── image_cache/                # 图片缓存
    ├── skill_data/                 # 技能数据（含 stickers/ 表情包 RAG 库）
    └── logs/                       # 文件日志
```

## 技术栈

- **Python 3.12+**、**asyncio**、**watchdog>=4.0.0**
- **SQLite**（WAL 模式，统一 persona.db）
- **可选依赖**：`test`（pytest 等）、`provider`（tenacity、httpx）、`dev`（black、isort、mypy 等）、`quality`（tiktoken）
- **构建**：`setuptools>=61.0`

## 修改路由指南

- **新增 provider**：修改 `sirius_pulse/providers/`、`providers/routing.py`，并补测试与文档。新增 Provider 继承 `OpenAICompatibleProvider`，只需覆盖路径/认证差异。
- **修改对话主流程**：优先检查 `sirius_pulse/core/emotional_engine.py`、`core/engine_core.py`、`core/pipeline.py`、`core/brain.py`、`core/prompt_factory.py`、`core/bg_tasks.py`、`core/bg_tasks_delayed.py`、`core/bg_tasks_proactive.py`、`core/helpers.py`、`core/cognition.py`、`core/response_strategy.py`。
- **修改记忆系统**：同步检查 `sirius_pulse/memory/cold_detector.py`、`memory/evolution/chain.py`、`memory/evolution/store.py`、`memory/situation/extractor.py`、`memory/situation/store.py`、`memory/biography/view.py`、`memory/schema.py`、`memory/gap_detector.py`、`memory/basic/manager.py`、`memory/diary/manager.py`、`memory/semantic/manager.py`、`memory/user/unified_manager.py`、`memory/glossary/manager.py`、`memory/context_assembler.py`、`memory/cognition_store.py`、`memory/storage.py`。
- **修改人格生命周期**：同步检查 `sirius_pulse/persona_manager.py`、`persona_worker.py`、`persona_config.py`、`core/persona_db.py`、`core/persona_store.py`、`core/persona_generator.py`、`core/orchestration_store.py`、`platforms/runtime.py`。
- **修改平台适配**：同步检查 `sirius_pulse/adapters/base.py`、`adapters/models.py`、`platforms/onebot_v11/napcat/manager.py`、`platforms/onebot_v11/napcat/adapter.py`、`platforms/onebot_v11/protocol.py`、`platforms/runtime.py`、`platforms/persona_utils.py`。
- **修改插件系统**：同步检查 `sirius_pulse/plugins/api.py`、`plugins/base.py`、`plugins/loader.py`、`plugins/registry.py`、`plugins/executor.py`、`plugins/lexer.py`、`plugins/dispatcher.py`、`plugins/scheduler.py`、`plugins/context.py`、`plugins/models.py`、`plugins/decorators.py`、`plugins/config.py`、`plugins/events.py`、`core/plugin_intent_matcher.py`、`core/plugin_intent_verifier.py`、`webui/server_plugin_api.py`。
- **修改 SKILL 系统**：同步检查 `sirius_pulse/skills/api.py`、`skills/registry.py`、`skills/executor.py`、`skills/security.py`、`skills/models.py`、`skills/data_store.py`、`skills/dependency_resolver.py`、`skills/telemetry.py`、`core/skill_engine_context.py`、`core/helpers.py`（被动 SKILL 注册与触发分发）。
- **修改配置系统**：同步检查 `sirius_pulse/config/manager.py`、`config/models.py`、`config/helpers.py`、`config/config_builder.py`、`config/config_helpers.py`、`config/file_io.py`、`config/jsonc.py`。
- **修改外部 API**：同步更新 `sirius_pulse/__init__.py`、README、docs 与示例代码。
- **修改 WebUI**：同步检查 `sirius_pulse/webui/server_core.py`、`webui/server_utils.py`、`webui/persona_api.py`、`webui/biography_api.py`、`webui/evolution_api.py`、`webui/memory_api.py`、`webui/server_plugin_api.py`、`webui/server_skill_api.py`、`webui/auth.py`、`webui/middleware.py`、`webui/monitoring_api.py`、`webui/ws_server.py`。
- **修改状态持久化**：同步检查 `sirius_pulse/core/engine_persistence.py`、`core/engine_core.py`、`core/orchestration_store.py`、`core/persona_store.py`、`core/persona_db.py`、`utils/json_io.py`、`config/file_io.py`。
- **修改表情包系统**：同步检查 `sirius_pulse/core/engine_sticker.py`、`core/utils.py`、`skills/builtin/send_image.py`。
- **修改身份系统**：同步检查 `sirius_pulse/core/identity_resolver.py`、`core/user_lookup.py`、`core/user_lookup_mixin.py`、`memory/user/unified_manager.py`、`memory/storage.py`。
- **修改 GitHub 集成**：同步检查 `sirius_pulse/github/client.py`、`github/event_bridge.py`、`github/events.py`、`github/webhook.py`、`skills/builtin/github_monitor.py`。
- **修改 Token 系统**：同步检查 `sirius_pulse/token/token_store.py`、`token/analytics.py`、`token/usage.py`、`token/token_utils.py`、`token/utils.py`。
- **修改工具函数**：同步检查 `sirius_pulse/core/constants.py`、`core/utils.py`、`utils/json_io.py`、`utils/retry.py`、`utils/sqlite_base.py`、`utils/query_builder.py`。
