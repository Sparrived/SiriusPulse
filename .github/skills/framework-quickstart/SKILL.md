---
name: framework-quickstart
description: "你擅长使用自己的技能为其他人解决问题。当你需要在不通读全部代码的情况下快速理解 Sirius Pulse 架构时使用，包括模块边界、执行流与扩展点。关键词：架构总览、框架地图、修改位置、provider 集成。"
---

# 框架快速上手

## 目标

在开始修改前，快速建立对 Sirius Pulse 当前代码结构的准确认知，优先搞清楚：

- 推荐入口是什么
- 真正的 engine 实现位于哪里
- workspace / config / session / provider / memory 的边界如何划分
- 哪些文件是当前架构的事实来源，哪些只是兼容层或历史迁移材料

补充目标：本项目致力于构建具有真实情感表达、能提供帮助与情绪价值的核心引擎。

## 语言规范

- 本仓库所有 SKILL 文件必须使用中文编写。
- 后续新增或修改任意 SKILL 时，frontmatter 的 `description` 与正文均需使用中文。
- 若发现历史 SKILL 出现英文内容，需在当前任务中一并改为中文。

## 阅读顺序（先做这个）

1. `docs/architecture.md`
2. `docs/full-architecture-flow.md`
3. `README.md`
4. `docs/orchestration-policy.md`（任务模型覆盖与动态路由）
5. `sirius_pulse/models/models.py` ✨ (包重构)
6. `docs/external-usage.md`
7. `sirius_pulse/api/__init__.py`
8. `sirius_pulse/workspace/layout.py`
9. `sirius_pulse/workspace/runtime.py`
10. `sirius_pulse/workspace/roleplay_manager.py`
11. `sirius_pulse/config/models.py`
12. `sirius_pulse/config/manager.py`
13. `sirius_pulse/persona_generation/`
14. `sirius_pulse/core/emotional_engine.py`
15. `sirius_pulse/core/cognition.py`
16. `sirius_pulse/core/prompt_factory.py`
17. `sirius_pulse/core/response_strategy.py`
18. `sirius_pulse/core/model_router.py`
19. `sirius_pulse/memory/basic/manager.py`
20. `sirius_pulse/memory/diary/manager.py`
21. `sirius_pulse/memory/user/simple.py`
22. `sirius_pulse/memory/glossary/manager.py`
23. `sirius_pulse/memory/context_assembler.py`
24. `sirius_pulse/core/identity_resolver.py`
24. `sirius_pulse/session/store.py`
25. `sirius_pulse/providers/base.py`
26. `sirius_pulse/providers/routing.py`
27. `sirius_pulse/providers/middleware/base.py`
28. `sirius_pulse/cli.py`
29. `tests/test_workspace_runtime.py`
30. `tests/test_emotional_engine_basic.py`

- `models/models.py` ✨ **（包重构）** 定义数据契约（多人用户 + 单 AI 主助手）。
- 任务路由与模型选择由 `ModelRouter` 管理，通过 `emotional_engine.task_model_overrides` 配置。v1.0 中 `OrchestrationPolicy` dataclass 已废弃，不再用于任务控制。
- 兼容层面，旧 `enable_intent_analysis` / `intent_analysis_model` 仅作为读取时的映射入口存在；当前模板、workspace 持久化与示例应统一使用 `task_enabled/task_models`。

## 心智模型

- 当前推荐入口是 `WorkspaceRuntime`；它负责文件布局、session 恢复、participants 写回、watcher 热刷新和 provider 注册表联动。
- **v1.0 默认引擎** `EmotionalGroupChatEngine` 直接处理消息，不经过 `run_live_message` 的 legacy 队列系统。
- `WorkspaceRuntime.initialize()` 会预先初始化共享 SKILL runtime，并在 `skills/` 目录变化时通过 watcher 触发全量 reload，不再在消息路径按次扫描目录。SKILL runtime 会先加载包内置技能（当前包含 `system_info`、`learn_term`、`url_content_reader`、`bing_search` 与 developer-only 的 `desktop_screenshot`），再加载 workspace `skills/`；同名 workspace 文件覆盖内置实现。
- 内置 SKILL 与 workspace SKILL 共用依赖自动安装路径；`SKILL_META["dependencies"]` 会在模块真正导入前参与解析。
- SKILL 执行结果现在支持结构化 `text_blocks` / `multimodal_blocks` / `internal_metadata`；`core/emotional_engine.py` 负责把结果注入 basic memory，`core/prompt_factory.py` 负责把可用文本与图片转成隐藏模型上下文，并在最近少量 assistant turn 内继续保留这些内部结果，避免模型在短期追问里立刻忘掉刚拿到的观察，同时避免把元信息泄露到用户回复中。
- `Participant.metadata` / `UserProfile.metadata` 中的 `is_developer` 是 SKILL 安全模型的显式权限来源；engine 会据此构建 `SkillInvocationContext`，让 developer-only 工具在非 developer 当前轮次中自动隐藏，并在执行时再次校验。
- 会话事件流包含 PERCEPTION/COGNITION/DECISION/EXECUTION 四层管道事件，以及 DELAYED/PROACTIVE 触发事件。技能执行结果由 `prompt_factory` 注入 assistant 回复，不通过独立事件暴露。
- `WorkspaceRuntime` 会把 `WorkspaceBootstrap` 的签名记入 `workspace.json`；同一份 bootstrap 只在首次命中时持久化一次，后续重启会保留用户在 config root 下的手工修改。
- `WorkspaceLayout` 是路径语义的单一事实来源：config root 放配置与资产，data root 放运行态数据。
- **v1.0.0 默认引擎** `EmotionalGroupChatEngine` 的真实实现位于 `sirius_pulse/core/emotional_engine.py`；采用四层认知架构（感知→认知→决策→执行）与简化记忆模型（基础记忆 → 日记记忆 → 语义记忆）。
- `sirius_pulse/async_engine/` 只承担兼容导出与 prompts/orchestration/utils 辅助层。
- 一个 `SessionConfig` 只对应一个主 AI，主 AI 由 `preset=AgentPreset(...)` 描述，不再推荐在外部配置里手写完整 agent prompt。
- `User` 只是 `Participant` 的别名；运行时识人与记忆的事实来源是 `engine.user_manager`，而不是旧版 `participants` 配置字段。`profile.identities/name/aliases` 是可信身份锚点，`runtime.inferred_aliases` 只是弱线索，不参与稳定识人绑定。
- provider 注册表由 `WorkspaceProviderManager` 管理，路由顺序是 `models` 列表优先、`healthcheck_model` 次之、最后回退到第一个启用 provider。
- roleplay 资产统一存放在 `roleplay/generated_agents.json` 与 `roleplay/generated_agent_traces/`，`active_agent_key` 决定 `SessionConfig` 使用哪份资产。
- session store、token store、memory store、SKILL data store 都已经收敛到 workspace 语义下，修改这些层时必须同时检查路径文档。

## 修改路由指南

- 新增 provider：修改 `sirius_pulse/providers/`、`sirius_pulse/providers/routing.py`、`sirius_pulse/api/providers.py`，并补测试与文档。
- 修改对话主流程（当前唯一 Emotional 引擎）：优先检查 `sirius_pulse/core/emotional_engine.py`、`core/prompt_factory.py`、`core/cognition.py`、`core/response_strategy.py`。
- 修改记忆系统（基础记忆 / 日记记忆 / 用户管理 / 名词解释）：同步检查 `sirius_pulse/memory/basic/manager.py`、`sirius_pulse/memory/diary/manager.py`、`sirius_pulse/memory/user/simple.py`、`sirius_pulse/memory/glossary/manager.py`、`sirius_pulse/memory/context_assembler.py`、`sirius_pulse/core/identity_resolver.py`。
- 修改 workspace / session 持久化：同步检查 `sirius_pulse/workspace/`、`sirius_pulse/config/manager.py`、`sirius_pulse/session/store.py`。
- 修改识人或记忆逻辑：同步检查 `sirius_pulse/memory/user/simple.py`、`sirius_pulse/core/identity_resolver.py`、`sirius_pulse/models/models.py` 与 `docs/external-usage.md`。
- 修改外部 API：同步更新 `sirius_pulse/api/`、README、`docs/external-usage.md` 与示例代码。
- 修改 roleplay 资产流：同步更新 `sirius_pulse/roleplay_prompting.py`、`workspace/roleplay_manager.py` 和架构文档。
- `providers/*` 实现具体的 LLM 后端。
- `roleplay_prompting.py` 提供自动问题清单、回答提取式提示词生成、关键词/依赖文件驱动的人格生成、人格持久化、完整本地生成轨迹与依赖文件重生能力；问卷支持 `default` / `companion` / `romance` / `group_chat` 四类模板，可通过 `list_roleplay_question_templates()` 获取模板名，再用 `generate_humanized_roleplay_questions(template=...)` 生成对应的高层人格问卷。人格资产现统一存放于 `roleplay/generated_agents.json` 与 `roleplay/generated_agent_traces/`；对会写入 `work_path` 的人格生成链路，会先暂存 `PersonaSpec` 与待生成快照，再调用模型；结构化人格生成默认使用 `max_tokens=5120`、`timeout_seconds=120.0`，并通过 `GenerationRequest.timeout_seconds` 透传请求级超时。
- 内置 provider 包含 `OpenAICompatibleProvider`、`AliyunBailianProvider`、`DeepSeekProvider`、`SiliconFlowProvider` 与 `VolcengineArkProvider`。
- 若配置了多 provider，`AutoRoutingProvider` 会优先按 `ProviderConfig.models`，其次按 `healthcheck_model` 精确选择可用 provider。
- `cli.py` 是库内薄封装，默认执行单轮会话；同时提供人格模板辅助命令 `--list-roleplay-question-templates` 与 `--print-roleplay-questions-template <template>`，方便外部快速导出问卷模板。
- `api/` 是统一对外接口文件；外部调用优先使用该文件暴露的 API。
- Provider 检测流程已下沉到 `providers/routing.py`：配置检查 -> 平台适配检查 -> 可用性检查（依赖 `healthcheck_model`）。
- Provider 注册命令要求显式提供检测模型：`/provider add <type> <api_key> <healthcheck_model> [base_url]`。
- 提示词流程：`list_roleplay_question_templates()` 暴露问卷模板枚举，`generate_humanized_roleplay_questions(template=...)` 产出高层人格问题；`agenerate_agent_prompts_from_answers` / `agenerate_from_persona_spec`（支持 `trait_keywords`、`answers`、`dependency_files`）生成完整 `GeneratedSessionPreset`。推荐先收集人物原型、核心矛盾、关系策略、情绪原则、表达节奏、边界和小缺点等上位约束，再让生成器展开为具体人物小传与语言习惯；推荐将生成结果作为 agent 资产持久化（`roleplay/generated_agents.json`），并利用 `roleplay/generated_agent_traces/<agent_key>.json` 保存完整生成轨迹。对于 `abuild_roleplay_prompt_from_answers_and_apply(...)`、`aupdate_agent_prompt(...)`、`aregenerate_agent_prompt_from_dependencies(...)` 三条持久化链路，框架会先落盘输入快照，再调用模型，失败时可通过 `load_persona_spec(...)` 恢复最近一次输入。依赖文件更新后可调用 `aregenerate_agent_prompt_from_dependencies(...)` 直接重生人格。
- 内部实现允许重构；当前未发布阶段若影响外部接口，可直接升级 `api/`，并同步文档与示例。
- 内部新增能力需同步在 `api/` 提供对外入口。
- `main.py` 是仓库级测试/业务入口，承载主用户档案初始化、provider 管理命令与持续会话流程。
- ✨ **开发工具链** (P1-004)：
  - `.github/workflows/ci.yml`：GitHub Actions 多版本 Python 自动化测试与代码质量检查
  - `.pre-commit-config.yaml`：预提交钩子 (black, isort, flake8, mypy, bandit 等)
  - `scripts/ci_check.py`：本地/CI 检查脚本
  - `scripts/setup_dev_env.py`：开发环境自动化初始化
  - `Makefile`：便捷开发命令集

## 修改路由指南（补充）

- 新增 provider 支持：修改 `sirius_pulse/providers/`，并保持 `sirius_pulse/core/emotional_engine.py` 不含 provider 细节。
- 修改主 AI 或多人轮次策略：更新 `sirius_pulse/core/emotional_engine.py`，并检查 transcript 兼容性。
- 修改动态参与者或识人记忆逻辑：同步更新 `models/models.py`、`sirius_pulse/core/emotional_engine.py` 与 `docs/external-usage.md`。
- 修改会话恢复或压缩策略：同步更新 `workspace/`、`session/store.py`、`session/runner.py`、`docs/architecture.md`、相关迁移文档；若外部可见行为变化，再同步 `README.md`。
- 修改配置结构或环境变量处理：同步更新 `sirius_pulse/config/manager.py`、`sirius_pulse/cli.py`、`README.md` 与 `examples/session.json`。
- 修改缓存策略或后端：在 `sirius_pulse/cache/` 实现新后端或修改现有接口，并更新 `docs/best-practices.md`。
- 修改性能监控或基准：更新 `sirius_pulse/performance/` 中的指标收集或分析逻辑，添加相应测试。
- 修改 engine/provider 行为：在 `tests/` 下新增或更新测试。
- 新增可对外使用功能：在 `sirius_pulse/api/` 暴露接口并补充外部调用示例。

## 代码变更后的必做同步

当架构、命令或 API 形态变化后，必须同步更新：

1. `docs/architecture.md`
2. `docs/full-architecture-flow.md`
3. `docs/memory-system.md`（若涉及记忆系统变更；如不存在则集中写入 `docs/architecture.md`）
4. `docs/engine-emotional.md`（若涉及引擎变更；如不存在则集中写入 `docs/architecture.md`）
5. `README.md`（若用户可见用法发生变化）
6. 本文件（`.github/skills/framework-quickstart/SKILL.md`）
7. 相关 SKILL 文件（`.github/skills/external-integration/SKILL.md` 等）

**重点提醒**：实现新功能后，**不应自动生成额外的 markdown 文档**来说明新功能的用法（如指南、快速启动、参考手册），除非用户明确提及。应将功能文档集中在现有位置或等待用户要求。
