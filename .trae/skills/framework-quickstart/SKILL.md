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
- 各模块的边界如何划分
- 哪些文件是当前架构的事实来源

## 语言规范

- 本仓库所有 SKILL 文件必须使用中文编写。
- 后续新增或修改任意 SKILL 时，frontmatter 的 `description` 与正文均需使用中文。
- 若发现历史 SKILL 出现英文内容，需在当前任务中一并改为中文。

## 阅读顺序（先做这个）

1. `docs/architecture.md`
2. `docs/full-architecture-flow.md`
3. `docs/persona-lifecycle.md`
4. `docs/engine-deep-dive.md`
5. `docs/persistence-system.md`
6. `docs/skill-guide.md`
7. `docs/provider-system.md`
8. `docs/platforms.md`
9. `README.md`
10. `sirius_pulse/__init__.py`（顶层公开 API 清单）
11. `sirius_pulse/persona_manager.py`
12. `sirius_pulse/persona_worker.py`
13. `sirius_pulse/persona_config.py`
14. `sirius_pulse/platforms/runtime.py`
15. `sirius_pulse/platforms/onebot_v11/napcat/manager.py`
16. `sirius_pulse/platforms/onebot_v11/napcat/adapter.py`
17. `sirius_pulse/platforms/onebot_v11/protocol.py`
18. `sirius_pulse/plugins/loader.py`
19. `sirius_pulse/plugins/registry.py`
20. `sirius_pulse/plugins/executor.py`
21. `sirius_pulse/plugins/config.py`
22. `sirius_pulse/plugins/decorators.py`
23. `sirius_pulse/core/emotional_engine.py`（最终类定义，Mixin 组合）
19. `sirius_pulse/core/engine_core.py`（引擎基类：__init__、公开 API、持久化）
20. `sirius_pulse/core/pipeline.py`（5 阶段管线 Mixin）
21. `sirius_pulse/core/prompt_factory.py`（PromptFactory：无状态 prompt 构建工具类，含 StyleAdapter 风格适配）
22. `sirius_pulse/core/bg_tasks.py`（6 个后台任务 Mixin）
23. `sirius_pulse/core/helpers.py`（技能集成、被动 SKILL 注册与触发分发、token 记录 Mixin）
23a. `sirius_pulse/core/skill_engine_context.py`（SkillEngineContextImpl：被动 SKILL 与引擎交互适配器）
24. `sirius_pulse/core/cognition.py`
25. `sirius_pulse/core/response_strategy.py`
27. `sirius_pulse/core/model_router.py`
28. `sirius_pulse/core/threshold_engine.py`
29. `sirius_pulse/core/rhythm.py`
30. `sirius_pulse/core/events.py`
31. `sirius_pulse/memory/basic/manager.py`
32. `sirius_pulse/memory/diary/manager.py`
33. `sirius_pulse/memory/semantic/manager.py`
34. `sirius_pulse/memory/user/simple.py`
35. `sirius_pulse/memory/glossary/manager.py`
36. `sirius_pulse/memory/context_assembler.py`
37. `sirius_pulse/memory/cognition_store.py`
38. `sirius_pulse/skills/registry.py`
39. `sirius_pulse/skills/executor.py`
40. `sirius_pulse/skills/security.py`
41. `sirius_pulse/skills/models.py`
42. `sirius_pulse/skills/sticker/__init__.py`（表情包子系统入口）
43. `sirius_pulse/providers/routing.py`
44. `sirius_pulse/providers/base.py`
45. `sirius_pulse/config/manager.py`
46. `sirius_pulse/config/models.py`
47. `sirius_pulse/config/helpers.py`
48. `sirius_pulse/models/models.py`
49. `sirius_pulse/models/persona.py`
50. `sirius_pulse/models/emotion.py`
51. `sirius_pulse/models/intent_v3.py`
52. `sirius_pulse/session/store.py`
53. `sirius_pulse/utils/layout.py`
54. `sirius_pulse/webui/server.py`
55. `tests/test_engine_event_stream.py`
56. `main.py`

## 心智模型

### 多人格进程架构（v1.0 核心）

```
主进程（python main.py run）
    │
    ├── PersonaManager          # 扫描人格目录、端口分配、启停调度
    ├── WebUIServer             # aiohttp REST API + 静态页面
    └── NapCatManager           # NapCat 全局安装/多实例管理
            │
            ▼
    子进程（独立控制台窗口）
    ├── PersonaWorker ── EngineRuntime ── EmotionalGroupChatEngine
    │       │
    │       ├── NapCatAdapter ── NapCat OneBot v11 WS
    │       ├── BasicMemoryManager + DiaryManager + SemanticMemory
    │       ├── ModelRouter（任务感知模型选择）
    │       ├── PluginExecutor（插件执行）
    │       └── SkillRegistry + SkillExecutor
    ├── EmbeddingClient ── Embedding 微服务（共享，主进程启动）
    └── ...（多个人格并行）
```

### 关键事实

- **v1.1.0 推荐生产入口** 是 `PersonaManager`（多人格生命周期管理）；单个人格可直接创建 `EngineRuntime` 或 `EmotionalGroupChatEngine`。
- `EmotionalGroupChatEngine` 是 **v1.0 唯一引擎**，采用 Mixin 架构：`emotional_engine.py`（最终类）= `engine_core.py`（基类）+ `pipeline.py`（管线）+ `prompt_factory.py`（PromptFactory：无状态 prompt 构建）+ `bg_tasks.py`（后台任务）+ `helpers.py`（辅助）。
- `sirius_pulse/async_engine/` 承担 prompts/orchestration/utils 辅助层。
- 一个 `SessionConfig` 只对应一个主 AI，主 AI 由 `preset=AgentPreset(...)` 描述。
- `User` 是 `Participant` 的公开别名，不存在第二套独立的人类参与者模型。
- 配置资产与运行态数据支持双根分离：config root 负责配置与角色资产，data root 负责 session、memory、token 与 skill_data。
- `sirius_pulse/__init__.py` 是顶层公开 API 统一重导出（严格 `__all__`），所有对外接口从这里导入。

### 模块职责

| 模块 | 主要职责 | 不应承担的职责 |
|------|---------|-------------|
| `sirius_pulse/__init__.py` | 顶层公开 API 统一重导出 | 不直接实现底层编排或路径布局 |
| `sirius_pulse/persona_manager.py` | **推荐生产入口**：多人格生命周期管理 | 不实现底层对话生成 |
| `sirius_pulse/persona_worker.py` | 单个人格子进程入口 | 不管理其他人格 |
| `sirius_pulse/persona_config.py` | 人格级配置模型 | 不处理全局配置 |
| `sirius_pulse/platforms/` | 平台适配层：`platforms/onebot_v11/napcat/`（NapCat 适配器、管理器、协议解析）、`runtime.py`（EngineRuntime 封装） | 不介入高层人格调度 |
| `sirius_pulse/webui/` | WebUI REST API + 静态页面（含插件管理 API） | 不直接操作 NapCat 进程 |
| `sirius_pulse/plugins/` | 插件系统：插件加载、注册表、执行器、配置管理、@command 装饰器、PluginContext、响应调度、事件定义 | 不负责 SKILL 执行 |
| `sirius_pulse/core/` | 编排核心：EmotionalGroupChatEngine（Mixin 架构：engine_core + pipeline + prompt_factory + bg_tasks + helpers）、认知分析、响应策略、阈值引擎、节奏分析、事件总线、身份解析、表情包决策 | 不负责人格目录组织 |
| `sirius_pulse/memory/` | 基础记忆、日记记忆、语义记忆、用户管理、名词解释、上下文组装 | 不直接决定 provider 路由 |
| `sirius_pulse/providers/` | provider 协议、具体上游实现、注册表、自动路由 | 不介入高层人格生命周期 |
| `sirius_pulse/skills/` | SKILL 注册、依赖解析、执行、安全校验、遥测、数据存储；被动 SKILL 支持（BackgroundTaskSpec/TriggerSpec/SkillEngineContext）；表情包子系统 `skills/sticker/`（向量检索、偏好管理、学习、反馈） | 不负责 provider 注册表 |
| `sirius_pulse/config/` | SessionConfig、WorkspaceConfig、ConfigManager、JSONC、helpers | 不改变核心对话契约 |
| `sirius_pulse/models/` | 数据契约：Message、Participant、EmotionState、IntentAnalysisV3 等 | 不处理持久化 |
| `sirius_pulse/session/` | SessionStore（Json/Sqlite）、持久化后端 | 不介入对话逻辑 |
| `sirius_pulse/token/` | Token 记录、SQLite 持久化、成本分析 | 不介入对话逻辑 |
| `sirius_pulse/utils/` | 工具函数、WorkspaceLayout 路径布局 | 不改变核心对话契约 |

## 修改路由指南

- **新增 provider**：修改 `sirius_pulse/providers/`、`sirius_pulse/providers/routing.py`，并补测试与文档。
- **修改对话主流程**：优先检查 `sirius_pulse/core/emotional_engine.py`、`core/engine_core.py`、`core/pipeline.py`、`core/prompt_factory.py`、`core/bg_tasks.py`、`core/helpers.py`、`core/cognition.py`、`core/response_strategy.py`。
- **修改记忆系统**：同步检查 `sirius_pulse/memory/basic/manager.py`、`memory/diary/manager.py`、`memory/semantic/manager.py`、`memory/user/simple.py`、`memory/glossary/manager.py`、`memory/context_assembler.py`、`memory/cognition_store.py`、`core/identity_resolver.py`。
- **修改人格生命周期**：同步检查 `sirius_pulse/persona_manager.py`、`persona_worker.py`、`persona_config.py`、`platforms/runtime.py`。
- **修改平台适配**：同步检查 `sirius_pulse/platforms/onebot_v11/napcat/manager.py`、`platforms/onebot_v11/napcat/adapter.py`、`platforms/onebot_v11/protocol.py`、`platforms/runtime.py`。
- **修改插件系统**：同步检查 `sirius_pulse/plugins/loader.py`、`plugins/registry.py`、`plugins/executor.py`、`plugins/config.py`、`plugins/decorators.py`、`plugins/context.py`、`plugins/dispatcher.py`、`plugins/events.py`、`webui/server_plugin_api.py`。
- **修改 SKILL 系统**：同步检查 `sirius_pulse/skills/registry.py`、`skills/executor.py`、`skills/security.py`、`skills/models.py`、`core/skill_engine_context.py`、`core/helpers.py`（被动 SKILL 注册与触发分发）。
- **修改配置系统**：同步检查 `sirius_pulse/config/manager.py`、`config/models.py`、`config/helpers.py`。
- **修改外部 API**：同步更新 `sirius_pulse/__init__.py`、README、docs 与示例代码。
- **修改 WebUI**：同步检查 `sirius_pulse/webui/server.py`、`webui/server_core.py`、`webui/server_utils.py`、`webui/persona_api.py`、`webui/memory_api.py`、`webui/napcat_api.py`、`webui/server_skill_api.py`。
