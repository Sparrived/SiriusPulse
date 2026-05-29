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

1. `docs/persona-lifecycle.md`
2. `docs/engine-deep-dive.md`
3. `docs/persistence-system.md`
4. `docs/skill-guide.md`
5. `docs/provider-system.md`
6. `docs/platforms.md`
7. `README.md`
8. `sirius_pulse/__init__.py`（顶层公开 API 清单）
9. `sirius_pulse/persona_manager.py`
10. `sirius_pulse/persona_worker.py`
11. `sirius_pulse/persona_config.py`
12. `sirius_pulse/platforms/runtime.py`
13. `sirius_pulse/platforms/onebot_v11/napcat/manager.py`
14. `sirius_pulse/platforms/onebot_v11/napcat/adapter.py`
15. `sirius_pulse/platforms/onebot_v11/protocol.py`
16. `sirius_pulse/plugins/loader.py`
17. `sirius_pulse/plugins/registry.py`
18. `sirius_pulse/plugins/executor.py`
19. `sirius_pulse/plugins/config.py`
20. `sirius_pulse/plugins/decorators.py`
21. `sirius_pulse/core/emotional_engine.py`（组合模式最终类，委托 shim）
22. `sirius_pulse/core/engine_core.py`（引擎基类：__init__、公开 API、委托方法）
23. `sirius_pulse/core/pipeline.py`（Pipeline 组件：5 阶段管线）
24. `sirius_pulse/core/prompt_factory.py`（PromptFactory：无状态 prompt 构建工具类，含 StyleAdapter 风格适配）
25. `sirius_pulse/core/bg_tasks.py`（BackgroundTasks 组件：后台任务管理）
26. `sirius_pulse/core/bg_tasks_delayed.py`（DelayedQueueTasks 组件：延迟队列任务）
27. `sirius_pulse/core/bg_tasks_proactive.py`（ProactiveTasks 组件：主动消息任务）
28. `sirius_pulse/core/helpers.py`（Helpers 组件：技能集成、被动 SKILL 注册与触发分发、token 记录）
29. `sirius_pulse/core/engine_persistence.py`（EnginePersistence 组件 + EngineStateStore：状态持久化）
30. `sirius_pulse/core/engine_sticker.py`（EngineSticker 组件：表情包系统）
31. `sirius_pulse/core/constants.py`（核心引擎常量定义）
32. `sirius_pulse/core/utils.py`（核心引擎工具函数）
33. `sirius_pulse/core/skill_engine_context.py`（SkillEngineContextImpl：被动 SKILL 与引擎交互适配器）
34. `sirius_pulse/core/cognition.py`
35. `sirius_pulse/core/response_strategy.py`
36. `sirius_pulse/core/model_router.py`
37. `sirius_pulse/core/threshold_engine.py`
38. `sirius_pulse/core/rhythm.py`
39. `sirius_pulse/core/events.py`
40. `sirius_pulse/memory/basic/manager.py`
41. `sirius_pulse/memory/diary/manager.py`
42. `sirius_pulse/memory/semantic/manager.py`
43. `sirius_pulse/memory/user/simple.py`
44. `sirius_pulse/memory/glossary/manager.py`
45. `sirius_pulse/memory/context_assembler.py`
46. `sirius_pulse/memory/cognition_store.py`
47. `sirius_pulse/skills/registry.py`
48. `sirius_pulse/skills/executor.py`
49. `sirius_pulse/skills/security.py`
50. `sirius_pulse/skills/models.py`
51. `sirius_pulse/skills/sticker/__init__.py`（表情包子系统入口）
52. `sirius_pulse/providers/routing.py`
53. `sirius_pulse/providers/base.py`
54. `sirius_pulse/config/manager.py`
55. `sirius_pulse/config/models.py`
56. `sirius_pulse/config/helpers.py`
57. `sirius_pulse/models/models.py`
58. `sirius_pulse/models/persona.py`
59. `sirius_pulse/models/emotion.py`
60. `sirius_pulse/models/intent_v3.py`
61. `sirius_pulse/session/store.py`
62. `sirius_pulse/utils/layout.py`
63. `sirius_pulse/utils/json_io.py`（公共 JSON I/O 工具）
64. `sirius_pulse/utils/retry.py`（通用异步重试工具）
65. `sirius_pulse/webui/server.py`
66. `sirius_pulse/webui/auth.py`（JWT 认证管理器）
67. `sirius_pulse/webui/middleware.py`（认证中间件）
68. `sirius_pulse/webui/monitoring_api.py`（监控 API）
69. `sirius_pulse/webui/ws_server.py`（WebSocket 事件推送服务）
70. `tests/test_engine_event_stream.py`
71. `main.py`

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
    │       ├── NapCatAdapter ── NapCat OneBot v11 WS
    │       ├── BasicMemoryManager + DiaryManager + SemanticMemory
    │       ├── ModelRouter（任务感知模型选择）
    │       ├── PluginExecutor（插件执行）
    │       └── SkillRegistry + SkillExecutor
    ├── EmbeddingClient ── Embedding 微服务（共享，主进程启动）
    └── ...（多个人格并行）
```

### 关键事实

- **推荐生产入口** 是 `PersonaManager`（多人格生命周期管理）；单个人格可直接创建 `EngineRuntime` 或 `EmotionalGroupChatEngine`。
- `EmotionalGroupChatEngine` 是唯一引擎，采用**组合模式**：`emotional_engine.py`（最终类，委托 shim）继承 `_EmotionalGroupChatEngineBase`（基类），通过以下组件实现功能：
  - `engine._pipeline: Pipeline` — 5 阶段管线（感知→认知→决策→执行→后台）
  - `engine._bg_tasks_mgr: BackgroundTasks` — 后台任务管理（委托给 `ProactiveTasks` 和 `DelayedQueueTasks`）
  - `engine._helpers: Helpers` — 技能集成、被动 SKILL 注册与触发分发、token 记录
  - `engine._persistence: EnginePersistence` — 状态持久化（`EngineStateStore` 负责序列化）
  - `engine._sticker: EngineSticker` — 表情包系统（初始化/选择/发送）
  - `engine._prompt_factory: PromptFactory` — 无状态 prompt 构建
- `engine_core.py` 通过委托方法（thin wrappers）保持向后兼容的 API。
- `sirius_pulse/async_engine/` 承担 prompts/orchestration/utils 辅助层。
- 一个 `SessionConfig` 只对应一个主 AI，主 AI 由 `preset=AgentPreset(...)` 描述。
- `User` 是 `Participant` 的公开别名，不存在第二套独立的人类参与者模型。
- 配置资产与运行态数据支持双根分离：config root 负责配置与角色资产，data root 负责 session、memory、token 与 skill_data。
- `sirius_pulse/__init__.py` 是顶层公开 API 统一重导出（严格 `__all__`），所有对外接口从这里导入。
- WebUI 支持 JWT 认证（admin/viewer 角色）、WebSocket 事件推送、监控 API。

### 模块职责

| 模块 | 主要职责 | 不应承担的职责 |
|------|---------|-------------|
| `sirius_pulse/__init__.py` | 顶层公开 API 统一重导出 | 不直接实现底层编排或路径布局 |
| `sirius_pulse/persona_manager.py` | **推荐生产入口**：多人格生命周期管理 | 不实现底层对话生成 |
| `sirius_pulse/persona_worker.py` | 单个人格子进程入口 | 不管理其他人格 |
| `sirius_pulse/persona_config.py` | 人格级配置模型 | 不处理全局配置 |
| `sirius_pulse/platforms/` | 平台适配层：`platforms/onebot_v11/napcat/`（NapCat 适配器、管理器、协议解析）、`runtime.py`（EngineRuntime 封装） | 不介入高层人格调度 |
| `sirius_pulse/webui/` | WebUI REST API + 静态页面 + JWT 认证 + WebSocket 事件推送 + 监控 API（含插件管理 API） | 不直接操作 NapCat 进程 |
| `sirius_pulse/plugins/` | 插件系统：插件加载、注册表、执行器、配置管理、@command 装饰器、PluginContext、响应调度、事件定义 | 不负责 SKILL 执行 |
| `sirius_pulse/core/` | 编排核心：EmotionalGroupChatEngine（组合模式：engine_core + pipeline + prompt_factory + bg_tasks + helpers + engine_persistence + engine_sticker）、认知分析、响应策略、阈值引擎、节奏分析、事件总线、身份解析、表情包决策 | 不负责人格目录组织 |
| `sirius_pulse/memory/` | 基础记忆、日记记忆、语义记忆、用户管理、名词解释、上下文组装 | 不直接决定 provider 路由 |
| `sirius_pulse/providers/` | provider 协议、具体上游实现、注册表、自动路由 | 不介入高层人格生命周期 |
| `sirius_pulse/skills/` | SKILL 注册、依赖解析、执行、安全校验、遥测、数据存储；被动 SKILL 支持（BackgroundTaskSpec/TriggerSpec/SkillEngineContext）；表情包子系统 `skills/sticker/`（向量检索、偏好管理、学习、反馈） | 不负责 provider 注册表 |
| `sirius_pulse/config/` | SessionConfig、WorkspaceConfig、ConfigManager、JSONC、helpers | 不改变核心对话契约 |
| `sirius_pulse/models/` | 数据契约：Message、Participant、EmotionState、IntentAnalysisV3 等 | 不处理持久化 |
| `sirius_pulse/session/` | SessionStore（Json/Sqlite）、持久化后端 | 不介入对话逻辑 |
| `sirius_pulse/token/` | Token 记录、SQLite 持久化、成本分析 | 不介入对话逻辑 |
| `sirius_pulse/utils/` | 工具函数、WorkspaceLayout 路径布局、JSON I/O、异步重试 | 不改变核心对话契约 |

## 常用文件路径

| 路径                                        | 说明                                 |
| ----------------------------------------- | ---------------------------------- |
| `main.py`                                 | 统一 CLI 入口（默认启动 WebUI；`run` 启动全部人格） |
| `sirius_pulse/core/emotional_engine.py`    | 核心情感群聊引擎（组合模式最终类，委托 shim）       |
| `sirius_pulse/core/engine_core.py`         | 引擎基类（__init__、公开 API、委托方法）           |
| `sirius_pulse/core/pipeline.py`            | Pipeline 组件（5 阶段管线：感知→认知→决策→执行→后台） |
| `sirius_pulse/core/bg_tasks.py`            | BackgroundTasks 组件（后台任务管理，委托给 proactive/delayed） |
| `sirius_pulse/core/bg_tasks_delayed.py`    | DelayedQueueTasks 组件（延迟队列任务）  |
| `sirius_pulse/core/bg_tasks_proactive.py`  | ProactiveTasks 组件（主动消息任务）     |
| `sirius_pulse/core/engine_persistence.py`  | EnginePersistence 组件 + EngineStateStore（状态持久化） |
| `sirius_pulse/core/engine_sticker.py`      | EngineSticker 组件（表情包系统：初始化/选择/发送） |
| `sirius_pulse/core/prompt_factory.py`     | PromptFactory：无状态 prompt 构建工具类（含 StyleAdapter 风格适配） |
| `sirius_pulse/core/helpers.py`             | Helpers 组件（技能集成、被动 SKILL 注册与触发分发、token 记录） |
| `sirius_pulse/core/constants.py`           | 核心引擎常量定义（时间、Token、记忆等魔法数字） |
| `sirius_pulse/core/utils.py`              | 核心引擎工具函数（时间戳、XML 清理、表情包标签解析） |
| `sirius_pulse/core/skill_engine_context.py` | SkillEngineContextImpl：被动 SKILL 与引擎交互适配器 |
| `sirius_pulse/utils/json_io.py`           | 公共 JSON I/O 工具（原子写入 + 安全读取） |
| `sirius_pulse/utils/retry.py`             | 通用异步重试工具                        |
| `sirius_pulse/embedding/server.py`         | Embedding 微服务端（aiohttp + asyncio.Queue 批量合并推理） |
| `sirius_pulse/embedding/client.py`         | Embedding 同步客户端（urllib） |
| `sirius_pulse/persona_generation/`         | 人格资产生成子包（templates + builders） |
| `sirius_pulse/persona_manager.py`          | 多人格生命周期管理                          |
| `sirius_pulse/persona_worker.py`           | 子进程入口                              |
| `sirius_pulse/persona_config.py`           | 人格级配置模型                            |
| `sirius_pulse/platforms/onebot_v11/napcat/manager.py` | NapCat 多实例管理                       |
| `sirius_pulse/platforms/runtime.py`        | 单人格运行时封装                           |
| `sirius_pulse/webui/server.py`             | WebUI REST API 主入口                  |
| `sirius_pulse/webui/auth.py`              | JWT 认证管理器（HMAC-SHA256 签名，admin/viewer 角色） |
| `sirius_pulse/webui/middleware.py`         | 认证中间件（白名单放行 + RBAC 权限控制）      |
| `sirius_pulse/webui/monitoring_api.py`     | 监控 API（全局概览、单人格指标、健康检查）     |
| `sirius_pulse/webui/ws_server.py`         | WebSocket 事件推送服务（桥接 SessionEventBus 到前端） |
| `sirius_pulse/__init__.py`                 | 顶层公开 API 导出清单（严格 `__all__`）        |
| `tests/conftest.py`                       | 测试最小 fixture                       |
| `scripts/ci_check.py`                     | 统一 CI 检查脚本                         |

## 多人格数据隔离

```
data/
├── global_config.json              # 全局配置
├── providers/provider_keys.json    # 全局 Provider 凭证
├── adapter_port_registry.json      # 端口分配表
└── personas/{name}/                # 人格隔离目录
    ├── persona.json                # 人格定义
    ├── orchestration.json          # 模型编排
    ├── adapters.json               # 平台适配器
    ├── experience.json             # 体验参数
    ├── engine_state/               # 运行状态
    ├── memory/                     # 语义记忆
    ├── diary/                      # 日记记忆
    ├── image_cache/                # 图片缓存
    ├── skill_data/                 # 技能数据（含 stickers/ 表情包 RAG 库）
    └── logs/                       # 文件日志
```

## 技术栈

- **Python 3.12+**、**asyncio**、**watchdog>=4.0.0**
- **可选依赖**：`test`（pytest 等）、`provider`（tenacity、httpx）、`dev`（black、isort、mypy 等）、`quality`（tiktoken）
- **构建**：`setuptools>=61.0`

## 修改路由指南

- **新增 provider**：修改 `sirius_pulse/providers/`、`sirius_pulse/providers/routing.py`，并补测试与文档。
- **修改对话主流程**：优先检查 `sirius_pulse/core/emotional_engine.py`、`core/engine_core.py`、`core/pipeline.py`、`core/prompt_factory.py`、`core/bg_tasks.py`、`core/bg_tasks_delayed.py`、`core/bg_tasks_proactive.py`、`core/helpers.py`、`core/cognition.py`、`core/response_strategy.py`。
- **修改记忆系统**：同步检查 `sirius_pulse/memory/basic/manager.py`、`memory/diary/manager.py`、`memory/semantic/manager.py`、`memory/user/simple.py`、`memory/glossary/manager.py`、`memory/context_assembler.py`、`memory/cognition_store.py`、`core/identity_resolver.py`。
- **修改人格生命周期**：同步检查 `sirius_pulse/persona_manager.py`、`persona_worker.py`、`persona_config.py`、`platforms/runtime.py`。
- **修改平台适配**：同步检查 `sirius_pulse/platforms/onebot_v11/napcat/manager.py`、`platforms/onebot_v11/napcat/adapter.py`、`platforms/onebot_v11/protocol.py`、`platforms/runtime.py`。
- **修改插件系统**：同步检查 `sirius_pulse/plugins/loader.py`、`plugins/registry.py`、`plugins/executor.py`、`plugins/config.py`、`plugins/decorators.py`、`plugins/context.py`、`plugins/dispatcher.py`、`plugins/events.py`、`webui/server_plugin_api.py`。
- **修改 SKILL 系统**：同步检查 `sirius_pulse/skills/registry.py`、`skills/executor.py`、`skills/security.py`、`skills/models.py`、`core/skill_engine_context.py`、`core/helpers.py`（被动 SKILL 注册与触发分发）。
- **修改配置系统**：同步检查 `sirius_pulse/config/manager.py`、`config/models.py`、`config/helpers.py`。
- **修改外部 API**：同步更新 `sirius_pulse/__init__.py`、README、docs 与示例代码。
- **修改 WebUI**：同步检查 `sirius_pulse/webui/server.py`、`webui/server_core.py`、`webui/server_utils.py`、`webui/persona_api.py`、`webui/memory_api.py`、`webui/napcat_api.py`、`webui/server_skill_api.py`、`webui/auth.py`、`webui/middleware.py`、`webui/monitoring_api.py`、`webui/ws_server.py`。
- **修改状态持久化**：同步检查 `sirius_pulse/core/engine_persistence.py`、`core/engine_core.py`、`utils/json_io.py`。
- **修改表情包系统**：同步检查 `sirius_pulse/core/engine_sticker.py`、`core/utils.py`、`skills/sticker/`。
- **修改工具函数**：同步检查 `sirius_pulse/core/constants.py`、`core/utils.py`、`utils/json_io.py`、`utils/retry.py`。
