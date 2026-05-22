---
name: external-integration
description: "当需要让外部项目正确接入 Sirius Pulse 时使用，覆盖 Python API 调用、CLI 调用、配置组织和安全实践。关键词：外部接入、库调用、CLI 集成、provider 配置。"
---

# 外部接入指南

## 目标

帮助 AI 在不破坏框架边界的前提下，为外部系统提供正确、可维护的 Sirius Pulse 集成方案。

项目方向：集成时应支持“问题帮助 + 情绪价值”双目标，保障用户上下文与情感线索连续。

## 语言规范（强制）

- 本 SKILL 及所有后续新增/修改的 SKILL 必须使用中文。
- `description` 和正文必须为中文。
- 若任务中发现英文 SKILL 内容，需在同一任务中同步中文化。

## 推荐读取顺序

1. `docs/external-usage.md`
2. `docs/architecture.md`
3. `docs/full-architecture-flow.md`
4. `sirius_pulse/api/__init__.py`
5. `sirius_pulse/workspace/runtime.py`
6. `sirius_pulse/workspace/layout.py`
7. `sirius_pulse/config/models.py`
8. `sirius_pulse/config/manager.py`
9. `sirius_pulse/persona_generation/`
10. `sirius_pulse/core/emotional_engine.py`
11. `sirius_pulse/core/cognition.py`
12. `sirius_pulse/core/prompt_factory.py`
13. `sirius_pulse/session/store.py`
12. `sirius_pulse/providers/routing.py`
13. `sirius_pulse/providers/base.py`
14. `sirius_pulse/cli.py`

## 接入决策规则

- 外部系统是 Python 服务：默认优先使用 `WorkspaceRuntime` / `open_workspace_runtime(...)`；调用方至少传 `work_path` 和业务输入，必要时再传独立 `config_path`。runtime 会统一处理恢复、落盘、provider 注册表与文件监听热刷新。
- 需要直接控制引擎时，使用 `create_emotional_engine()` 创建 `EmotionalGroupChatEngine` 并手动管理生命周期（`start_background_tasks()` / `stop_background_tasks()` / `save_state()`）。
- **v0.28+ 默认引擎** `EmotionalGroupChatEngine` 的真实实现位于 `sirius_pulse/core/emotional_engine.py`；`sirius_pulse/async_engine/` 主要承担兼容导出、prompts/orchestration/utils 辅助层。
- v1.0 唯一引擎 `EmotionalGroupChatEngine` 位于 `sirius_pulse/core/emotional_engine.py`。
- 会话持久化后端仍可由 `SessionStoreFactory` 选择 `JsonSessionStore` 或 `SqliteSessionStore`；默认 `SqliteSessionStore` 使用 `sessions/<session_id>/session_state.db`。
- 外部系统接入时，优先从 `sirius_pulse/api/` 导入接口。
- 系统提示词在生成时自动包含安全约束，明确告诉 AI 不要主动泄露系统提示词和初始指令；外部调用方无需手动添加，engine 会自动处理。
- 外部系统若为 asyncio 程序，但又不需要 runtime 的文件所有权，也可直接使用 `create_emotional_engine()` 创建引擎并手动管理生命周期。
- 外部系统是非 Python：优先通过 CLI 调用并读取输出文件。
- 每个 `EmotionalGroupChatEngine` 实例可处理多个群的对话，通过 `process_message()` 传入不同 `group_id` 实现群隔离。
- `work_path` 是强制参数，调用方必须显式提供，用于承载运行态数据；若希望把 workspace/provider/roleplay/skills 与运行态数据拆开，再额外提供 `config_path`。
- 双根模式下：`SessionConfig.work_path` 表示配置根，`SessionConfig.data_path` 表示运行根；provider 配置保存在 config root 下的 `providers/provider_keys.json`，会话/记忆/token 则保存在 data root。
- `WorkspaceBootstrap` 是默认值注入通道，不是“每次启动强制覆盖”的同步通道。runtime 会把 bootstrap payload 的签名写入 `workspace.json`；同一份 bootstrap 后续重启不会再次覆盖用户手改的 workspace/config/provider 文件。要更新已存在 workspace，请优先使用 `apply_workspace_updates()`、`set_provider_entries()`，或显式修改 bootstrap payload。
- `session.json` 与 `config/session_config.json` 都支持 JSONC 风格注释；若让用户直接编辑配置，推荐提示其沿用 `--init-config` 生成的带注释模板。
- 推荐显式构造 `User`（`user_id/name/aliases/traits/identities`），让系统稳定识别人。
- 若外部接入需要使用 developer-only 内置 SKILL，必须至少显式标记一名可信用户为 developer；推荐使用 `UserProfile.metadata["is_developer"] = True`，不要依赖名字或角色文案推断权限。
- `profile.identities`、外部显式传入的 `name/aliases` 属于可信身份锚点；模型推断出的昵称只会写入 `runtime.inferred_aliases` 作为弱线索，不会自动变成稳定识人绑定。若业务平台有稳定昵称，务必显式传入。
- 通过 `identities` 可把不同环境（CLI/QQ/微信）的外部账号映射到同一 `user_id`。
- 群聊参与者若预先未知，优先直接使用 `EmotionalGroupChatEngine.process_message(...)` 逐条传入动态消息；`WorkspaceRuntime` 可作为高级封装使用。
- `EmotionalGroupChatEngine` 支持四种响应策略：IMMEDIATE（立即回复）、DELAYED（延迟回复）、SILENT（不回复）、PROACTIVE（主动发言）。
- 外部系统可通过 `engine.event_bus.subscribe()` 订阅事件流，实时接收 PERCEPTION/COGNITION/DECISION/EXECUTION 事件以及 DELAYED/PROACTIVE 触发事件。
- `Message` 的 `reply_mode` 已不在 Emotional Engine 中使用；回复策略由引擎内部决策层统一决定。
- 参与决策由引擎内部 `ThresholdEngine` 与 `ResponseStrategyEngine` 自动处理；`engagement_sensitivity` 等旧参数已不再通过 `OrchestrationPolicy` 配置。
- 用户记忆已改为群隔离：`UserManager.entries` 为 `{group_id: {user_id: UserProfile}}`。
- 日记记忆通过 `_bg_diary_promoter` 周期性生成，从 `basic_memory` 归档消息 LLM 总结为 `DiaryEntry`。
- 记忆系统配置通过 `emotional_engine` 配置字段完成，如 `basic_memory_hard_limit`、`diary_top_k`、`diary_token_budget`。
- AI 自身记忆通过 `GlossaryManager` 维护名词解释，持久化至 `{work_path}/memory/glossary/terms.json`。
- ✨ **(v0.15.0)** 自身记忆触发改回主流程内联：通过 `self_memory_extract_batch_size`、`self_memory_min_chars` 和长上下文自动触发控制，不再支持 `self_memory_extract_interval_seconds`。
- ✨ **参与决策系统** (v0.14.0)：三级架构替代旧意愿分系统：HeatAnalyzer（零 LLM 开销热度分析）→ IntentAnalyzer v2（意图分类 + target 识别）→ EngagementCoordinator（融合决策）。LLM 意图分析现由 `intent_analysis` 任务驱动，可通过 `task_enabled/task_models/task_temperatures/task_max_tokens/task_retries` 精细控制；任务关闭时使用关键词回退，但任务启用后若调用失败或解析失败，不再自动降级为关键词意图推断。多 AI 群聊里，分析器会进一步区分“当前模型自身”与“其他 AI”，并在后者场景下抑制当前模型自动回复；为降低误判，传给模型的上下文已改为最近交互链摘要，并会显式附带最近 AI 发言者、最近用户侧发言者、aliases、`environment_context`，以及当前消息命中的当前模型/其他 AI/名称含 AI 线索对象/possible-AI 候选对象等线索。对未明确点名当前模型的群控/停用类命令，还会做硬抑制，不触发当前模型回复。
- 外部系统应直接使用 `EmotionalGroupChatEngine.process_message(...)` 处理消息；`WorkspaceRuntime` 的 legacy 队列系统已在 v1.0 中移除。
- 兼容提醒：旧配置里的 `enable_intent_analysis` / `intent_analysis_model` 仍可读取，但 `ConfigManager` 会在加载时自动映射到 `task_enabled["intent_analysis"]` / `task_models["intent_analysis"]`，新的模板与持久化输出不再写回旧字段。
- **后台任务**（v0.28+）：`EmotionalGroupChatEngine.start_background_tasks()` 启动 4 个后台任务：延迟队列 ticker（10 秒）、主动触发 checker（60 秒）、观察提取 promoter（5 分钟，event_memory 批量 LLM 提取）、语义整合 consolidator（10 分钟，event_memory → semantic 画像）。
- 引擎运行时应主动更新 `runtime`（偏好标签、情绪线索、摘要），以提升拟人化体验。
- 需要按渠道身份直查时，使用 `transcript.find_user_by_channel_uid(channel, uid)`。
- workspace runtime 会自动持久化 transcript，实现重启后恢复会话；若工作目录里仍有旧 `session_state.json` 或早期 `session_state(payload)` 数据，`SqliteSessionStore` 会自动迁移到 `sessions/<session_id>/session_state.db`。
- 通过 `Transcript.token_usage_records` 获取全量 token 调用归档。
- 通过 `summarize_token_usage` 和 `build_token_usage_baseline`（来自 `token/usage.py`）输出成本与损耗基准分析。
- ✨ **(v0.11.0)** 引擎自动将 token 记录持久化至 `{work_path}/token_usage.db`（SQLite）。使用 `TokenUsageStore` + `sirius_pulse.token.analytics` 进行跨会话分析（`compute_baseline`、`group_by_actor/task/model/session`、`time_series`、`full_report`）。
- 通过 `list_roleplay_question_templates()` 获取问卷模板名，再用 `generate_humanized_roleplay_questions(template=...)` 自动生成拟人化问题清单；当前支持 `default`、`companion`、`romance`、`group_chat` 四类模板。
- 若外部系统暂时只想通过命令行拿模板数据，可直接使用 `sirius-pulse --list-roleplay-question-templates` 与 `sirius-pulse --print-roleplay-questions-template <template>`。
- 通过 `agenerate_agent_prompts_from_answers`、`agenerate_from_persona_spec`（支持 `trait_keywords`、`answers`、`dependency_files`）或 `abuild_roleplay_prompt_from_answers_and_apply` 生成并应用完整 `GeneratedSessionPreset`。
- 外部调用方推荐传入高层人格 brief，而不是完整系统提示词：优先收集人物原型、核心矛盾、关系策略、情绪原则、表达节奏、边界和小缺点，再交给生成人格 API 落成具体人物小传与语言习惯。
- 对 `abuild_roleplay_prompt_from_answers_and_apply(...)`、`aupdate_agent_prompt(...)`、`aregenerate_agent_prompt_from_dependencies(...)` 这三条持久化链路，框架会先把最新 `PersonaSpec` 和待生成快照写入 `work_path`，再发起模型调用；若生成失败，可用 `load_persona_spec(work_path, agent_key)` 恢复最近一次输入。
- 结构化人格生成默认使用 `max_tokens=5120` 和 `timeout_seconds=120.0`，并把 `timeout_seconds` 透传到 `GenerationRequest`；各同步 provider 会优先使用请求级 timeout，而不是只使用 provider 构造器上的默认 30 秒。
- 若模型返回被 ```json 包裹但未完整闭合的 JSON-like 响应，框架会显式报错并把原始响应保留在 `roleplay/generated_agent_traces/<agent_key>.json`，避免脏数据覆盖现有人格配置。
- 当外部素材文件（角色卡、语气样本、设定稿）变化时，可使用 `aregenerate_agent_prompt_from_dependencies(...)` 重新读取 `dependency_files` 并重生人格，无需重新收集问答。
- 推荐采用 agent-first：先生成并持久化 agent 资产（`roleplay/generated_agents.json`），再用 `select_generated_agent_profile(work_path, agent_key)` 选择，最后通过 `WorkspaceRuntime` 或 `create_session_config_from_selected_agent(...)` 创建会话。
- 每次生成的完整过程都会本地化到 `{work_path}/roleplay/generated_agent_traces/<agent_key>.json`；外部若需审计/回放，可调用 `load_persona_generation_traces(...)`。
- ✨ **动态模型路由**：当需要在有图像时自动升级模型时，通过 `Agent.metadata["multimodal_model"]` 配置多模态专用模型
  - 推荐使用 `create_agent_with_multimodal(name, persona, model="gpt-4o-mini", multimodal_model="gpt-4o", ...)` 便捷构造函数
  - 或使用 `auto_configure_multimodal_agent(agent, multimodal_model="gpt-4o")` 灵活配置既有 Agent
  - 引擎自动检测输入中的多媒体数据，无多媒体时使用廉价模型，有多媒体时自动升级至指定的多模态模型
  - 完全透明，无需调用方手动干预
- 通过 `history_max_messages/history_max_chars` 启用自动记忆压缩，控制 token 增长。
- ✨ **配置管理** (P1-006)：使用 `ConfigManager` 处理多环境配置
  - 支持多环境配置文件（base.json/dev.json/test.json/prod.json）
  - 支持 ${VAR_NAME} 环境变量替换语法
  - 可选验证配置的有效性
  - 示例：`from sirius_pulse.config import ConfigManager; cfg = ConfigManager.load_from_json('config/base.json')`
- ✨ **缓存层** (P2-001)：使用 `cache/` 模块实现高效的响应缓存
  - MemoryCache：本地内存缓存，支持 LRU 策略和 TTL 过期
  - 通过 `CacheBackend` 抽象实现自定义后端
  - 使用 `generate_cache_key()` 生成确定性的 key（支持温度感知）
  - 示例：`from sirius_pulse.cache import MemoryCache; cache = MemoryCache(max_size=1000, ttl=3600)`
- ✨ **性能监控** (P2-002)：通过 `performance/` 模块追踪和优化应用性能
  - ExecutionMetrics：记录单次执行的时间和内存消耗
  - MetricsCollector：聚合执行指标，提供统计分析
  - PerformanceProfiler：上下文管理器用于代码块分析
  - @profile_sync/@profile_async：装饰器用于函数级性能追踪
  - Benchmark：支持同步/异步/并发性能基准测试
  - 示例：`from sirius_pulse.performance import PerformanceProfiler; with PerformanceProfiler("task"): ...`
- ✨ **SKILL 系统**：通过 `skills/` 模块让 AI 在运行时调用外部 Python 代码
  - 默认：`enable_skills=True`；框架会先加载包内置 SKILL（当前包含 `system_info`、`learn_term`、`url_content_reader`、`bing_search` 与 developer-only 的 `desktop_screenshot`），再加载 workspace `skills/` 目录。SKILL 文件默认放在 `{work_path}/skills/`，双根布局时位于 `config_root/skills/`。若只想保留目录结构、不执行 SKILL，可显式设置 `enable_skills=False`
  - 加载时机：框架启动时预加载，`skills/` 目录变化时自动全量重载；不再在每条 message 路径上扫描 SKILL
  - 覆盖规则：如果 workspace 中存在同名文件（如 `skills/system_info.py`），则以 workspace 版本覆盖内置实现
  - 权限模型：developer-only SKILL 只会在 developer 当前轮次出现在提示词中，执行时 runtime 会再次校验当前调用者是否被显式标记为 developer
  - SKILL 文件需导出 `SKILL_META` 字典（含 name, description, parameters, 可选 dependencies、`developer_only` 与 `silent`）和 `run(**kwargs)` 函数；如需审计调用者，可显式接收 `invocation_context`。`silent=True` 时 SKILL 结果不追加到回复文本，仅保留在内部元数据中
  - 依赖自动安装：加载 SKILL 前自动扫描 `SKILL_META["dependencies"]` 和 import 语句，用 `uv pip install`（回退 `pip`）安装缺失包。内置 SKILL 与 workspace SKILL 共用这条流程，可通过 `auto_install_skill_deps=False` 关闭
  - 持久化：每个 SKILL 自动获得独立的 JSON 键值存储（`SkillDataStore`），通过 `data_store` 参数注入
  - 超时：`skill_execution_timeout`（默认 30 秒），超时返回 `SkillResult(success=False)`
  - 引擎自动检测 AI 回复中的内置 `[SKILL_CALL: name | {params}]` 标记并执行，结果会先规范化为内部文本/多模态通道后再重新生成
  - 若 SKILL 返回 `text_blocks`、`multimodal_blocks`、`internal_metadata`，模型只会看到内部推理通道；最终回复会被约束为只输出用户有用的结论，不复述字段名、`mime_type`、`label`、路径或 URL
  - 导入：`from sirius_pulse import SkillRegistry, SkillExecutor, SkillDataStore, SkillInvocationContext, resolve_skill_dependencies`
  - 示例 SKILL：`examples/skills/system_info.py`
- 任何情况下，不应在编排核心中写入 provider 细节（provider 抽象优先原则）。
  - 所有 provider 特定逻辑都应在 `sirius_pulse/providers/` 目录下实现。
  - `sirius_pulse/core/emotional_engine.py`（v1.0 唯一引擎）通过 `LLMProvider`/`AsyncLLMProvider` 抽象与 provider 交互，不依赖任何具体实现。
- 内部重构若影响外部接口（当前未发布阶段），可直接升级 `api/`，并同步外部文档与示例。
- 内部新增功能必须同步在 `api/` 暴露可调用接口。
- 异步引擎在同步 provider 场景下会自动线程化调用，避免阻塞事件循环。

## 最小可用接入模板

- Python 调用示例：`examples/external_api_usage.py`
- 动态群聊示例：`examples/dynamic_group_chat_usage.py`
- CLI 调用示例：`sirius-pulse --config examples/session.json --work-path data/session_runtime --output transcript.json`
- 恢复会话示例（默认自动恢复）：`sirius-pulse --config examples/session.json --work-path data/session_runtime`
- 如需禁用自动恢复，可在 `main.py` 入口使用 `--no-resume`。

## 变更同步要求（强制）

当以下内容发生变化时，必须同步更新本 SKILL：

1. 外部接入方式（API 或 CLI）
2. 配置结构或关键参数
3. provider 接入策略或边界约束

并同步更新：

- `README.md`（用户可见用法）
- `docs/external-usage.md`
- `docs/architecture.md`（若边界变化）

## Provider 选型补充

- OpenAI 兼容上游：使用 `OpenAICompatibleProvider`。
- 阿里云百炼上游：优先使用 `AliyunBailianProvider`（默认 `https://dashscope.aliyuncs.com/compatible-mode`，兼容传入 `/compatible-mode/v1` 后缀）。
- 智谱 BigModel 上游：优先使用 `BigModelProvider`（默认 `https://open.bigmodel.cn/api/paas/v4`，接口 `POST /chat/completions`，兼容传入根域名或完整 `api/paas/v4` 前缀）。
- DeepSeek 上游：优先使用 `DeepSeekProvider`（默认 `https://api.deepseek.com`，兼容传入 `/v1` 后缀，接口 `POST /chat/completions`）。
- SiliconFlow 上游：优先使用 `SiliconFlowProvider`（默认 `https://api.siliconflow.cn`，兼容传入 `/v1` 后缀）。
- 火山方舟上游：优先使用 `VolcengineArkProvider`（默认 `https://ark.cn-beijing.volces.com/api/v3`，接口 `/api/v3/chat/completions`）。
- 多平台自动选择：使用 `AutoRoutingProvider` + `ProviderRegistry`，通过模型前缀路由。
- 交互模式下可用 `/provider platforms|add|remove|list` 管理 API Key（持久化在 config root 下的 `providers/provider_keys.json`）。
- `/provider add` 需提供 `healthcheck_model`，注册时会执行可用性检测：
  `/provider add <type> <api_key> <healthcheck_model> [base_url]`
- 框架会执行统一 Provider 检测流程：配置检查（平台名/API）-> 平台适配检查（仅允许已适配平台）-> 可用性检查（healthcheck model）。
- **多模型协同**：`OrchestrationPolicy` 通过 `unified_model` 或 `task_models` 工作；若未显式传入 orchestration，`SessionConfig` 会默认用主 agent 模型构造 `unified_model`。图片输入不会触发独立解析任务，而是直接进入主模型；如需自动升级多模态模型，请配置 `Agent.metadata["multimodal_model"]`。
- 多模态接入补充：对 `OpenAICompatibleProvider` / `AliyunBailianProvider` 这类 HTTP provider，`multimodal_inputs` 中的本地图片路径或 `file://` URI 会在发送前自动转换为 Data URL；若传公网 URL，需确保上游能直接下载且响应头带 `Content-Type` / `Content-Length`。
- 生产环境建议配置 `task_retries` 与多模态限流参数，避免上游抖动与超长输入导致的失败。
- ✨ **Provider 中间件** (P1-003)：支持在 provider 调用前后插入可组合的中间件，功能包括：
  - 速率限制（固定窗口、令牌桶）
  - 自动重试（指数退避）与断路器保护
  - 成本计量和使用统计
  - 通过 `from sirius_pulse import MiddlewareChain, RateLimiterMiddleware, RetryMiddleware, CircuitBreakerMiddleware, CostMetricsMiddleware` 导入
  - 支持自定义中间件扩展（继承 Middleware ABC）


