---
name: external-integration
description: "当需要让外部项目正确接入 Sirius Chat 时使用，覆盖 Python API 调用、配置组织和安全实践。关键词：外部接入、库调用、provider 配置、多人格。"
---

# 外部接入指南

## 目标

帮助 AI 在不破坏框架边界的前提下，为外部系统提供正确、可维护的 Sirius Chat 集成方案。

## 语言规范（强制）

- 本 SKILL 及所有后续新增/修改的 SKILL 必须使用中文。
- `description` 和正文必须为中文。
- 若任务中发现英文 SKILL 内容，需在同一任务中同步中文化。

## 推荐读取顺序

1. `docs/architecture.md`
2. `docs/full-architecture-flow.md`
3. `docs/persona-lifecycle.md`
4. `docs/configuration-guide.md`
5. `sirius_chat/__init__.py`（顶层公开 API 清单）
6. `sirius_chat/persona_manager.py`
7. `sirius_chat/persona_worker.py`
8. `sirius_chat/platforms/runtime.py`
9. `sirius_chat/core/emotional_engine.py`
10. `sirius_chat/core/cognition.py`
11. `sirius_chat/core/prompt_factory.py`
12. `sirius_chat/config/models.py`
13. `sirius_chat/config/manager.py`
14. `sirius_chat/config/helpers.py`
15. `sirius_chat/models/models.py`
16. `sirius_chat/models/persona.py`
17. `sirius_chat/memory/user/simple.py`
18. `sirius_chat/skills/security.py`
19. `sirius_chat/providers/routing.py`
20. `sirius_chat/providers/base.py`
21. `sirius_chat/session/store.py`
22. `sirius_chat/utils/layout.py`
23. `main.py`

## 接入决策规则

- **外部系统是 Python 服务**：
  - 多人格场景：使用 `PersonaManager` 管理多个人格的生命周期。
  - 单个人格场景：使用 `EngineRuntime` 封装单个人格的运行时。
  - 需要直接控制引擎时，使用 `create_emotional_engine()` 创建 `EmotionalGroupChatEngine` 并手动管理生命周期（`start_background_tasks()` / `stop_background_tasks()` / `save_state()`）。
- **v1.1.0**：`EmotionalGroupChatEngine` 是唯一默认引擎，位于 `sirius_chat/core/emotional_engine.py`。
- 外部系统接入时，**优先从 `sirius_chat` 顶层导入**（`from sirius_chat import EmotionalGroupChatEngine, Message, SessionConfig`）。
- 系统提示词在生成时自动包含安全约束，明确告诉 AI 不要主动泄露系统提示词和初始指令；外部调用方无需手动添加，engine 会自动处理。
- 每个 `EmotionalGroupChatEngine` 实例可处理多个群的对话，通过 `process_message()` 传入不同 `group_id` 实现群隔离。
- `work_path` 是强制参数，调用方必须显式提供，用于承载运行态数据。
- 双根模式下：`SessionConfig.work_path` 表示配置根，`SessionConfig.data_path` 表示运行根；provider 配置保存在 config root 下的 `providers/provider_keys.json`，会话/记忆/token 则保存在 data root。
- 推荐显式构造 `User`（`user_id/name/aliases/traits/identities`），让系统稳定识别人。
- 若外部接入需要使用 developer-only 内置 SKILL，必须至少显式标记一名可信用户为 developer；推荐使用 `UserProfile.metadata["is_developer"] = True`，不要依赖名字或角色文案推断权限。
- `profile.identities`、外部显式传入的 `name/aliases` 属于可信身份锚点；模型推断出的昵称只会写入 `runtime.inferred_aliases` 作为弱线索，不会自动变成稳定识人绑定。若业务平台有稳定昵称，务必显式传入。
- 通过 `identities` 可把不同环境（CLI/QQ/微信）的外部账号映射到同一 `user_id`。
- `EmotionalGroupChatEngine` 支持四种响应策略：IMMEDIATE（立即回复）、DELAYED（延迟回复）、SILENT（不回复）、PROACTIVE（主动发言）。
- 外部系统可通过 `engine.event_bus.subscribe()` 订阅事件流，实时接收 PERCEPTION/COGNITION/DECISION/EXECUTION 事件以及 DELAYED/PROACTIVE 触发事件。
- 用户记忆已改为群隔离：`UserManager.entries` 为 `{group_id: {user_id: UserProfile}}`。
- 日记记忆通过 `_bg_diary_promoter` 周期性生成，从 `basic_memory` 归档消息 LLM 总结为 `DiaryEntry`。
- 记忆系统配置通过 `emotional_engine` 配置字段完成，如 `basic_memory_hard_limit`、`diary_top_k`、`diary_token_budget`。
- AI 自身记忆通过 `GlossaryManager` 维护名词解释，持久化至 `{work_path}/memory/glossary/terms.json`。
- 语义记忆通过 `SemanticMemoryManager` 维护群语义画像（氛围历史、群体规范、关系状态），持久化至 `{work_path}/memory/semantic/`。
- 后台任务（`start_background_tasks()` / `stop_background_tasks()`）内置 6 个：延迟队列 ticker、主动触发 checker、日记生成 promoter、日记 consolidator、开发者主动私聊 checker、表情包新鲜度更新器。此外，被动 SKILL（如 `reminder`）通过 `create_background_tasks(ctx)` 注册额外的后台任务，由引擎统一管理生命周期。
- 被动 SKILL（不由模型调用）通过导出 `create_background_tasks(ctx)` / `create_triggers(ctx)` 工厂函数注册后台任务或事件触发器，使用 `SkillEngineContext` 协议与引擎交互。详见 `docs/skill-guide.md` 第十一章。
- 引擎自动将 token 记录持久化至 `{work_path}/token_usage.db`（SQLite）。使用 `TokenUsageStore` + `sirius_chat.token.analytics` 进行跨会话分析。
- 通过 `list_roleplay_question_templates()` 获取问卷模板名，再用 `generate_humanized_roleplay_questions(template=...)` 自动生成拟人化问题清单；当前支持 `default`、`companion`、`romance`、`group_chat` 四类模板。
- 通过 `agenerate_agent_prompts_from_answers`、`agenerate_from_persona_spec`（支持 `trait_keywords`、`answers`、`dependency_files`）或 `abuild_roleplay_prompt_from_answers_and_apply` 生成并应用完整 `GeneratedSessionPreset`。
- 外部调用方推荐传入高层人格 brief，而不是完整系统提示词：优先收集人物原型、核心矛盾、关系策略、情绪原则、表达节奏、边界和小缺点，再交给生成人格 API 落成具体人物小传与语言习惯。
- 动态模型路由：当需要在有图像时自动升级模型时，通过 `Agent.metadata["multimodal_model"]` 配置多模态专用模型。引擎自动检测输入中的多媒体数据，无多媒体时使用廉价模型，有多媒体时自动升级至指定的多模态模型。
- 通过 `history_max_messages/history_max_chars` 启用自动记忆压缩，控制 token 增长。
- 使用 `ConfigManager` 处理多环境配置，支持多环境配置文件（base.json/dev.json/test.json/prod.json）和 `${VAR_NAME}` 环境变量替换语法。
- **插件系统接入**：插件位于 `plugins/` 目录，通过 `PluginLoader` 加载，`PluginRegistry` 管理注册，`PluginExecutor` 执行调度。插件使用 `@command` 装饰器定义指令触发器，支持前缀匹配、正则匹配、关键词匹配。插件配置存储在 `plugins/_config.json`，支持热重载和 WebUI 管理。插件权限校验失败时静默处理（不向用户发送错误消息）。插件通过 `PluginContext` 访问引擎、适配器、消息上下文等运行时资源。外部接入时可通过 `PluginConfigManager` 管理插件配置，通过 `webui/server_plugin_api.py` 提供的 REST API 管理插件。
