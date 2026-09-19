---
name: external-integration
description: "当需要让外部项目正确接入 Sirius Pulse 时使用，覆盖 Python API 调用、CLI 调用、配置组织和安全实践。关键词：外部接入、库调用、CLI 集成、AMKR 边界、任务名契约。"
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

1. `docs/guide/architecture-overview.md`
2. `docs/guide/engine-architecture.md`
3. `docs/reference/python-api.md`
4. `docs/reference/provider-config.md`（AMKR 接入配置与任务名契约）
5. `docs/modules/provider-system.md`（AMKR 接入模块详解）
6. `sirius_pulse/__init__.py`（公开符号统一从顶层导出，没有 `sirius_pulse/api/` 子模块）
7. `sirius_pulse/platforms/runtime.py`
8. `sirius_pulse/config/models.py`
9. `sirius_pulse/config/manager.py`
10. `sirius_pulse/persona_generation/`
11. `sirius_pulse/core/emotional_engine.py`
12. `sirius_pulse/core/cognition.py`
13. `sirius_pulse/core/model_router.py`
14. `sirius_pulse/core/prompt_factory.py`
15. `sirius_pulse/session/store.py`
16. `sirius_pulse/providers/base.py`
17. `sirius_pulse/providers/amkr.py`
18. `sirius_pulse/cli.py`

## 接入决策规则

- 外部系统是 Python 服务：默认优先使用 `EngineRuntime`（`from sirius_pulse.platforms import EngineRuntime`）；构造时至少传 `work_path`，必要时再传 `global_data_path`。runtime 会统一处理恢复、落盘、AMKR 任务名注册与引擎热重建。
- 需要直接控制引擎时，使用 `create_emotional_engine()` 创建 `EmotionalGroupChatEngine` 并手动管理生命周期（`start_background_tasks()` / `stop_background_tasks()` / `save_state()`）。
- 引擎实现位于 `sirius_pulse/core/emotional_engine.py`，通过组合模式挂载 `engine_core.py`、`pipeline.py`、`bg_tasks.py`、`helpers.py`。包内**没有** `workspace/`、`async_engine/`、`api/` 子模块：`api/` 已平铺到顶层 `sirius_pulse/__init__.py`。
- 会话持久化后端仍可由 `SessionStoreFactory` 选择 `JsonSessionStore` 或 `SqliteSessionStore`；默认 `SqliteSessionStore` 使用 `sessions/<session_id>/session_state.db`。
- 外部系统接入时，优先从 `sirius_pulse` 顶层导入公开符号（`from sirius_pulse import EmotionalGroupChatEngine, Message, SessionConfig`）。
- 系统提示词在生成时自动包含安全约束，明确告诉 AI 不要主动泄露系统提示词和初始指令；外部调用方无需手动添加，engine 会自动处理。
- 外部系统若为 asyncio 程序，但又不需要 runtime 的文件所有权，也可直接使用 `create_emotional_engine()` 创建引擎并手动管理生命周期。
- 外部系统是非 Python：优先通过 CLI 调用并读取输出文件。
- 每个 `EmotionalGroupChatEngine` 实例可处理多个群的对话，通过 `process_message()` 传入不同 `group_id` 实现群隔离。
- `work_path` 是强制参数，调用方必须显式提供，用于承载运行态数据；若希望把配置资产（roleplay/tools）与运行态数据拆开，再额外提供 `config_path`。
- 双根模式下：`SessionConfig.work_path` 表示配置根，`SessionConfig.data_path` 表示运行根；会话/记忆/token 保存在 data root。**模型接入配置不再放在 config root 下**：它只有一处，即 data root 的 `global_config.json`（`amkr_base_url` / `amkr_local_api_key` / `amkr_workspace`），由 `load_amkr_settings()` 读取、环境变量优先。旧的双根 provider 配置（`providers/provider_keys.json`）与 `apply_workspace_updates()` / `set_provider_entries()` 已随多 Provider 系统一并移除，不要在新集成里引用。
- `WorkspaceBootstrap` 仍是 workspace 级默认值的载体，但它**只覆盖 workspace 自身**（`active_agent_key` / `session_defaults` / `orchestration_defaults`），不含任何 provider 字段。`WorkspaceConfig` 会把 bootstrap payload 的签名记在 `workspace.json` 的 `bootstrap_signature`，用于判断是否需要用 bootstrap 覆盖已存在 workspace。要修改已存在 workspace，请显式改 bootstrap payload，或直接编辑 `workspace.json` / `config/session_config.json`（`ConfigManager.load_workspace_config()` / `save_workspace_config()` 是读写入口）。
- `session.json` 与 `config/session_config.json` 都支持 JSONC 风格注释；若让用户直接编辑配置，推荐保留模板中的注释键，便于对照 `docs/guide/configuration.md`。
- 推荐显式构造 `User`（`user_id/name/aliases/traits/identities`），让系统稳定识别人。
- 若外部接入需要使用 developer-only 内置 SKILL，必须至少显式标记一名可信用户为 developer；推荐使用 `UserProfile.metadata["is_developer"] = True`，不要依赖名字或角色文案推断权限。
- `profile.identities`、外部显式传入的 `name/aliases` 属于可信身份锚点；模型推断出的昵称只会写入 `runtime.inferred_aliases` 作为弱线索，不会自动变成稳定识人绑定。若业务平台有稳定昵称，务必显式传入。
- 通过 `identities` 可把不同环境（CLI/QQ/微信）的外部账号映射到同一 `user_id`。
- 群聊参与者若预先未知，优先直接使用 `EmotionalGroupChatEngine.process_message(...)` 逐条传入动态消息；`EngineRuntime` 可作为高级封装使用。
- `EmotionalGroupChatEngine` 支持四种响应策略：IMMEDIATE（立即回复）、DELAYED（延迟回复）、SILENT（不回复）、PROACTIVE（主动发言）。
- 外部系统可通过 `engine.event_bus.subscribe()` 订阅事件流，实时接收 PERCEPTION/COGNITION/DECISION/EXECUTION 事件以及 DELAYED/PROACTIVE 触发事件。
- `Message` 的 `reply_mode` 已不在 Emotional Engine 中使用；回复策略由引擎内部决策层统一决定。
- 参与决策由引擎内部 `ThresholdEngine` 与 `ResponseStrategyEngine` 自动处理；`engagement_sensitivity` 等旧参数已不再通过 `OrchestrationPolicy` 配置。
- 用户记忆已改为群隔离：`UserManager.entries` 为 `{group_id: {user_id: UserProfile}}`。
- 日记记忆由后台任务周期性生成：`_diary_promoter` 现为 `_memory_unit_checkpointer` 的向后兼容别名（旧的日记归档链路已并入记忆单元检查点），从 `basic_memory` 归档消息 LLM 总结为 `DiaryEntry`。
- 记忆系统配置通过 `emotional_engine` 配置字段完成，如 `basic_memory_hard_limit`、`diary_top_k`、`diary_token_budget`。
- ✨ **(v0.15.0)** 自身记忆触发改回主流程内联：通过 `self_memory_extract_batch_size`、`self_memory_min_chars` 和长上下文自动触发控制，不再支持 `self_memory_extract_interval_seconds`。
- ✨ **参与决策系统** (v0.14.0)：三级架构替代旧意愿分系统：HeatAnalyzer（零 LLM 开销热度分析）→ IntentAnalyzer v2（意图分类 + target 识别）→ EngagementCoordinator（融合决策）。LLM 意图分析复用 `cognition_analyze` 任务（`plugin_intent_verifier.py` 即按该任务名取配置），因此模型、温度与最大 token 都由 AMKR 的任务定义决定，本地只保留 `task_retries` 这类传输层参数。解析失败时不自动降级为关键词意图推断。多 AI 群聊里，分析器会进一步区分“当前模型自身”与“其他 AI”，并在后者场景下抑制当前模型自动回复；为降低误判，传给模型的上下文已改为最近交互链摘要，并会显式附带最近 AI 发言者、最近用户侧发言者、aliases、`environment_context`，以及当前消息命中的当前模型/其他 AI/名称含 AI 线索对象/possible-AI 候选对象等线索。对未明确点名当前模型的群控/停用类命令，还会做硬抑制，不触发当前模型回复。
- 外部系统应直接使用 `EmotionalGroupChatEngine.process_message(...)` 处理消息；旧 `WorkspaceRuntime` 的 legacy 队列系统已在 v1.0 中移除，运行时封装现为 `EngineRuntime`。
- 兼容提醒：`intent_analysis` 已不是独立任务，意图分析走 `cognition_analyze`；`enable_intent_analysis` / `intent_analysis_model` 不再被读取或迁移，会被直接忽略。另外 `OrchestrationPolicy.task_enabled` 目前只被配置层解析，引擎运行时不消费它——不要用它当作任务的启用开关。
- **后台任务**（v0.28+）：`EmotionalGroupChatEngine.start_background_tasks()` 启动 4 个后台任务：延迟队列 ticker（10 秒）、主动触发 checker（60 秒）、观察提取 promoter（5 分钟，event_memory 批量 LLM 提取）、语义整合 consolidator（10 分钟，event_memory → semantic 画像）。
- 用户侧画像由 `UnifiedUserManager` 维护（`memory/user/unified_manager.py`）；旧的 `session_user_runtime`（偏好标签、情绪线索、摘要）仅作为 schema 兼容保留，不再由业务写入，不要向它写入新数据。
- 需要按渠道身份直查时，使用 `transcript.find_user_by_channel_uid(channel, uid)`。
- `EngineRuntime` 会自动持久化 transcript，实现重启后恢复会话；若工作目录里仍有旧 `session_state.json` 或早期 `session_state(payload)` 数据，`SqliteSessionStore` 会自动迁移到 `sessions/<session_id>/session_state.db`。
- 通过 `Transcript.token_usage_records` 获取全量 token 调用归档。
- 通过 `summarize_token_usage` 和 `build_token_usage_baseline`（来自 `token/usage.py`）输出成本与损耗基准分析。
- ✨ **(v0.11.0)** 引擎自动将 token 记录持久化至 `{work_path}/token_usage.db`（SQLite）。使用 `TokenUsageStore` + `sirius_pulse.token.analytics` 进行跨会话分析（`compute_baseline`、`group_by_actor/task/model/session`、`time_series`、`full_report`）。
- 通过 `list_roleplay_question_templates()` 获取问卷模板名，再用 `generate_humanized_roleplay_questions(template=...)` 自动生成拟人化问题清单；当前支持 `default`、`companion`、`romance`、`group_chat` 四类模板。
- 若外部系统只需要模板数据，可直接从 `sirius_pulse.persona_generation` 导入 `list_roleplay_question_templates()` 与 `generate_humanized_roleplay_questions(template=...)`；CLI 当前没有对应的角色问卷开关参数。
- 通过 `agenerate_agent_prompts_from_answers`、`agenerate_from_persona_spec`（支持 `trait_keywords`、`answers`、`dependency_files`）或 `abuild_roleplay_prompt_from_answers_and_apply` 生成并应用完整 `GeneratedSessionPreset`。
- 外部调用方推荐传入高层人格 brief，而不是完整系统提示词：优先收集人物原型、核心矛盾、关系策略、情绪原则、表达节奏、边界和小缺点，再交给生成人格 API 落成具体人物小传与语言习惯。
- 对 `abuild_roleplay_prompt_from_answers_and_apply(...)`、`aupdate_agent_prompt(...)`、`aregenerate_agent_prompt_from_dependencies(...)` 这三条持久化链路，框架会先把最新 `PersonaSpec` 和待生成快照写入 `work_path`，再发起模型调用；若生成失败，可用 `load_persona_spec(work_path, agent_key)` 恢复最近一次输入。
- 结构化人格生成默认使用 `max_tokens=5120` 和 `timeout_seconds=120.0`，并把 `timeout_seconds` 透传到 `GenerationRequest`；请求级 timeout 优先于 provider 构造器上的默认 30 秒（由 `resolve_generation_timeout_seconds()` 决定）。
- 若模型返回被 ```json 包裹但未完整闭合的 JSON-like 响应，框架会显式报错并把原始响应保留在 `roleplay/generated_agent_traces/<agent_key>.json`，避免脏数据覆盖现有人格配置。
- 当外部素材文件（角色卡、语气样本、设定稿）变化时，可使用 `aregenerate_agent_prompt_from_dependencies(...)` 重新读取 `dependency_files` 并重生人格，无需重新收集问答。
- 推荐采用 agent-first：先生成并持久化 agent 资产（`roleplay/generated_agents.json`），再用 `select_generated_agent_profile(work_path, agent_key)` 选择，最后通过 `EngineRuntime` 或 `create_session_config_from_selected_agent(...)` 创建会话。
- 每次生成的完整过程都会本地化到 `{work_path}/roleplay/generated_agent_traces/<agent_key>.json`；外部若需审计/回放，可调用 `load_persona_generation_traces(...)`。
- **多模态输入**：本地图片路径与 `file://` URI 会在发送前由 `prepare_openai_compatible_messages()` 转成 data URL（AMKR 不负责这一步，框架必须自己做）；传公网 URL 时需确保上游能直接下载。**多模态用哪个模型由 AMKR 的任务定义决定**，框架不再提供「有图像时自动升级模型」的本地切换逻辑，`Agent.metadata["multimodal_model"]` 已无消费者，不要在新集成里依赖它。
- 通过 `history_max_messages/history_max_chars` 启用自动记忆压缩，控制 token 增长。
- ✨ **配置管理** (P1-006)：使用 `ConfigManager` 处理多环境配置
  - 支持多环境配置文件（base.json/dev.json/test.json/prod.json）
  - 支持 ${VAR_NAME} 环境变量替换语法
  - 可选验证配置的有效性
  - 示例：`from sirius_pulse.config import ConfigManager; cfg = ConfigManager.load_from_json('config/base.json')`
- ✨ **SKILL 系统**：通过 `tools/` 模块让 AI 在运行时调用外部 Python 代码
  - 默认：`enable_tools=True`；框架会先加载包内置 Tool（当前为 `autonomy`、`bash`、`desktop_screenshot`、`group_file_exec`、`group_management`、`intend_share`、`interaction_with_master`、`qq_like`、`qq_member_info`、`read_skill`、`web_lookup`、`workflow_state`，见 `sirius_pulse/tools/builtin/`），再加载 workspace `tools/` 目录。Tool 文件默认放在 `{work_path}/tools/`，双根布局时位于 `config_root/tools/`。若只想保留目录结构、不执行 Tool，可显式设置 `enable_tools=False`
  - 加载时机：框架启动时预加载，`tools/` 目录变化时自动全量重载；不再在每条 message 路径上扫描 SKILL
  - 覆盖规则：如果 workspace 中存在同名文件（如 `tools/system_info.py`），则以 workspace 版本覆盖内置实现
  - 权限模型：developer-only SKILL 只会在 developer 当前轮次出现在提示词中，执行时 runtime 会再次校验当前调用者是否被显式标记为 developer
  - SKILL 文件需导出 `TOOL_META` 字典（含 name, description, parameters, 可选 dependencies、`developer_only` 与 `silent`）和 `run(**kwargs)` 函数；如需审计调用者，可显式接收 `invocation_context`。`silent=True` 时 SKILL 结果不追加到回复文本，仅保留在内部元数据中
  - 依赖自动安装：加载 SKILL 前自动扫描 `TOOL_META["dependencies"]` 和 import 语句，用 `uv pip install`（回退 `pip`）安装缺失包。内置 SKILL 与 workspace SKILL 共用这条流程，可通过 `auto_install_tool_deps=False` 关闭
  - 持久化：每个 SKILL 自动获得独立的 JSON 键值存储（`ToolDataStore`），通过 `data_store` 参数注入
  - 超时：`tool_execution_timeout`（默认 30 秒），超时返回 `ToolResult(success=False)`
  - 引擎自动检测 AI 回复中的内置 `[TOOL_CALL: name | {params}]` 标记并执行，结果会先规范化为内部文本/多模态通道后再重新生成
  - 若 SKILL 返回 `text_blocks`、`multimodal_blocks`、`internal_metadata`，模型只会看到内部推理通道；最终回复会被约束为只输出用户有用的结论，不复述字段名、`mime_type`、`label`、路径或 URL
  - 导入：`from sirius_pulse import ToolRegistry, ToolExecutor, ToolDataStore, ToolInvocationContext, resolve_tool_dependencies`
  - 内置 Tool 参考：`sirius_pulse/tools/builtin/`
- 任何情况下，不应在编排核心中写入厂商或模型细节（**AMKR 边界原则**）。
  - 供应商、Key 池、模型选择、采样参数与故障切换全部由外部 AMKR（`auto-model-key-router`）承担，框架侧只保留一个 `OpenAICompatibleProvider`（`sirius_pulse/providers/openai_compatible.py`），端点固定为 `<amkr_base_url>/v1/chat/completions`。
  - 框架不再有厂商 Provider 实现、`models.dev` 模型目录、进程级 HTTP 代理配置，也没有本地路由注册表——`provider_keys.json`、`AutoRoutingProvider`、`ProviderRegistry` 均已随多 Provider 系统删除，加回来属于架构回退。
  - **任务名是唯一线协议**：框架把任务名本身填进 OpenAI 兼容请求的 `model` 字段（如 `response_generate`、`memory_extract`），AMKR 用它查任务定义换成真实模型。
  - `sirius_pulse/core/model_router.py` 的 `_DEFAULT_TASK_REGISTRY` 只声明**本地**关注点：每个任务名的 `timeout` 与 `retries`，外加预算估算用的默认值。`TaskConfig.model_name` 始终等于任务名本身，`resolve()` 不按本地启发式换模型。
  - `sirius_pulse/core/emotional_engine.py` 通过 `LLMProvider`/`AsyncLLMProvider`（`providers/base.py`）与实现解耦；模型选择不在引擎层发生。
  - **不要给任务名传 `temperature` / `max_tokens`**：命中任务名时框架会省略这两个字段，让 AMKR 任务定义里的固定值生效；显式传会被 AMKR 以 400 拒绝，不做静默覆盖。
  - 任务的超时与重试是本地传输层参数，写在 `data/personas/<name>/engine_state/orchestration.json` 的 `task_timeout` / `task_retries`。
- 内部重构若影响外部接口（当前未发布阶段），可直接调整顶层导出，并同步外部文档。
- 内部新增功能必须同步在 `sirius_pulse/__init__.py` 暴露可调用接口。
- 发起请求前，本地图片路径与 `file://` URI 会被转成 data URL；不要指望 AMKR 代做这一步。

## 最小可用接入模板

- 接入前先配好 AMKR：在 `data/global_config.json` 写入 `amkr_base_url` / `amkr_local_api_key` / `amkr_workspace`（或设置 `SIRIUS_AMKR_BASE_URL` / `SIRIUS_AMKR_API_KEY` / `SIRIUS_AMKR_WORKSPACE`）。缺少凭据时引擎不就绪，`EngineRuntime.is_ready()` 返回 `False`。
- 首次启动会自动把 12 个内置任务名注册进 AMKR 的 `<amkr_workspace>/<persona>` 空间（只创建缺失项）；也可用 WebUI 的 `POST /api/amkr/register` 手动触发，用 `GET /api/amkr/status` 查看 `registered` / `missing`。
- Python 调用：从顶层 `sirius_pulse` 导入并构造引擎，参考本文档「接入决策规则」中的入口说明。
- CLI 入口：`python main.py run`（活跃人格 + WebUI）、`python main.py webui [--foreground|--status|--stop]`、`python main.py persona list|create|activate|delete`。CLI 当前没有 `--config` / `--work-path` 参数。
- 仓库内的 `examples/` 目录已整体删除，不要引用其中的示例文件。

## 变更同步要求（强制）

当以下内容发生变化时，必须同步更新本 SKILL：

1. 外部接入方式（API 或 CLI）
2. 配置结构或关键参数
3. AMKR 边界约束（任务名契约、工作空间、注册策略）

并同步更新：

- `README.md`（用户可见用法）
- `docs/guide/architecture-overview.md`（若边界变化）
- `docs/reference/provider-config.md` 与 `docs/modules/provider-system.md`（若 AMKR 契约变化）
- `docs/reference/python-api.md` / `docs/reference/webui-api.md`（若对外 API 变化）

## AMKR 边界补充

Sirius Pulse **不再自带多供应商系统**。所有模型调用都发往本地 [AMKR](https://github.com/Sparrived/auto-model-key-router)（`auto-model-key-router`），一个 OpenAI 兼容的本地路由服务；供应商、Key 池、故障切换、真实模型选择与采样参数全部由它承担。

### 唯一实现与连接配置

- 框架侧只有一个实现：`OpenAICompatibleProvider`，端点固定为 `<amkr_base_url>/v1/chat/completions`。
- 连接配置来自 `data/global_config.json`，由 `load_amkr_settings()` 解析，环境变量优先：

  | 配置键 | 环境变量 | 默认值 |
  |---|---|---|
  | `amkr_base_url` | `SIRIUS_AMKR_BASE_URL` | `http://127.0.0.1:8000` |
  | `amkr_local_api_key` | `SIRIUS_AMKR_API_KEY` | 空（未配置则引擎不就绪） |
  | `amkr_workspace` | `SIRIUS_AMKR_WORKSPACE` | `sirius-pulse` |

- `amkr_local_api_key` 是 AMKR 的**本地授权 Key，同时是它的管理员凭据**（可增删供应商与 Key），因此只保存在服务端；WebUI 响应中脱敏为 `sk-a****`，提交脱敏值时服务端保留磁盘原值。`amkr_ui_enabled` 控制是否显示跳转 AMKR 面板的外链。

### 任务名契约（核心）

- AMKR 把一个**任务名**解析成真实模型，因此框架把任务名本身填进 OpenAI 兼容请求的 `model` 字段。
- 内置 12 个任务名：`cognition_analyze`、`memory_extract`、`response_generate`、`proactive_generate`、`passive_tool`、`plugin_analyze`、`plugin_generate`、`plugin_render`、`plugin_raw`、`diary_generate`、`diary_consolidate`、`topic_cluster`。
- `model` 命中任务名时框架**不发送** `temperature` 与 `max_tokens`，模型选择、温度、最大 token 与故障切换都取 AMKR 任务定义里的值。显式传任务已固定的参数会被 AMKR 以 400 拒绝，不会静默覆盖。
- `model` 不是任务名时按普通模型直连，此时采样参数由框架给出。任务名必须与 AMKR 工作空间里的任务同名，否则 AMKR 会把它当成真实模型去查找并失败。
- 本地的任务超时与重试写在 `data/personas/<name>/engine_state/orchestration.json` 的 `task_timeout` / `task_retries`，属于传输层参数；`_DEFAULT_TASK_REGISTRY` 里的 `temperature` / `max_tokens` 仅供预算估算。

### 工作空间与注册

- AMKR 是**共享单实例**，多个 AI 服务可同时使用，**不是多租户**。隔离靠请求头 `X-AMKR-Workspace`，框架按人格拼接为 `<amkr_workspace>/<persona>`（如 `sirius-pulse/sirius`，由 `workspace_for()` 生成）。
- 工作空间由框架**显式创建**（`POST /api/workspaces`，见 `ensure_persona_workspace_key()`），不再靠「建第一个任务」隐式产生。原因：创建的那一刻是拿到该空间**面板 key** 的唯一时机，之后 AMKR 的目录与导出都刻意剥掉它。顺序必须是**先建空间拿 key，再注册任务**。请求头为空时不发送，等价于 AMKR 的默认工作空间。
- 面板 key 存在 `data/global_config.json` 的 `amkr_panel_keys`（`{工作空间: key}` 明文映射，只为服务端持有）。**该字段绝不随 `GET /api/global-config` 回显**；面板地址只从管理员专用的 `GET /api/amkr/panel?persona=` 取，地址形如 `<ui_url>/panel.html#k=<key>`，凭据必须在 fragment 里（查询串会进 `Referer` 与服务端日志）。
- 若空间已在 AMKR 侧存在而本地没有 key，AMKR 只返回 409 且不会重发 key：此时注册会报错并提示去读 AMKR 配置文件的 `workspaces.<空间>.api_key`，或删掉该空间后重建。
- 注册**只创建缺失的任务名**，已存在的一律不比对、不更新——后续所有模型与参数调整都在 AMKR 自带 WebUI 里完成，避免每次启动把运维调好的配置打回去。注册是幂等的，可重复触发。
- 写操作遵循 AMKR 的乐观并发（携带 `config_revision`），版本过期返回 409 时重读并重试一次。

### 已移除的接口（不要在新接入里引用）

- 厂商 Provider：`AliyunBailianProvider`、`BigModelProvider`、`DeepSeekProvider`、`MiMoProvider`、`OpenCodeProvider`、`SiliconFlowProvider`、`VolcengineArkProvider`、`YteaProvider`。
- 路由与注册表：`AutoRoutingProvider`、`ProviderRegistry`、`WorkspaceProviderManager`、`ProviderConfig`、`providers/routing.py`、`providers/models_dev.py`、`providers/proxy.py`。
- CLI：`/provider add|remove|list|platforms`（交互模式下的 API Key 管理命令）。
- WebUI：`/api/providers*` 全部路由、Providers 页面、模型编排页、`/api/persona/orchestration`、`/api/persona/task-params`、`data/providers/proxy.json`。
- 配置：`ProviderPolicy`、provider 注册表相关字段、`data/providers/` 目录（不再创建也不再监听）。
- 中间件：`MiddlewareChain`、`RateLimiterMiddleware`、`RetryMiddleware`、`CircuitBreakerMiddleware`、`CostMetricsMiddleware` 在当前代码中不存在，不要引用。

### 仍然保留的 WebUI / API

- `GET /api/amkr/status`：只读巡检（连通性、版本、各人格 `registered` / `missing` / `panel_ready`），可安全反复调用；**不含**面板 key 或面板地址。
- `GET /api/amkr/panel?persona=<名字>`：**仅管理员**（非 admin 返回 403）。返回 `{"persona", "url"}`，`url` 是可嵌入的 AMKR 工作空间面板地址（fragment 内含明文 key）；该人格没有 key 时返回 409 并说明补救路径。运维页按需调用它再把地址塞进 iframe。
- `POST /api/amkr/register`：建出工作空间（含取面板 key）并补齐缺失任务名；body `{"persona": "..."}` 指定单个人格，`{}` 表示全部人格。
- `GET /api/models`：仍然存在，但返回的是上述 12 个任务名（含中文标签），不再是厂商模型列表。

### 生产环境建议

- 配置每个任务的 `task_retries`，并限制多模态输入规模（`max_multimodal_inputs_per_turn`），避免上游抖动与超长输入导致失败。


